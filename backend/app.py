"""API de AccesoSeguro (Flask) — corre en ECS Fargate detrás del ALB.

Cambios respecto a la versión v3 local:
- Base de datos: RDS PostgreSQL con las variables que inyecta ECS (DB_HOST,
  DB_PORT, DB_NAME, DB_USER y DB_PASSWORD desde Secrets Manager). Si no hay
  DB_HOST, usa SQLite local (para probar en la compu).
- Autenticación: Cognito. Flask valida el token (firma, emisor, cliente y
  vencimiento) con las llaves públicas que Terraform le pasa en COGNITO_JWKS.
  Autorización: según el grupo del usuario (cognito:groups). Sin COGNITO_JWKS
  (modo local) la autenticación queda desactivada.
- Documentos: el navegador sube el PDF directo a S3 con una presigned URL
  (no pasa por la API). La clave va por empresa:
  empresas/{empresa}/solicitudes/{id}/documento-{uuid}.pdf
  Cada PDF nuevo dispara S3 -> SQS -> Lambda validar-documento.
- Eventos: cada cambio importante se publica en SNS (fan-out a auditoría y
  notificaciones).
"""
import csv
import io
import json
import os
import re
import uuid
from datetime import datetime, timedelta
from functools import wraps
from urllib.parse import quote_plus

import boto3
import jwt
from botocore.config import Config
from flask import Flask, Response, g, jsonify, request
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy

REGION = os.environ.get("AWS_REGION", "us-east-1")
SEGUNDOS_VENCIMIENTO = int(os.environ.get("SEGUNDOS_VENCIMIENTO", "10"))
PRESIGNED_URL_TTL = int(os.environ.get("PRESIGNED_URL_TTL", "300"))
MAX_PDF_SIZE_MB = int(os.environ.get("MAX_PDF_SIZE_MB", "10"))

# Bucket de documentos (lo inyecta Terraform como DOCS_BUCKET).
BUCKET_NAME = os.environ.get("DOCS_BUCKET") or os.environ.get("BUCKET_NAME", "accesoseguro-documentos-local")
SNS_TOPIC_ARN = os.environ.get("SNS_TOPIC_ARN", "")

# s3v4 es obligatorio para presigned URLs de objetos cifrados con KMS.
s3 = boto3.client("s3", region_name=REGION, config=Config(signature_version="s3v4"))
sns = boto3.client("sns", region_name=REGION)

app = Flask(__name__)

# El frontend vive en el bucket S3 (otro origen): el navegador exige CORS.
FRONTEND_ORIGIN = os.environ.get("CORS_ALLOWED_ORIGIN") or os.environ.get("FRONTEND_ORIGIN", "*")
CORS(
    app,
    resources={r"/api/*": {"origins": FRONTEND_ORIGIN}},
    allow_headers=["Authorization", "Content-Type"],
)


# -----------------------------------------------------------------------------
# Base de datos
# -----------------------------------------------------------------------------
def _database_url():
    if os.environ.get("DATABASE_URL"):
        return os.environ["DATABASE_URL"]
    host = os.environ.get("DB_HOST")
    if not host:
        return "sqlite:///accesoseguro.db"  # modo local
    user = quote_plus(os.environ.get("DB_USER", "postgres"))
    password = quote_plus(os.environ.get("DB_PASSWORD", ""))
    port = os.environ.get("DB_PORT", "5432")
    name = os.environ.get("DB_NAME", "accesoseguro")
    # RDS tiene rds.force_ssl = 1: la conexión tiene que ir cifrada.
    return f"postgresql+psycopg://{user}:{password}@{host}:{port}/{name}?sslmode=require"


app.config["SQLALCHEMY_DATABASE_URI"] = _database_url()
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {"pool_pre_ping": True, "pool_size": 5, "max_overflow": 5} \
    if app.config["SQLALCHEMY_DATABASE_URI"].startswith("postgresql") else {}
db = SQLAlchemy(app)


