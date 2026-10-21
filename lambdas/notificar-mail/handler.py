"""SQS -> RDS (control de duplicados) -> SNS email con destinatario estable."""
import json
import os
import boto3
from botocore.config import Config
import notification_jobs
import notifications

sns = boto3.client("sns", config=Config(connect_timeout=2, read_timeout=3, retries={"total_max_attempts": 2}))
TOPIC_ARN = os.environ.get("NOTIFICACIONES_TOPIC_ARN", "")
SITE_URL = os.environ.get("SITE_URL", "")


def lambda_handler(event, context):
    engine = notification_jobs.database_engine()
    failures = []
    try:
        for record in event.get("Records", []):
            try:
                payload = json.loads(record["body"])
                with engine.begin() as conn:
                    status = notifications.send_email(conn, sns, TOPIC_ARN, SITE_URL, payload)
                print(json.dumps({"level": "INFO", "msg": "aviso_" + status, "evento_id": payload["evento_id"]}))
            except Exception as exc:
                print(json.dumps({"level": "ERROR", "msg": "aviso_fallido", "messageId": record["messageId"],
                                  "error": type(exc).__name__}))
                failures.append({"itemIdentifier": record["messageId"]})
        return {"batchItemFailures": failures}
    finally:
        engine.dispose()
