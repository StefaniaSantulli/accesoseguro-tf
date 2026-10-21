"""API de AccesoSeguro (Flask) — corre en ECS Fargate detrás del ALB.

- Base de datos: RDS PostgreSQL con las variables que inyecta ECS (DB_HOST,
  DB_PORT, DB_NAME, DB_USER y DB_PASSWORD desde Secrets Manager). Sin DB_HOST
  usa SQLite local (demo y pruebas).
- Autenticación: Cognito. Flask valida el access token (firma, emisor, cliente
  y vencimiento) con las llaves públicas que Terraform le pasa en COGNITO_JWKS.
  Autorización: un único grupo de Cognito (transportistas, porteros o
  responsables_sede) y la sede asignada por el operador.
  La demostración sin Cognito requiere LOCAL_DEMO=true y no admite DB_HOST/APP_ENV.
- Documentos: el navegador sube el PDF directo a S3 con una presigned URL
  (no pasa por la API). La clave va por empresa:
  empresas/{empresa}/solicitudes/{id}/documento-{uuid}.pdf
  Cada PDF nuevo dispara S3 -> SQS -> Lambda validar-documento.
- Eventos: cada cambio importante se guarda en un outbox dentro de la misma
  transacción y se publica en SNS (fan-out a auditoría y notificaciones).
- Vencimientos: los hace la Lambda vencer-permisos (EventBridge), no la API.
"""
import json
import os
import re
import uuid
from datetime import datetime
from functools import wraps
from urllib.parse import quote_plus

import boto3
import jwt
import notifications
import application_jobs
from database_schema import check_ready, upgrade
from botocore.config import Config
from flask import Flask, Response, abort, g, jsonify, request
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy

REGION = os.environ.get("AWS_REGION", "us-east-1")
PRESIGNED_URL_TTL = int(os.environ.get("PRESIGNED_URL_TTL", "300"))
MAX_PDF_SIZE_MB = int(os.environ.get("MAX_PDF_SIZE_MB", "10"))

# Bucket de documentos (lo inyecta Terraform).
DOCS_BUCKET = os.environ.get("DOCS_BUCKET", "accesoseguro-documentos-local")
SNS_TOPIC_ARN = os.environ.get("SNS_TOPIC_ARN", "")
# Lambda fuera de la VPC: consulta el perfil y habilita el alta de transportistas.
ROLES_LAMBDA_NAME = os.environ.get("ROLES_LAMBDA_NAME", "")
REPORT_LAMBDA_NAME = os.environ.get("REPORT_LAMBDA_NAME", "")
AUTO_INIT_DB = not (os.environ.get("DB_HOST") or os.environ.get("APP_ENV")) and os.environ.get("AUTO_INIT_DB", "true").lower() == "true"

# s3v4 es obligatorio para presigned URLs de objetos cifrados con KMS.
s3 = boto3.client("s3", region_name=REGION, config=Config(signature_version="s3v4"))
sns = boto3.client("sns", region_name=REGION,
                   config=Config(connect_timeout=2, read_timeout=3, retries={"total_max_attempts": 2}))
lambda_client = boto3.client("lambda", region_name=REGION)

app = Flask(__name__)

# El frontend vive en el bucket S3 (otro origen): el navegador exige CORS.
CORS_ALLOWED_ORIGIN = os.environ.get("CORS_ALLOWED_ORIGIN", "*")
CORS(
    app,
    resources={r"/api/*": {"origins": CORS_ALLOWED_ORIGIN}},
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
# Pool de conexiones solo para PostgreSQL; SQLite (demo y pruebas) no lo admite.
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
    "pool_pre_ping": True, "pool_size": 5, "max_overflow": 5, "pool_timeout": 5,
    "connect_args": {"connect_timeout": 5},
} if app.config["SQLALCHEMY_DATABASE_URI"].startswith("postgresql") else {}
db = SQLAlchemy(app)
# Tablas compartidas con las Lambdas (outbox, dueños y validación de documentos).
for table in [*notifications.metadata.tables.values(), *application_jobs.metadata.tables.values()]:
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
        documento = estado_documento(self)
        data["documento_estado"] = documento["estado"] if documento else ("sin_validar" if self.documento_s3_key else None)
        data["documento_motivo"] = documento["motivo"] if documento else None
        if incluir_url_documento:
            data["url_documento"] = generar_url_documento(self.documento_s3_key, documento)
        return data


class AuditoriaSede(db.Model):
    """Sede de cada registro de auditoría: cada responsable consulta solo la suya."""
    log_id = db.Column(db.Integer, db.ForeignKey("log_auditoria.id"), primary_key=True)
    sede_id = db.Column(db.Integer, db.ForeignKey("sede.id"), nullable=False, index=True)


