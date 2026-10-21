"""Flask autoriza la sede; esta Lambda consulta RDS y entrega un CSV privado."""
import json
import os
import uuid

import boto3
from botocore.config import Config
from application_jobs import report_csv
from notification_jobs import database_engine

s3 = boto3.client("s3", config=Config(signature_version="s3v4"))
BUCKET = os.environ["AUDIT_BUCKET"]
PREFIX = os.environ.get("REPORT_PREFIX", "reportes/")
TTL = int(os.environ.get("PRESIGNED_URL_TTL", "300"))


def lambda_handler(event, context):
    try:
        sede_id = int(event.get("sede_id", 0))
        if sede_id <= 0:
            raise ValueError()
    except (TypeError, ValueError):
        return {"ok": False, "error": "sede_id debe ser un entero positivo"}
    engine = database_engine()
    try:
        with engine.connect() as conn:
            content, count = report_csv(conn, sede_id, event.get("desde"), event.get("hasta"))
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        engine.dispose()
    key = f"{PREFIX}{sede_id}/{uuid.uuid4().hex}.csv"
    s3.put_object(Bucket=BUCKET, Key=key, Body=content.encode("utf-8-sig"),
                  ContentType="text/csv; charset=utf-8",
                  ContentDisposition='attachment; filename="reporte_accesos.csv"')
    url = s3.generate_presigned_url("get_object", Params={"Bucket": BUCKET, "Key": key}, ExpiresIn=TTL)
    print(json.dumps({"level": "INFO", "msg": "reporte_generado", "filas": count}))
    return {"ok": True, "url": url, "filas": count}
