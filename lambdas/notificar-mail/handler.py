"""notificar-mail: arma el mail según el evento y lo manda con SES.

Disparada por cola-notificaciones. El evento trae el destinatario:
- solicitud_creada            -> responsable de la sede
- solicitud_aprobada/rechazada/vencida -> transportista
"""
import json
import os

import boto3

ses = boto3.client("ses")
SENDER = os.environ.get("SES_SENDER", "")
SITE_URL = os.environ.get("SITE_URL", "")

PLANTILLAS = {
    "solicitud_creada": ("Nueva solicitud de acceso", "Hay una solicitud nueva para tu sede: {solicitud_id}."),
    "solicitud_aprobada": ("Tu solicitud fue aprobada", "La solicitud {solicitud_id} fue aprobada."),
    "solicitud_rechazada": ("Tu solicitud fue rechazada", "La solicitud {solicitud_id} fue rechazada. Motivo: {motivo}."),
    "permiso_vencido": ("Tu permiso venció", "El permiso de la solicitud {solicitud_id} venció."),
}


def lambda_handler(event, context):
    failures = []
    for record in event.get("Records", []):
        try:
            evento = json.loads(record["body"])
            tipo = evento.get("tipo")
            destinatario = evento.get("destinatario_email")

            if tipo not in PLANTILLAS or not destinatario:
                print(json.dumps({"level": "INFO", "msg": "evento_sin_mail", "tipo": tipo}))
                continue
            if not SENDER:
                print(json.dumps({"level": "WARN", "msg": "SES_SENDER vacío, no se envía"}))
                continue

            asunto, cuerpo = PLANTILLAS[tipo]
            texto = cuerpo.format(
                solicitud_id=evento.get("solicitud_id", "-"),
                motivo=evento.get("motivo", "-"),
            ) + f"\n\nIngresá a {SITE_URL} para ver el detalle."

            ses.send_email(
                Source=SENDER,
                Destination={"ToAddresses": [destinatario]},
                Message={
                    "Subject": {"Data": f"AccesoSeguro - {asunto}", "Charset": "UTF-8"},
                    "Body": {"Text": {"Data": texto, "Charset": "UTF-8"}},
                },
            )
        except Exception as exc:  # noqa: BLE001
            print(json.dumps({"level": "ERROR", "messageId": record.get("messageId"), "error": str(exc)}))
            failures.append({"itemIdentifier": record["messageId"]})

    return {"batchItemFailures": failures}