def inicializar_base():
    """Solo para desarrollo local; en AWS se ejecuta manage_db.py antes del servicio."""
    with app.app_context():
        upgrade(db.engine, db.metadata)


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
            if len(roles) > 1:
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


def datos_peticion():
    if request.is_json:
        data = request.get_json()
        if not isinstance(data, dict):
            abort(400, description="El cuerpo debe ser un objeto JSON")
        return data
    return request.form


def texto_campo(data, campo, limite, obligatorio=False):
    valor = data.get(campo, "")
    if not isinstance(valor, str):
        abort(400, description=f"{campo} debe ser texto")
    valor = valor.strip()
    if obligatorio and not valor:
        abort(400, description=f"Falta {campo}")
    if len(valor) > limite:
        abort(400, description=f"{campo} admite hasta {limite} caracteres")
    return valor


# -----------------------------------------------------------------------------
# Auxiliares: S3, SNS y auditoría
# -----------------------------------------------------------------------------
def estado_documento(solicitud):
    if not solicitud.documento_s3_key:
        return None
    docs = application_jobs.documents
    return db.session.execute(db.select(docs).where(
        docs.c.solicitud_id == solicitud.id, docs.c.s3_key == solicitud.documento_s3_key,
    )).mappings().one_or_none()


def generar_url_documento(key, documento, expiracion=PRESIGNED_URL_TTL):
    if not documento or documento["estado"] != "valido" or not documento["version_id"]:
        return None
    return s3.generate_presigned_url(
        "get_object", Params={"Bucket": DOCS_BUCKET, "Key": key, "VersionId": documento["version_id"]},
        ExpiresIn=expiracion,
    )


def auditar(accion, detalle, sede_id=None):
    # El detalle es un resumen; el motivo completo permanece en la solicitud.
    resumen = detalle if len(detalle) <= 300 else detalle[:297] + "..."
    log = LogAuditoria(accion=accion, detalle=resumen, usuario=g.get("usuario", {}).get("email"))
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
    try:
        with db.engine.connect() as conn:
            check_ready(conn)
    except Exception:
        return jsonify({"status": "unavailable", "service": "accesoseguro-api"}), 503
    return jsonify({"status": "ok", "service": "accesoseguro-api"})