class Sede(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    nombre = db.Column(db.String(100), nullable=False)

    def to_dict(self):
        return {"id": self.id, "nombre": self.nombre}


class LogAuditoria(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    accion = db.Column(db.String(100), nullable=False)
    detalle = db.Column(db.String(300))
    usuario = db.Column(db.String(200))
    fecha = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id,
            "accion": self.accion,
            "detalle": self.detalle,
            "usuario": self.usuario,
            "fecha": self.fecha.isoformat() if self.fecha else None,
        }


class Solicitud(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    nombre_solicitante = db.Column(db.String(100), nullable=False)
    dni = db.Column(db.String(20), nullable=False)
    patente = db.Column(db.String(20))
    empresa = db.Column(db.String(100))
    motivo = db.Column(db.String(200))
    email_solicitante = db.Column(db.String(200))
    sede_id = db.Column(db.Integer, db.ForeignKey("sede.id"), nullable=False)
    estado = db.Column(db.String(20), default="pendiente")
    fecha_creacion = db.Column(db.DateTime, default=datetime.utcnow)
    fecha_aprobacion = db.Column(db.DateTime)
    hora_ingreso = db.Column(db.DateTime)
    hora_egreso = db.Column(db.DateTime)
    documento_s3_key = db.Column(db.String(300))
    motivo_decision = db.Column(db.String(300))
    notificado = db.Column(db.Boolean, default=False)

    sede = db.relationship("Sede")

    def to_dict(self, incluir_url_documento=False):
        data = {
            "id": self.id,
            "nombre_solicitante": self.nombre_solicitante,
            "dni": self.dni,
            "patente": self.patente,
            "empresa": self.empresa,
            "motivo": self.motivo,
            "sede": self.sede.to_dict() if self.sede else None,
            "estado": self.estado,
            "fecha_creacion": self.fecha_creacion.isoformat() if self.fecha_creacion else None,
            "fecha_aprobacion": self.fecha_aprobacion.isoformat() if self.fecha_aprobacion else None,
            "hora_ingreso": self.hora_ingreso.isoformat() if self.hora_ingreso else None,
            "hora_egreso": self.hora_egreso.isoformat() if self.hora_egreso else None,
            "motivo_decision": self.motivo_decision,
            "tiene_documento": bool(self.documento_s3_key),
        }
        if incluir_url_documento:
            data["url_documento"] = generar_url_documento(self.documento_s3_key)
        return data


_BASE_LISTA = False


def inicializar_base():
    """Crea las tablas y las sedes iniciales (idempotente)."""
    global _BASE_LISTA
    with app.app_context():
        try:
            db.create_all()
            if not Sede.query.first():
                for nombre in ("Planta Central", "Deposito Norte", "Deposito Sur"):
                    db.session.add(Sede(nombre=nombre))
                db.session.commit()
            _BASE_LISTA = True
        except Exception as exc:  # RDS todavía no responde u otra task la creó a la vez
            db.session.rollback()
            print(json.dumps({"level": "WARN", "msg": "inicializar_base", "error": str(exc)}))


@app.before_request
def _reintentar_inicializacion():
    # Si al arrancar la base no estaba lista, se reintenta en el primer pedido
    # a la API (el health check no la necesita).
    if not _BASE_LISTA and request.path.startswith("/api/"):
        inicializar_base()


# -----------------------------------------------------------------------------
# Autenticación (Cognito) y autorización (grupos)
# -----------------------------------------------------------------------------
COGNITO_JWKS = os.environ.get("COGNITO_JWKS", "")
COGNITO_ISSUER = os.environ.get("COGNITO_ISSUER", "")
COGNITO_CLIENT_ID = os.environ.get("COGNITO_CLIENT_ID", "")
AUTH_ENABLED = bool(COGNITO_JWKS)
_JWKS = jwt.PyJWKSet.from_json(COGNITO_JWKS) if AUTH_ENABLED else None

GRUPOS_RESPONSABLE = {"responsables_sede", "administradores"}
GRUPOS_PORTERIA = {"porteria", "administradores"}
GRUPOS_PERSONAL = GRUPOS_RESPONSABLE | GRUPOS_PORTERIA


def _validar_token(token):
    kid = jwt.get_unverified_header(token).get("kid")
    clave = next((k for k in _JWKS.keys if k.key_id == kid), None)
    if clave is None:
        raise jwt.InvalidTokenError("kid desconocido")
    claims = jwt.decode(
        token,
        clave.key,
        algorithms=["RS256"],
        audience=COGNITO_CLIENT_ID,
        issuer=COGNITO_ISSUER,
    )
    if claims.get("token_use") != "id":
        raise jwt.InvalidTokenError("se esperaba un id token")
    return claims


def requiere_login(grupos=None):
    """401 si no hay token válido; 403 si el usuario no está en los grupos."""

    def decorador(fn):
        @wraps(fn)
        def envoltura(*args, **kwargs):
            if not AUTH_ENABLED:  # modo local: sin Cognito
                g.usuario = {"email": "local@accesoseguro", "grupos": list(GRUPOS_PERSONAL), "empresa_id": None}
                return fn(*args, **kwargs)

            cabecera = request.headers.get("Authorization", "")
            if not cabecera.startswith("Bearer "):
                return jsonify({"error": "Falta el token"}), 401
            try:
                claims = _validar_token(cabecera[7:])
            except jwt.PyJWTError as exc:
                return jsonify({"error": f"Token inválido: {exc}"}), 401

            g.usuario = {
                "email": claims.get("email"),
                "grupos": claims.get("cognito:groups", []),
                "empresa_id": claims.get("custom:empresa_id"),
                "sede_id": claims.get("custom:sede_id"),
            }
            if grupos and not set(g.usuario["grupos"]) & set(grupos):
                return jsonify({"error": "No tenés permiso para esta acción"}), 403
            return fn(*args, **kwargs)

        return envoltura

    return decorador


def es_personal():
    return bool(set(g.usuario["grupos"]) & GRUPOS_PERSONAL)


# -----------------------------------------------------------------------------
# Auxiliares: S3, SNS y auditoría
# -----------------------------------------------------------------------------
def generar_url_documento(key, expiracion=PRESIGNED_URL_TTL):
    if not key:
        return None
    return s3.generate_presigned_url(
        "get_object", Params={"Bucket": BUCKET_NAME, "Key": key}, ExpiresIn=expiracion
    )


def auditar(accion, detalle):
    db.session.add(LogAuditoria(accion=accion, detalle=detalle, usuario=g.get("usuario", {}).get("email")))


def publicar_evento(solicitud, tipo, destinatario=None):
    """Publica el evento en SNS. Si falla, la columna notificado queda en
    False y vencer-permisos lo vuelve a publicar más tarde."""
    if not SNS_TOPIC_ARN:
        return
    try:
        sns.publish(
            TopicArn=SNS_TOPIC_ARN,
            Message=json.dumps({
                "tipo": tipo,
                "solicitud_id": solicitud.id,
                "estado": solicitud.estado,
                "sede": solicitud.sede.nombre if solicitud.sede else None,
                "empresa": solicitud.empresa,
                "destinatario_email": destinatario,
                "motivo": solicitud.motivo_decision,
                "fecha": datetime.utcnow().isoformat(),
            }),
        )
        solicitud.notificado = True
        db.session.commit()
    except Exception as exc:
        print(json.dumps({"level": "ERROR", "msg": "sns_publish", "error": str(exc)}))


def _slug(texto):
    return re.sub(r"[^a-z0-9-]+", "-", (texto or "sin-empresa").lower()).strip("-") or "sin-empresa"


# -----------------------------------------------------------------------------
# Health check (lo consulta el ALB)
# -----------------------------------------------------------------------------
@app.route("/")
@app.route("/health")
def health():
    return jsonify({"status": "ok", "service": "accesoseguro-api"})


# -----------------------------------------------------------------------------
# Datos comunes
# -----------------------------------------------------------------------------
@app.route("/api/sedes")
@requiere_login()
def listar_sedes():
    return jsonify([s.to_dict() for s in Sede.query.all()])


@app.route("/api/me")
@requiere_login()
def quien_soy():
    return jsonify(g.usuario)


# -----------------------------------------------------------------------------
# Transportista
# -----------------------------------------------------------------------------
@app.route("/api/solicitudes", methods=["POST"])
@requiere_login()
def crear_solicitud():
    data = request.get_json(silent=True) or request.form
    nueva = Solicitud(
        nombre_solicitante=data["nombre_solicitante"],
        dni=data["dni"],
        patente=data.get("patente"),
        empresa=data.get("empresa"),
        motivo=data.get("motivo"),
        sede_id=int(data["sede_id"]),
        email_solicitante=g.usuario.get("email"),
    )
    db.session.add(nueva)
    db.session.flush()  # para tener el id
    auditar("Nueva solicitud", f"Solicitud #{nueva.id} ({nueva.nombre_solicitante}) - sede {nueva.sede_id}")
    db.session.commit()  # la solicitud y su auditoría, en la misma transacción

    publicar_evento(nueva, "solicitud_creada")
    return jsonify(nueva.to_dict()), 201


@app.route("/api/solicitudes/<int:id>/documento", methods=["POST"])
@requiere_login()
def url_subida_documento(id):
    """Devuelve una presigned URL para que el navegador suba el PDF directo a S3."""
    solicitud = db.get_or_404(Solicitud, id)
    if not es_personal() and solicitud.email_solicitante != g.usuario.get("email"):
        return jsonify({"error": "La solicitud no es tuya"}), 403

    empresa = g.usuario.get("empresa_id") or solicitud.empresa
    key = f"empresas/{_slug(empresa)}/solicitudes/{solicitud.id}/documento-{uuid.uuid4().hex[:8]}.pdf"
    url = s3.generate_presigned_url(
        "put_object",
        Params={"Bucket": BUCKET_NAME, "Key": key, "ContentType": "application/pdf"},
        ExpiresIn=PRESIGNED_URL_TTL,
    )
    solicitud.documento_s3_key = key
    auditar("Carga de documentación", f"Solicitud #{solicitud.id} - {key}")
    db.session.commit()
    return jsonify({"url": url, "key": key, "max_mb": MAX_PDF_SIZE_MB})


@app.route("/api/mis-solicitudes")
@requiere_login()
def mis_solicitudes():
    dni = (request.args.get("dni") or "").strip()
    query = Solicitud.query
    if AUTH_ENABLED and not es_personal():
        # Un transportista solo ve las suyas (las de su usuario).
        query = query.filter_by(email_solicitante=g.usuario.get("email"))
        if dni:
            query = query.filter_by(dni=dni)
    else:
        if not dni:
            return jsonify({"error": "Falta el parámetro dni"}), 400
        query = query.filter_by(dni=dni)
    return jsonify([s.to_dict() for s in query.order_by(Solicitud.fecha_creacion.desc()).all()])


# -----------------------------------------------------------------------------
# Responsable de sede
# -----------------------------------------------------------------------------
@app.route("/api/solicitudes/pendientes")
@requiere_login(GRUPOS_RESPONSABLE)
def solicitudes_pendientes():
    pendientes = Solicitud.query.filter_by(estado="pendiente").all()
    return jsonify([s.to_dict(incluir_url_documento=True) for s in pendientes])


@app.route("/api/solicitudes/<int:id>/resolver", methods=["POST"])
@requiere_login(GRUPOS_RESPONSABLE)
def resolver_solicitud(id):
    solicitud = db.get_or_404(Solicitud, id)
    data = request.get_json(silent=True) or request.form
    accion = data.get("accion")
    motivo = data.get("motivo_decision", "")

    if accion not in ("aprobada", "rechazada"):
        return jsonify({"error": "accion inválida"}), 400

    solicitud.estado = accion
    solicitud.motivo_decision = motivo
    solicitud.notificado = False
    if accion == "aprobada":
        solicitud.fecha_aprobacion = datetime.utcnow()

    auditar(
        "Aprobación" if accion == "aprobada" else "Rechazo",
        f"Solicitud #{solicitud.id} ({solicitud.nombre_solicitante}) - {accion}"
        + (f" - Motivo: {motivo}" if motivo else ""),
    )
    db.session.commit()

    publicar_evento(solicitud, f"solicitud_{accion}", destinatario=solicitud.email_solicitante)
    return jsonify(solicitud.to_dict())


@app.route("/api/solicitudes")
@requiere_login(GRUPOS_PERSONAL)
def listar_solicitudes():
    solicitudes = Solicitud.query.order_by(Solicitud.fecha_creacion.desc()).all()
    return jsonify([s.to_dict() for s in solicitudes])


@app.route("/api/auditoria")
@requiere_login(GRUPOS_RESPONSABLE)
def ver_auditoria():
    logs = LogAuditoria.query.order_by(LogAuditoria.fecha.desc()).all()
    return jsonify([l.to_dict() for l in logs])


@app.route("/api/reporte/exportar")
@requiere_login(GRUPOS_RESPONSABLE)
def exportar_reporte():
    sede_id = request.args.get("sede_id")
    desde = request.args.get("desde")
    hasta = request.args.get("hasta")

    query = Solicitud.query
    if sede_id:
        query = query.filter_by(sede_id=sede_id)
    if desde:
        query = query.filter(Solicitud.fecha_creacion >= desde)
    if hasta:
        query = query.filter(Solicitud.fecha_creacion <= hasta + " 23:59:59")

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["ID", "Nombre", "DNI", "Patente", "Empresa", "Sede", "Estado", "Fecha solicitud", "Hora ingreso", "Hora egreso"])
    for s in query.order_by(Solicitud.fecha_creacion).all():
        writer.writerow([
            s.id, s.nombre_solicitante, s.dni, s.patente, s.empresa,
            s.sede.nombre, s.estado, s.fecha_creacion,
            s.hora_ingreso or "", s.hora_egreso or "",
        ])

    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=reporte_accesos.csv"},
    )


