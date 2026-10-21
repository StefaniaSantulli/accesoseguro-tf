"""Outbox y registro de publicación compartidos por Flask y dos Lambdas.

Una publicación SNS y un commit SQL no pueden ser atómicos: se prefiere
reintentar ante un resultado incierto, aunque excepcionalmente se repita el mail.
"""
import json
import re
import uuid
from datetime import datetime, timedelta

from sqlalchemy import Column, DateTime, Integer, MetaData, String, Table, Text, select, update

metadata = MetaData()
owners = Table(
    "solicitud_destinatario", metadata,
    Column("solicitud_id", Integer, primary_key=True),
    Column("usuario_sub", String(128), nullable=False, index=True),
)
outbox = Table(
    "evento_pendiente", metadata,
    Column("evento_id", String(36), primary_key=True),
    Column("solicitud_id", Integer, nullable=False, index=True),
    Column("tipo", String(40), nullable=False),
    Column("contenido", Text, nullable=False),
    Column("creado", DateTime, nullable=False),
    Column("proximo_intento", DateTime, nullable=False, index=True),
    Column("intentos", Integer, nullable=False, default=0),
    Column("ultimo_error", String(100)),
    Column("publicado", DateTime, index=True),
    Column("sns_message_id", String(100)),
    Column("mail_publicado", DateTime),
    Column("mail_message_id", String(100)),
)
TYPES = {"solicitud_creada", "solicitud_aprobada", "solicitud_rechazada", "permiso_vencido"}


def enqueue(conn, *, solicitud_id, tipo, sede_id, sede, estado, now=None):
    """Usar dentro de la MISMA transacción que cambia la solicitud y su auditoría."""
    if tipo not in TYPES or int(sede_id) <= 0:
        raise ValueError("Evento inválido")
    now = now or datetime.utcnow()
    event_id = str(uuid.uuid4())
    # El destinatario personal se resuelve por sub al publicar, nunca por correo.
    event = {"version": 1, "evento_id": event_id, "tipo": tipo,
             "solicitud_id": solicitud_id, "sede_id": int(sede_id),
             "sede": sede, "estado": estado, "fecha": now.isoformat() + "Z"}
    conn.execute(outbox.insert().values(
        evento_id=event_id, solicitud_id=solicitud_id, tipo=tipo,
        contenido=json.dumps(event), creado=now, proximo_intento=now, intentos=0,
    ))
    return event_id


def publish_one(conn, sns, topic_arn, *, event_id=None, now=None):
    """Bloquea un pendiente; solo lo marca publicado si SNS lo aceptó.

    SKIP LOCKED permite API y recuperador concurrentes sin tomar el mismo evento.
    Un error SNS se guarda con backoff; no cancela la operación del usuario.
    """
    now = now or datetime.utcnow()
    query = select(outbox).where(outbox.c.publicado.is_(None), outbox.c.proximo_intento <= now)
    if event_id:
        query = query.where(outbox.c.evento_id == event_id)
    row = conn.execute(query.order_by(outbox.c.creado, outbox.c.evento_id)
                       .limit(1).with_for_update(skip_locked=True)).mappings().first()
    if not row:
        return None
    try:
        if not topic_arn:
            raise ValueError("Falta el tópico de eventos")
        event = json.loads(row["contenido"])
        if event["tipo"] == "solicitud_creada":
            recipient = f"responsable-sede-{event['sede_id']}"
        else:
            sub = conn.execute(select(owners.c.usuario_sub).where(
                owners.c.solicitud_id == row["solicitud_id"])).scalar_one_or_none()
            if not sub:
                raise ValueError("La solicitud no tiene un dueño registrado")
            recipient = f"usuario:{sub}"
        event["destinatario_id"] = recipient
        result = sns.publish(TopicArn=topic_arn, Message=json.dumps(event))
        message_id = result["MessageId"]
    except Exception as exc:
        attempts = row["intentos"] + 1
        conn.execute(update(outbox).where(outbox.c.evento_id == row["evento_id"]).values(
            intentos=attempts, ultimo_error=type(exc).__name__,
            proximo_intento=now + timedelta(seconds=min(3600, 30 * 2 ** min(attempts, 7))),
        ))
        print(json.dumps({"level": "ERROR", "msg": "evento_pendiente", "evento_id": row["evento_id"],
                          "error": type(exc).__name__}))
        return False
    conn.execute(update(outbox).where(outbox.c.evento_id == row["evento_id"]).values(
        contenido=json.dumps(event), publicado=now, sns_message_id=message_id,
        intentos=row["intentos"] + 1, ultimo_error=None,
    ))
    return True