@app.errorhandler(400)
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
    data = datos_peticion()
    grupo = texto_campo(data, "grupo", 30)
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
    data = datos_peticion()
    try:
        if type(data["sede_id"]) not in (str, int):
            raise ValueError()
        sede_id = int(data["sede_id"])
        if not db.session.get(Sede, sede_id):
            raise ValueError()
    except (KeyError, ValueError, TypeError):
        return jsonify({"error": "Seleccioná una sede existente"}), 400
    nueva = Solicitud(
        nombre_solicitante=texto_campo(data, "nombre_solicitante", 100, obligatorio=True),
        dni=texto_campo(data, "dni", 20, obligatorio=True),
        patente=texto_campo(data, "patente", 20),
        empresa=texto_campo(data, "empresa", 100),
        motivo=texto_campo(data, "motivo", 200),
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
    solicitud = Solicitud.query.filter_by(id=id).with_for_update().first_or_404()
    owner = db.session.execute(db.select(notifications.owners.c.usuario_sub).where(
        notifications.owners.c.solicitud_id == id)).scalar_one_or_none()
    if owner != g.usuario["sub"]:
        return jsonify({"error": "La solicitud no es tuya"}), 403

    if solicitud.estado != "pendiente":
        return jsonify({"error": "Solo se puede cambiar el documento de una solicitud pendiente"}), 409
    # Validar antes de reemplazar la referencia al documento anterior.
    tamano = datos_peticion().get("tamano")
    if type(tamano) is not int or not 0 < tamano <= MAX_PDF_SIZE_MB * 1024 * 1024:
        return jsonify({"error": f"El PDF debe tener contenido y no superar {MAX_PDF_SIZE_MB} MB"}), 400
    empresa = g.usuario.get("empresa_id") or solicitud.empresa
    key = f"empresas/{_slug(empresa)}/solicitudes/{solicitud.id}/documento-{uuid.uuid4().hex[:8]}.pdf"
    url = s3.generate_presigned_url(
        "put_object",
        Params={"Bucket": DOCS_BUCKET, "Key": key, "ContentType": "application/pdf"},
        ExpiresIn=PRESIGNED_URL_TTL,
    )
    solicitud.documento_s3_key = key
    docs = application_jobs.documents
    db.session.execute(docs.delete().where(docs.c.solicitud_id == id))
    db.session.execute(docs.insert().values(solicitud_id=id, s3_key=key, estado="pendiente",
                                            actualizado=datetime.utcnow()))
    auditar("Carga de documentación", f"Solicitud #{solicitud.id} - {key}", solicitud.sede_id)
    db.session.commit()
    return jsonify({"url": url, "key": key, "max_mb": MAX_PDF_SIZE_MB})


@app.route("/api/mis-solicitudes")
@requiere_login(GRUPOS_TRANSPORTISTA)
def mis_solicitudes():
    dni = (request.args.get("dni") or "").strip()
    # El dueño se identifica por el sub de Cognito, guardado al crear la solicitud.
    owner = notifications.owners
    query = Solicitud.query.join(owner, owner.c.solicitud_id == Solicitud.id).filter(
        owner.c.usuario_sub == g.usuario["sub"])
    if dni:
        query = query.filter(Solicitud.dni == dni)
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
    data = datos_peticion()
    accion = data.get("accion")
    motivo = texto_campo(data, "motivo_decision", 300)

    if accion not in ("aprobada", "rechazada"):
        return jsonify({"error": "accion inválida"}), 400
    if solicitud.estado != "pendiente":
        return jsonify({"error": "La solicitud ya fue resuelta"}), 409

    if accion == "aprobada" and solicitud.documento_s3_key:
        documento = estado_documento(solicitud)
        if not documento or documento["estado"] != "valido" or not documento["version_id"]:
            return jsonify({"error": "El documento debe estar validado antes de aprobar"}), 409
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
    sede_id = g.usuario["sede_id"]  # siempre la sede asignada al responsable
    desde, hasta = request.args.get("desde"), request.args.get("hasta")
    try:
        application_jobs.report_dates(desde, hasta)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    if REPORT_LAMBDA_NAME:
        try:
            response = lambda_client.invoke(
                FunctionName=REPORT_LAMBDA_NAME, InvocationType="RequestResponse",
                Payload=json.dumps({"sede_id": sede_id, "desde": desde, "hasta": hasta}).encode(),
            )
            if response.get("FunctionError"):
                raise RuntimeError("Falló generar-reporte")
            result = json.loads(response["Payload"].read())
            if not result.get("ok"):
                return jsonify({"error": result.get("error", "No se pudo generar el reporte")}), 400
            if not result.get("url", "").startswith("https://"):
                raise RuntimeError("Respuesta de reporte inválida")
            return jsonify(result)
        except Exception:
            app.logger.exception("No se pudo generar el reporte")
            return jsonify({"error": "El reporte no está disponible; reintentá"}), 503
    if os.environ.get("DB_HOST") or os.environ.get("APP_ENV"):
        return jsonify({"error": "Falta configurar la Lambda de reportes"}), 503
    try:
        content, _ = application_jobs.report_csv(db.session.connection(), sede_id, desde, hasta)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return Response(content, mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=reporte_accesos.csv"})


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
        Solicitud.hora_egreso.is_(None),
        (Solicitud.dni == busqueda) | (Solicitud.patente == busqueda),
    ).order_by(Solicitud.hora_ingreso.is_(None), Solicitud.fecha_creacion.desc(), Solicitud.id.desc()).first()

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
    if solicitud.hora_ingreso is not None or solicitud.hora_egreso is not None:
        return jsonify({"error": "Esta solicitud ya registró un ingreso"}), 409
    solicitud.hora_ingreso = datetime.utcnow()
    auditar("Registro de ingreso", f"Solicitud #{solicitud.id} ({solicitud.nombre_solicitante})", solicitud.sede_id)
    db.session.commit()
    return jsonify(solicitud.to_dict())


@app.route("/api/solicitudes/<int:id>/egreso", methods=["POST"])
@requiere_login(GRUPOS_PORTERIA)
def registrar_egreso(id):
    solicitud = solicitudes_de_sede().filter_by(id=id).with_for_update().first_or_404()
    if solicitud.hora_ingreso is None:
        return jsonify({"error": "Primero debe registrarse el ingreso"}), 409
    if solicitud.hora_egreso is not None:
        return jsonify({"error": "Esta solicitud ya registró un egreso"}), 409
    solicitud.hora_egreso = datetime.utcnow()
    auditar("Registro de egreso", f"Solicitud #{solicitud.id} ({solicitud.nombre_solicitante})", solicitud.sede_id)
    db.session.commit()
    return jsonify(solicitud.to_dict())


# AWS inicia el esquema con una tarea puntual antes de habilitar el servicio.
if AUTO_INIT_DB:
    inicializar_base()

if __name__ == "__main__":
    app.run(host="0.0.0.0", debug=True, port=int(os.environ.get("PORT", "5000")))