# -----------------------------------------------------------------------------
# Portería
# -----------------------------------------------------------------------------
@app.route("/api/verificacion")
@requiere_login(GRUPOS_PORTERIA)
def verificacion():
    busqueda = (request.args.get("busqueda") or "").strip()
    if not busqueda:
        return jsonify({"error": "Falta el parámetro busqueda"}), 400

    resultado = Solicitud.query.filter(
        Solicitud.estado == "aprobada",
        (Solicitud.dni == busqueda) | (Solicitud.patente == busqueda),
    ).first()

    if not resultado:
        return jsonify({"autorizado": False})
    return jsonify({"autorizado": True, "solicitud": resultado.to_dict()})


@app.route("/api/solicitudes/<int:id>/ingreso", methods=["POST"])
@requiere_login(GRUPOS_PORTERIA)
def registrar_ingreso(id):
    solicitud = db.get_or_404(Solicitud, id)
    solicitud.hora_ingreso = datetime.utcnow()
    auditar("Registro de ingreso", f"Solicitud #{solicitud.id} ({solicitud.nombre_solicitante})")
    db.session.commit()
    return jsonify(solicitud.to_dict())


@app.route("/api/solicitudes/<int:id>/egreso", methods=["POST"])
@requiere_login(GRUPOS_PORTERIA)
def registrar_egreso(id):
    solicitud = db.get_or_404(Solicitud, id)
    solicitud.hora_egreso = datetime.utcnow()
    auditar("Registro de egreso", f"Solicitud #{solicitud.id} ({solicitud.nombre_solicitante})")
    db.session.commit()
    return jsonify(solicitud.to_dict())


