"""notificar-mail: arma el aviso según el evento y lo manda por mail con SNS.

Disparada por cola-notificaciones (SNS eventos-solicitud -> SQS).

Antes usaba SES, pero en el Learner Lab el LabRole no puede verificar
identidades de SES (ses:VerifyEmailIdentity) y SES en sandbox solo manda a
direcciones verificadas. Ahora publica en el tópico SNS "notificaciones-mail",
al que cada persona se suscribe por mail. El atributo "destinatario" y la
filter policy de cada suscripción deciden quién recibe cada aviso:
- solicitud_creada                     -> "responsable-sede-<id>" (responsables de esa sede)
- solicitud_aprobada/rechazada/vencida -> el mail del transportista
"""
import json
import os

import boto3

sns = boto3.client("sns")
TOPIC_ARN = os.environ.get("NOTIFICACIONES_TOPIC_ARN", "")
SITE_URL = os.environ.get("SITE_URL", "")

PLANTILLAS = {
    "solicitud_creada": ("Nueva solicitud de acceso", "Hay una solicitud nueva para la sede {sede}: #{solicitud_id} ({empresa})."),
    "solicitud_aprobada": ("Tu solicitud fue aprobada", "La solicitud #{solicitud_id} para la sede {sede} fue aprobada."),
    "solicitud_rechazada": ("Tu solicitud fue rechazada", "La solicitud #{solicitud_id} fue rechazada. Motivo: {motivo}."),
    "permiso_vencido": ("Tu permiso venció", "El permiso de la solicitud #{solicitud_id} venció."),
}


def lambda_handler(event, context):
    failures = []
    for record in event.get("Records", []):
        try:
            evento = json.loads(record["body"])
            tipo = evento.get("tipo")

            if tipo not in PLANTILLAS:
                print(json.dumps({"level": "INFO", "msg": "evento_sin_aviso", "tipo": tipo}))
                continue
            if not TOPIC_ARN:
                print(json.dumps({"level": "WARN", "msg": "NOTIFICACIONES_TOPIC_ARN vacío, no se envía"}))
                continue

            # No enviar avisos de una sede a responsables de otras sedes.
            if tipo == "solicitud_creada":
                sede_id = int(evento["sede_id"])
                if sede_id <= 0:
                    raise ValueError("sede_id inválido")
                destinatario = f"responsable-sede-{sede_id}"
            else:
                destinatario = (evento.get("destinatario_email") or "").strip().lower()
                if not destinatario:
                    raise ValueError("El evento no identifica al destinatario")

            asunto, cuerpo = PLANTILLAS[tipo]
            texto = cuerpo.format(
                solicitud_id=evento.get("solicitud_id", "-"),
                sede=evento.get("sede") or "-",
                empresa=evento.get("empresa") or "-",
                motivo=evento.get("motivo") or "-",
            ) + f"\n\nIngresá a {SITE_URL} para ver el detalle."

            sns.publish(
                TopicArn=TOPIC_ARN,
                Subject=f"AccesoSeguro - {asunto}"[:100],  # SNS acepta hasta 100 caracteres
                Message=texto,
                MessageAttributes={
                    "destinatario": {"DataType": "String", "StringValue": destinatario},
                    "tipo": {"DataType": "String", "StringValue": tipo},
                },
            )
            print(json.dumps({"level": "INFO", "msg": "aviso_publicado", "tipo": tipo, "destinatario": destinatario}))
        except Exception as exc:  # noqa: BLE001
            print(json.dumps({"level": "ERROR", "messageId": record.get("messageId"), "error": str(exc)}))
            failures.append({"itemIdentifier": record["messageId"]})

    # Solo vuelven a la cola los mensajes que fallaron.
    return {"batchItemFailures": failures}
