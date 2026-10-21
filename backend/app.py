"""API de AccesoSeguro (Flask) — corre en ECS Fargate detrás del ALB.

Cambios respecto a la versión v3 local:
- Base de datos: RDS PostgreSQL con las variables que inyecta ECS (DB_HOST,
  DB_PORT, DB_NAME, DB_USER y DB_PASSWORD desde Secrets Manager). Si no hay
  DB_HOST, usa SQLite local (para probar en la compu).
- Autenticación: Cognito. Flask valida el token (firma, emisor, cliente y
  vencimiento) con las llaves públicas que Terraform le pasa en COGNITO_JWKS.
  Autorización: access token, grupos de Cognito y sede asignada por el operador.
  La demostración sin Cognito requiere LOCAL_DEMO=true y no admite DB_HOST/APP_ENV.
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
import notifications
from botocore.config import Config
from flask import Flask, Response, abort, g, jsonify, request
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy

REGION = os.environ.get("AWS_REGION", "us-east-1")
SEGUNDOS_VENCIMIENTO = int(os.environ.get("SEGUNDOS_VENCIMIENTO", "10"))
PRESIGNED_URL_TTL = int(os.environ.get("PRESIGNED_URL_TTL", "300"))
MAX_PDF_SIZE_MB = int(os.environ.get("MAX_PDF_SIZE_MB", "10"))

# Bucket de documentos (lo inyecta Terraform como DOCS_BUCKET).
BUCKET_NAME = os.environ.get("DOCS_BUCKET") or os.environ.get("BUCKET_NAME", "accesoseguro-documentos-local")
SNS_TOPIC_ARN = os.environ.get("SNS_TOPIC_ARN", "")
# Lambda fuera de la VPC: consulta el perfil y habilita el alta de transportistas.
ROLES_LAMBDA_NAME = os.environ.get("ROLES_LAMBDA_NAME", "")

# s3v4 es obligatorio para presigned URLs de objetos cifrados con KMS.
s3 = boto3.client("s3", region_name=REGION, config=Config(signature_version="s3v4"))
sns = boto3.client("sns", region_name=REGION,
                   config=Config(connect_timeout=2, read_timeout=3, retries={"total_max_attempts": 2}))
lambda_client = boto3.client("lambda", region_name=REGION)

app = Flask(__name__)

# El frontend vive en el bucket S3 (otro origen): el navegador exige CORS.
FRONTEND_ORIGIN = os.environ.get("CORS_ALLOWED_ORIGIN") or os.environ.get("FRONTEND_ORIGIN", "*")
CORS(
    app,
    resources={r"/api/*": {"origins": FRONTEND_ORIGIN}},
    allow_headers=["Authorization", "Content-Type", "X-Demo-Role", "X-Demo-Sede"],
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
# Tablas aditivas: no se borran ni reinterpretan solicitudes históricas.
for table in notifications.metadata.tables.values():
    table.to_metadata(db.metadata)


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
    # Legado: el estado de publicación ahora está en evento_pendiente, por evento.
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


class AuditoriaSede(db.Model):
    """Asocia los nuevos logs a una sede sin alterar la tabla histórica existente."""
    log_id = db.Column(db.Integer, db.ForeignKey("log_auditoria.id"), primary_key=True)
    sede_id = db.Column(db.Integer, db.ForeignKey("sede.id"), nullable=False, index=True)


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
LOCAL_DEMO = os.environ.get("LOCAL_DEMO", "").lower() == "true"
if LOCAL_DEMO and (os.environ.get("DB_HOST") or os.environ.get("APP_ENV")):
    raise RuntimeError("LOCAL_DEMO solo se permite fuera del despliegue AWS")
if not AUTH_ENABLED and not LOCAL_DEMO:
    raise RuntimeError("Falta COGNITO_JWKS; para una demo local usar LOCAL_DEMO=true")
_JWKS = jwt.PyJWKSet.from_json(COGNITO_JWKS) if AUTH_ENABLED else None

GRUPOS_TRANSPORTISTA = {"transportistas"}
GRUPOS_RESPONSABLE = {"responsables_sede"}
GRUPOS_PORTERIA = {"porteros"}
GRUPOS_PERSONAL = GRUPOS_RESPONSABLE | GRUPOS_PORTERIA
GRUPOS_APLICACION = GRUPOS_TRANSPORTISTA | GRUPOS_PERSONAL
ROLES_ELEGIBLES = GRUPOS_TRANSPORTISTA


def _validar_token(token):
    kid = jwt.get_unverified_header(token).get("kid")
    clave = next((k for k in _JWKS.keys if k.key_id == kid), None)
    if clave is None:
        raise jwt.InvalidTokenError("kid desconocido")
    claims = jwt.decode(
        token,
        clave.key,
        algorithms=["RS256"],
        issuer=COGNITO_ISSUER,
        # El access token de Cognito identifica al app client con client_id.
        options={"verify_aud": False, "require": ["exp", "iat", "sub", "username", "token_use", "client_id"]},
    )
    if claims["token_use"] != "access" or claims["client_id"] != COGNITO_CLIENT_ID:
        raise jwt.InvalidTokenError("se esperaba un access token de este cliente")
    return claims


def gestionar_usuario(accion, **datos):
    """La identidad procede del token verificado, nunca del JSON del navegador."""
    try:
        respuesta = lambda_client.invoke(
            FunctionName=ROLES_LAMBDA_NAME,
            Payload=json.dumps({"accion": accion, **datos}).encode(),
        )
        if respuesta.get("FunctionError"):
            raise ValueError("Error de Lambda")
        resultado = json.loads(respuesta["Payload"].read())
        if not isinstance(resultado, dict):
            raise ValueError("Respuesta inválida")
        return resultado
    except Exception:
        app.logger.exception("No se pudo consultar la identidad en Cognito")
        abort(503, description="No se pudo consultar el perfil de usuario")


def requiere_login(grupos=None):
    """401 si no hay token válido; 403 si el usuario no está en los grupos."""

    def decorador(fn):
        @wraps(fn)
        def envoltura(*args, **kwargs):
            if not AUTH_ENABLED:  # solo demostración local explícita
                rol = request.headers.get("X-Demo-Role", "")
                g.usuario = {"sub": "local", "email": "local@accesoseguro", "username": "local",
                             "grupos": [rol] if rol in GRUPOS_APLICACION else [], "empresa_id": None,
                             "sede_id": request.headers.get("X-Demo-Sede", "1")}
            else:
                cabecera = request.headers.get("Authorization", "")
                if not cabecera.startswith("Bearer "):
                    return jsonify({"error": "Falta el access token"}), 401
                try:
                    claims = _validar_token(cabecera[7:])
                except (jwt.PyJWTError, ValueError, TypeError):
                    return jsonify({"error": "Access token inválido"}), 401
                perfil = gestionar_usuario("perfil", username=claims["username"], sub=claims["sub"])
                if not perfil.get("ok"):
                    return jsonify({"error": "Usuario inhabilitado o perfil no verificado"}), 403
                grupos_token = set(claims.get("cognito:groups", []))
                grupos_actuales = set(perfil.get("grupos", []))
                if grupos_token & GRUPOS_APLICACION != grupos_actuales & GRUPOS_APLICACION:
                    return jsonify({"error": "Tu rol cambió; volvé a iniciar sesión"}), 403
                g.usuario = {**perfil["usuario"], "username": claims["username"], "sub": claims["sub"],
                             "grupos": claims.get("cognito:groups", [])}
            roles = set(g.usuario["grupos"]) & GRUPOS_APLICACION
            if len(roles) > 1 or "administradores" in g.usuario["grupos"]:
                return jsonify({"error": "El operador debe asignarte un único rol de la aplicación"}), 403
            if grupos and not set(g.usuario["grupos"]) & set(grupos):
                return jsonify({"error": "No tenés permiso para esta acción"}), 403
            if roles & GRUPOS_PERSONAL:
                try:
                    g.usuario["sede_id"] = int(g.usuario.get("sede_id"))
                except (ValueError, TypeError):
                    return jsonify({"error": "El operador debe asignarte una sede"}), 403
                if not db.session.get(Sede, g.usuario["sede_id"]):
                    return jsonify({"error": "La sede asignada no existe"}), 403
            return fn(*args, **kwargs)

        return envoltura

    return decorador


def es_personal():
    return bool(set(g.usuario["grupos"]) & GRUPOS_PERSONAL)


def solicitudes_de_sede():
    return Solicitud.query.filter_by(sede_id=g.usuario["sede_id"])


def solicitud_de_sede(id):
    solicitud = solicitudes_de_sede().filter_by(id=id).first()
    if solicitud is None:
        abort(404)
    return solicitud


# -----------------------------------------------------------------------------
# Auxiliares: S3, SNS y auditoría
# -----------------------------------------------------------------------------
def generar_url_documento(key, expiracion=PRESIGNED_URL_TTL):
    if not key:
        return None
    return s3.generate_presigned_url(
        "get_object", Params={"Bucket": BUCKET_NAME, "Key": key}, ExpiresIn=expiracion
    )


def auditar(accion, detalle, sede_id=None):
    log = LogAuditoria(accion=accion, detalle=detalle, usuario=g.get("usuario", {}).get("email"))
    db.session.add(log)
    if sede_id is not None:
        db.session.flush()
        db.session.add(AuditoriaSede(log_id=log.id, sede_id=sede_id))


def guardar_evento(solicitud, tipo):
    return notifications.enqueue(
        db.session, solicitud_id=solicitud.id, tipo=tipo,
        sede_id=solicitud.sede_id, sede=solicitud.sede.nombre, estado=solicitud.estado,
    )


def publicar_evento(event_id):
    """Intento inmediato después del commit; si falla, el recuperador lo retoma."""
    try:
        with db.engine.begin() as conn:
            notifications.publish_one(conn, sns, SNS_TOPIC_ARN, event_id=event_id)
    except Exception:
        app.logger.exception("El aviso %s permanece pendiente", event_id)


def _slug(texto):
    return re.sub(r"[^a-z0-9-]+", "-", (texto or "sin-empresa").lower()).strip("-") or "sin-empresa"


# -----------------------------------------------------------------------------
# Health check (lo consulta el ALB)
# -----------------------------------------------------------------------------
@app.route("/")
@app.route("/health")
def health():
    return jsonify({"status": "ok", "service": "accesoseguro-api"})


@app.errorhandler(403)
@app.errorhandler(404)
@app.errorhandler(503)
def error_api(error):
    return jsonify({"error": error.description}), error.code


# -----------------------------------------------------------------------------
# Datos comunes
# -----------------------------------------------------------------------------
@app.route("/api/sedes")
@requiere_login()
def listar_sedes():
    query = Sede.query
    if es_personal():
        query = query.filter_by(id=g.usuario["sede_id"])
    return jsonify([s.to_dict() for s in query.all()])


@app.route("/api/me")
@requiere_login()
def quien_soy():
    return jsonify(g.usuario)


@app.route("/api/notificaciones/suscripcion", methods=["POST"])
@requiere_login(GRUPOS_APLICACION)
def activar_correos():
    if not AUTH_ENABLED:
        return jsonify({"ok": True, "estado": "demo", "mensaje": "La demo local no envía correos."})
    result = gestionar_usuario("sincronizar_notificaciones", username=g.usuario["username"], sub=g.usuario["sub"])
    return jsonify(result), 200 if result.get("ok") else 503


@app.route("/api/mi-rol", methods=["POST"])
@requiere_login()
def elegir_rol():
    """Solo permite el alta como transportista. El personal lo asigna el operador."""
    data = request.get_json(silent=True) or {}
    grupo = data.get("grupo")
    if grupo not in ROLES_ELEGIBLES:
        return jsonify({"error": "Rol inválido"}), 400
    if g.usuario["grupos"]:
        return jsonify({"error": "Ya tenés un rol asignado"}), 409
    if not AUTH_ENABLED:  # modo local: no hay Cognito
        return jsonify({"ok": True, "grupo": grupo})

    resultado = gestionar_usuario("alta_transportista", username=g.usuario["username"], sub=g.usuario["sub"], grupo=grupo)
    if not resultado.get("ok"):
        return jsonify({"error": resultado.get("error", "No se pudo asignar el rol")}), 409

    auditar("Elección de rol", f"{g.usuario.get('email')} eligió {grupo}")
    db.session.commit()
    return jsonify({"ok": True, "grupo": grupo})


# -----------------------------------------------------------------------------
# Transportista
# -----------------------------------------------------------------------------
@app.route("/api/solicitudes", methods=["POST"])
@requiere_login(GRUPOS_TRANSPORTISTA)
def crear_solicitud():
    data = request.get_json(silent=True) or request.form
    try:
        sede_id = int(data["sede_id"])
        if not db.session.get(Sede, sede_id):
            raise ValueError()
    except (KeyError, ValueError, TypeError):
        return jsonify({"error": "Seleccioná una sede existente"}), 400
    nueva = Solicitud(
        nombre_solicitante=data["nombre_solicitante"],
        dni=data["dni"],
        patente=data.get("patente"),
        empresa=data.get("empresa"),
        motivo=data.get("motivo"),
        sede_id=sede_id,
        email_solicitante=g.usuario.get("email"),
    )
    db.session.add(nueva)
    db.session.flush()  # para tener el id
    db.session.execute(notifications.owners.insert().values(solicitud_id=nueva.id, usuario_sub=g.usuario["sub"]))
    auditar("Nueva solicitud", f"Solicitud #{nueva.id} ({nueva.nombre_solicitante}) - sede {nueva.sede_id}", nueva.sede_id)
    event_id = guardar_evento(nueva, "solicitud_creada")
    db.session.commit()  # solicitud, auditoría y aviso pendiente: todo o nada

    publicar_evento(event_id)
    return jsonify(nueva.to_dict()), 201


@app.route("/api/solicitudes/<int:id>/documento", methods=["POST"])
@requiere_login(GRUPOS_TRANSPORTISTA)
def url_subida_documento(id):
    """Devuelve una presigned URL para que el navegador suba el PDF directo a S3."""
    solicitud = db.get_or_404(Solicitud, id)
    owner = db.session.execute(db.select(notifications.owners.c.usuario_sub).where(
        notifications.owners.c.solicitud_id == id)).scalar_one_or_none()
    if (owner != g.usuario["sub"] if owner else solicitud.email_solicitante != g.usuario.get("email")):
        return jsonify({"error": "La solicitud no es tuya"}), 403

    empresa = g.usuario.get("empresa_id") or solicitud.empresa
    key = f"empresas/{_slug(empresa)}/solicitudes/{solicitud.id}/documento-{uuid.uuid4().hex[:8]}.pdf"
    url = s3.generate_presigned_url(
        "put_object",
        Params={"Bucket": BUCKET_NAME, "Key": key, "ContentType": "application/pdf"},
        ExpiresIn=PRESIGNED_URL_TTL,
    )
    solicitud.documento_s3_key = key
    auditar("Carga de documentación", f"Solicitud #{solicitud.id} - {key}", solicitud.sede_id)
    db.session.commit()
    return jsonify({"url": url, "key": key, "max_mb": MAX_PDF_SIZE_MB})


@app.route("/api/mis-solicitudes")
@requiere_login(GRUPOS_TRANSPORTISTA)
def mis_solicitudes():
    dni = (request.args.get("dni") or "").strip()
    # Las solicitudes nuevas usan sub. Las antiguas conservan su acceso anterior
    # hasta que el operador confirme la vinculación (ver guía de migración).
    owner = notifications.owners
    query = Solicitud.query.outerjoin(owner, owner.c.solicitud_id == Solicitud.id).filter(
        (owner.c.usuario_sub == g.usuario["sub"]) |
        (owner.c.usuario_sub.is_(None) & (Solicitud.email_solicitante == g.usuario["email"]))
    )
    if dni:
        query = query.filter_by(dni=dni)
    return jsonify([s.to_dict() for s in query.order_by(Solicitud.fecha_creacion.desc()).all()])


# -----------------------------------------------------------------------------
# Responsable de sede
# -----------------------------------------------------------------------------
@app.route("/api/solicitudes/pendientes")
@requiere_login(GRUPOS_RESPONSABLE)
def solicitudes_pendientes():
    pendientes = solicitudes_de_sede().filter_by(estado="pendiente").all()
    return jsonify([s.to_dict(incluir_url_documento=True) for s in pendientes])


@app.route("/api/solicitudes/<int:id>/resolver", methods=["POST"])
@requiere_login(GRUPOS_RESPONSABLE)
def resolver_solicitud(id):
    solicitud = solicitudes_de_sede().filter_by(id=id).with_for_update().first_or_404()
    data = request.get_json(silent=True) or request.form
    accion = data.get("accion")
    motivo = data.get("motivo_decision", "")

    if accion not in ("aprobada", "rechazada"):
        return jsonify({"error": "accion inválida"}), 400
    if solicitud.estado != "pendiente":
        return jsonify({"error": "La solicitud ya fue resuelta"}), 409

    solicitud.estado = accion
    solicitud.motivo_decision = motivo
    if accion == "aprobada":
        solicitud.fecha_aprobacion = datetime.utcnow()

    auditar(
        "Aprobación" if accion == "aprobada" else "Rechazo",
        f"Solicitud #{solicitud.id} ({solicitud.nombre_solicitante}) - {accion}"
        + (f" - Motivo: {motivo}" if motivo else ""), solicitud.sede_id,
    )
    event_id = guardar_evento(solicitud, f"solicitud_{accion}")
    db.session.commit()
    publicar_evento(event_id)
    return jsonify(solicitud.to_dict())


@app.route("/api/solicitudes")
@requiere_login(GRUPOS_PERSONAL)
def listar_solicitudes():
    solicitudes = solicitudes_de_sede().order_by(Solicitud.fecha_creacion.desc()).all()
    return jsonify([s.to_dict() for s in solicitudes])


@app.route("/api/auditoria")
@requiere_login(GRUPOS_RESPONSABLE)
def ver_auditoria():
    logs = LogAuditoria.query.join(AuditoriaSede, AuditoriaSede.log_id == LogAuditoria.id).filter(
        AuditoriaSede.sede_id == g.usuario["sede_id"]
    ).order_by(LogAuditoria.fecha.desc()).all()
    return jsonify([l.to_dict() for l in logs])


@app.route("/api/reporte/exportar")
@requiere_login(GRUPOS_RESPONSABLE)
def exportar_reporte():
    sede_id = request.args.get("sede_id")
    desde = request.args.get("desde")
    hasta = request.args.get("hasta")

    # Las fechas llegan como texto (AAAA-MM-DD). Se convierten a datetime antes
    # de comparar: PostgreSQL no compara una columna timestamp contra texto.
    try:
        fecha_desde = datetime.strptime(desde, "%Y-%m-%d") if desde else None
        fecha_hasta = datetime.strptime(hasta, "%Y-%m-%d") + timedelta(days=1) if hasta else None
    except ValueError:
        return jsonify({"error": "Las fechas tienen que tener el formato AAAA-MM-DD"}), 400

    query = solicitudes_de_sede()
    if sede_id:
        if sede_id != str(g.usuario["sede_id"]):
            return jsonify({"error": "No tenés permiso para esa sede"}), 403
    if fecha_desde:
        query = query.filter(Solicitud.fecha_creacion >= fecha_desde)
    if fecha_hasta:
        query = query.filter(Solicitud.fecha_creacion < fecha_hasta)  # incluye todo el día "hasta"

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

    resultado = solicitudes_de_sede().filter(
        Solicitud.estado == "aprobada",
        (Solicitud.dni == busqueda) | (Solicitud.patente == busqueda),
    ).first()

    if not resultado:
        return jsonify({"autorizado": False})
    return jsonify({"autorizado": True, "solicitud": resultado.to_dict()})


@app.route("/api/solicitudes/<int:id>/ingreso", methods=["POST"])
@requiere_login(GRUPOS_PORTERIA)
def registrar_ingreso(id):
    # Mismo bloqueo que el worker: no vencer una solicitud mientras se registra ingreso.
    solicitud = solicitudes_de_sede().filter_by(id=id).with_for_update().first_or_404()
    if solicitud.estado != "aprobada":
        return jsonify({"error": "La solicitud no está aprobada"}), 409
    solicitud.hora_ingreso = datetime.utcnow()
    auditar("Registro de ingreso", f"Solicitud #{solicitud.id} ({solicitud.nombre_solicitante})", solicitud.sede_id)
    db.session.commit()
    return jsonify(solicitud.to_dict())


@app.route("/api/solicitudes/<int:id>/egreso", methods=["POST"])
@requiere_login(GRUPOS_PORTERIA)
def registrar_egreso(id):
    solicitud = solicitud_de_sede(id)
    if solicitud.hora_ingreso is None:
        return jsonify({"error": "Primero debe registrarse el ingreso"}), 409
    solicitud.hora_egreso = datetime.utcnow()
    auditar("Registro de egreso", f"Solicitud #{solicitud.id} ({solicitud.nombre_solicitante})", solicitud.sede_id)
    db.session.commit()
    return jsonify(solicitud.to_dict())


# -----------------------------------------------------------------------------
# Expiración (demo manual; en AWS la hace la Lambda vencer-permisos)
# -----------------------------------------------------------------------------
@app.route("/api/expirar-vencidas")
@requiere_login(GRUPOS_RESPONSABLE)
def expirar_vencidas():
    limite = datetime.utcnow() - timedelta(seconds=SEGUNDOS_VENCIMIENTO)
    vencidas = solicitudes_de_sede().filter(
        Solicitud.estado == "aprobada",
        Solicitud.hora_ingreso.is_(None),
        Solicitud.fecha_aprobacion.isnot(None),
        Solicitud.fecha_aprobacion < limite,
    ).with_for_update(skip_locked=True).all()

    eventos = []
    for s in vencidas:
        s.estado = "vencida"
        auditar("Expiración automática", f"Solicitud #{s.id} ({s.nombre_solicitante}) - vencida", s.sede_id)
        eventos.append(guardar_evento(s, "permiso_vencido"))
    db.session.commit()
    for event_id in eventos:
        publicar_evento(event_id)

    return jsonify({"vencidas": len(vencidas), "ids": [s.id for s in vencidas]})


# Se ejecuta al importar el módulo (gunicorn) y al correrlo directo.
inicializar_base()

if __name__ == "__main__":
    app.run(host="0.0.0.0", debug=True, port=int(os.environ.get("PORT", "5000")))
