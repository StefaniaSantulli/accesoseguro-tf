"""S3 -> SQS: valida cada versión de PDF y persiste el resultado en RDS."""
import json
import os
from urllib.parse import unquote_plus

import boto3
from botocore.config import Config
from application_jobs import save_document_result
from notification_jobs import database_engine

s3 = boto3.client("s3", config=Config(connect_timeout=3, read_timeout=5, retries={"total_max_attempts": 2}))
MAX_BYTES = int(os.environ.get("MAX_SIZE_MB", "10")) * 1024 * 1024
BUCKET = os.environ["DOCS_BUCKET"]


def validate_object(record):
    bucket = record["s3"]["bucket"]["name"]
    obj = record["s3"]["object"]
    key = unquote_plus(obj["key"])
    version = obj.get("versionId")
    sequence = obj["sequencer"]
    if (bucket != BUCKET or not key.startswith("empresas/") or not key.endswith(".pdf")
            or not version or version == "null"):
        raise ValueError("Objeto fuera del bucket/prefijo esperado o sin versión")
    int(sequence, 16)
    params = {"Bucket": bucket, "Key": key, "VersionId": version}
    head = s3.head_object(**params)
    size = head["ContentLength"]
    if head.get("ContentType", "").split(";", 1)[0].strip().lower() != "application/pdf":
        reason = "El tipo de archivo no es PDF"
    elif size <= 0 or size > MAX_BYTES:
        reason = "El archivo está vacío o supera el tamaño máximo"
    else:
        # Control básico del contenido real, no solamente de la etiqueta MIME.
        response = s3.get_object(**params, Range="bytes=0-4")
        try:
            header = response["Body"].read(5)
        finally:
            response["Body"].close()
        reason = None if header == b"%PDF-" else "El contenido no comienza con una cabecera PDF"
    return key, version, sequence, size, reason


def lambda_handler(event, context):
    failures = []
    engine = None
    try:
        for message in event.get("Records", []):
            try:
                body = json.loads(message["body"])
                if body.get("Event") == "s3:TestEvent":
                    continue
                for record in body["Records"]:
                    result = validate_object(record)
                    if engine is None:
                        engine = database_engine()
                    with engine.begin() as conn:
                        save_document_result(conn, *result)
            except Exception as exc:
                print(json.dumps({"level": "ERROR", "messageId": message.get("messageId"),
                                  "error": type(exc).__name__}))
                failures.append({"itemIdentifier": message["messageId"]})
    finally:
        if engine is not None:
            engine.dispose()
    return {"batchItemFailures": failures}
