"""validar-documento: revisa tipo y tamaño de cada PDF nuevo.

Disparada por cola-documentos (S3 ObjectCreated -> SQS). Lee los metadatos
del objeto (por el VPC Endpoint de S3) y actualiza el estado del documento en
RDS.

NOTA: la escritura en RDS está simulada (mock data, permitido por la
consigna). Para conectarse de verdad hace falta empaquetar un driver de
PostgreSQL (psycopg) en una Lambda Layer.
"""
import json
import os
import urllib.parse

import boto3

s3 = boto3.client("s3")
MAX_BYTES = int(os.environ.get("MAX_SIZE_MB", "10")) * 1024 * 1024


def _actualizar_estado_en_rds(key, estado, motivo=None):
    # TODO: UPDATE documentos SET estado = %s, motivo = %s WHERE s3_key = %s
    print(json.dumps({"level": "INFO", "msg": "estado_documento", "key": key, "estado": estado, "motivo": motivo}))


def lambda_handler(event, context):
    failures = []
    for record in event.get("Records", []):
        try:
            s3_event = json.loads(record["body"])
            for r in s3_event.get("Records", []):  # el primer mensaje es un s3:TestEvent
                bucket = r["s3"]["bucket"]["name"]
                key = urllib.parse.unquote_plus(r["s3"]["object"]["key"])
                head = s3.head_object(Bucket=bucket, Key=key)

                if head.get("ContentType") != "application/pdf":
                    _actualizar_estado_en_rds(key, "rechazado", "no es un PDF")
                elif head["ContentLength"] > MAX_BYTES:
                    _actualizar_estado_en_rds(key, "rechazado", "supera el tamaño máximo")
                else:
                    _actualizar_estado_en_rds(key, "valido")
        except Exception as exc:  # noqa: BLE001
            print(json.dumps({"level": "ERROR", "messageId": record.get("messageId"), "error": str(exc)}))
            failures.append({"itemIdentifier": record["messageId"]})

    return {"batchItemFailures": failures}
