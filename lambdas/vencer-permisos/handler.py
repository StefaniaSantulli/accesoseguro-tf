"""EventBridge: vencimientos reales en RDS y recuperación del outbox."""
import json
import os
import time
from datetime import datetime
import boto3
from botocore.config import Config
from sqlalchemy import func, select
import notification_jobs
import notifications

sns = boto3.client("sns", config=Config(connect_timeout=2, read_timeout=3, retries={"total_max_attempts": 2}))
TOPIC_ARN = os.environ["SNS_TOPIC_ARN"]


def lambda_handler(event, context):
    engine = notification_jobs.database_engine()
    try:
        with engine.begin() as conn:
            expired = notification_jobs.expire(conn, int(os.environ["SEGUNDOS_VENCIMIENTO"]))
        published = failed = 0
        for _ in range(100):
            if context and context.get_remaining_time_in_millis() < 15000:
                break
            with engine.begin() as conn:
                result = notifications.publish_one(conn, sns, TOPIC_ARN)
            if result is None:
                break
            published += int(result)
            failed += int(not result)
        with engine.connect() as conn:
            pending, oldest = conn.execute(select(func.count(), func.min(notifications.outbox.c.creado)).where(
                notifications.outbox.c.publicado.is_(None))).one()
        age = max(0, (datetime.utcnow() - oldest).total_seconds()) if oldest else 0
        print(json.dumps({"_aws": {"Timestamp": int(time.time() * 1000), "CloudWatchMetrics": [{
            "Namespace": "AccesoSeguro/Notificaciones", "Dimensions": [["FunctionName"]],
            "Metrics": [{"Name": "Pendientes", "Unit": "Count"}, {"Name": "EdadPendiente", "Unit": "Seconds"}],
        }]}, "FunctionName": os.environ.get("AWS_LAMBDA_FUNCTION_NAME", "local"),
            "Pendientes": pending, "EdadPendiente": age}))
        return {"vencidas": expired, "publicados": published, "fallidos": failed, "pendientes": pending}
    finally:
        engine.dispose()