def subscription_matches(sns, topic_arn, recipient):
    """Solo suscripciones email confirmadas con nuestro filtro exacto.

    Una suscripción sin filtro o con otro filtro recibiría avisos ajenos: se rechaza.
    """
    found = False
    for page in sns.get_paginator("list_subscriptions_by_topic").paginate(TopicArn=topic_arn):
        for item in page["Subscriptions"]:
            arn = item["SubscriptionArn"]
            if item["Protocol"] != "email" or not arn.startswith("arn:"):
                continue
            attrs = sns.get_subscription_attributes(SubscriptionArn=arn)["Attributes"]
            if attrs.get("PendingConfirmation") == "true":
                continue
            policy = json.loads(attrs.get("FilterPolicy", "{}"))
            scope = attrs.get("FilterPolicyScope", "MessageAttributes")
            if (scope != "MessageAttributes" or set(policy) != {"destinatario_id"}
                    or not isinstance(policy["destinatario_id"], list)
                    or not policy["destinatario_id"]
                    or not all(isinstance(value, str) and re.fullmatch(
                        r"usuario:[A-Za-z0-9-]+|responsable-sede-[1-9][0-9]*", value)
                               for value in policy["destinatario_id"])):
                raise ValueError("Hay una suscripción con filtro inseguro; revisar el tópico")
            found = found or recipient in policy["destinatario_id"]
    return found


def send_email(conn, sns, topic_arn, site_url, event):
    """Publica una vez por evento registrado; los duplicados ya finalizados se omiten."""
    if not topic_arn or not site_url:
        raise ValueError("Configuración de notificaciones incompleta")
    event_id = str(uuid.UUID(event["evento_id"]))
    row = conn.execute(select(outbox).where(outbox.c.evento_id == event_id)
                       .with_for_update(nowait=True)).mappings().one()
    saved = json.loads(row["contenido"])
    if not row["publicado"] or saved != event:
        raise ValueError("El evento no coincide con el registro publicado")
    if row["mail_publicado"]:
        return "duplicado"
    recipient = saved["destinatario_id"]
    if not subscription_matches(sns, topic_arn, recipient):
        raise ValueError("No hay una suscripción confirmada para el destinatario")
    subjects = {"solicitud_creada": "Nueva solicitud de acceso", "solicitud_aprobada": "Solicitud aprobada",
                "solicitud_rechazada": "Solicitud rechazada", "permiso_vencido": "Permiso vencido"}
    subject = subjects[saved["tipo"]]
    # Sin DNI, empresa, documentos ni motivos de rechazo: el detalle exige login.
    body = (f"{subject}: solicitud #{saved['solicitud_id']}, sede {saved['sede_id']}.\n"
            f"Evento registrado: {saved['fecha']}.\n\n"
            f"Ingresá a {site_url} para consultar el estado actual y el detalle.\n"
            f"Referencia del aviso: {event_id}")
    result = sns.publish(
        TopicArn=topic_arn, Subject=f"AccesoSeguro - {subject}", Message=body,
        MessageAttributes={"destinatario_id": {"DataType": "String", "StringValue": recipient},
                           "tipo": {"DataType": "String", "StringValue": saved["tipo"]}},
    )
    conn.execute(update(outbox).where(outbox.c.evento_id == event_id).values(
        mail_publicado=datetime.utcnow(), mail_message_id=result["MessageId"],
    ))
    return "publicado"
