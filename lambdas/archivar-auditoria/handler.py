"""archivar-auditoria: copia JSON de cada evento en el bucket de auditoría.

Disparada por cola-auditoria (SNS -> SQS, raw message delivery). La auditoría
oficial está en RDS; esta copia es un respaldo barato fuera de la base.
"""
import json
import os
from datetime import datetime, timezone

import boto3

s3 = boto3.client("s3")
BUCKET = os.environ["AUDIT_BUCKET"]
PREFIX = os.environ.get("EVENT_PREFIX", "eventos/")


def lambda_handler(event, context):
    failures = []
    for record in event.get("Records", []):
        try:
            evento = json.loads(record["body"])
            now = datetime.now(timezone.utc)
            key = (
                f"{PREFIX}{now:%Y/%m/%d}/"
                f"{evento.get('tipo', 'desconocido')}-{record['messageId']}.json"
            )
            s3.put_object(
                Bucket=BUCKET,
                Key=key,
                Body=json.dumps(evento, ensure_ascii=False).encode("utf-8"),
                ContentType="application/json",
            )
        except Exception as exc:  # noqa: BLE001
            print(json.dumps({"level": "ERROR", "messageId": record.get("messageId"), "error": str(exc)}))
            failures.append({"itemIdentifier": record["messageId"]})

    # Solo vuelven a la cola los mensajes que fallaron.
    return {"batchItemFailures": failures}