# -----------------------------------------------------------------------------
# Expiración (demo manual; en AWS la hace la Lambda vencer-permisos)
# -----------------------------------------------------------------------------
@app.route("/api/expirar-vencidas")
@requiere_login(GRUPOS_RESPONSABLE)
def expirar_vencidas():
    limite = datetime.utcnow() - timedelta(seconds=SEGUNDOS_VENCIMIENTO)
    vencidas = Solicitud.query.filter(
        Solicitud.estado == "aprobada",
        Solicitud.hora_ingreso.is_(None),
        Solicitud.fecha_aprobacion.isnot(None),
        Solicitud.fecha_aprobacion < limite,
    ).all()

    for s in vencidas:
        s.estado = "vencida"
        auditar("Expiración automática", f"Solicitud #{s.id} ({s.nombre_solicitante}) - vencida")
    db.session.commit()
    for s in vencidas:
        publicar_evento(s, "permiso_vencido", destinatario=s.email_solicitante)

    return jsonify({"vencidas": len(vencidas), "ids": [s.id for s in vencidas]})


# Se ejecuta al importar el módulo (gunicorn) y al correrlo directo.
inicializar_base()

if __name__ == "__main__":
    app.run(host="0.0.0.0", debug=True, port=int(os.environ.get("PORT", "5000")))
