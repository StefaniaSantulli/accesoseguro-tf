"""generar-reporte: la invoca Flask (por el VPC Endpoint de Lambda) cuando un
responsable pide un reporte.

Consulta RDS por rango de fechas y sede, arma un CSV, lo guarda en el bucket
de auditoría (prefijo reportes/, se borra al día) y devuelve una presigned URL.

NOTA: la consulta a RDS está simulada (mock data). Para conectarse de verdad
hace falta un driver de PostgreSQL en una Lambda Layer.
"""
import csv
import io
import json
import os
import uuid

import boto3

s3 = boto3.client("s3")
BUCKET = os.environ["AUDIT_BUCKET"]
PREFIX = os.environ.get("REPORT_PREFIX", "reportes/")
TTL = int(os.environ.get("PRESIGNED_URL_TTL", "300"))


def _consultar_rds_mock(sede_id, desde, hasta):
    # TODO: SELECT ... FROM ingresos JOIN solicitudes ... WHERE sede_id=%s AND fecha BETWEEN %s AND %s
    return [
        {"solicitud_id": "S-0001", "empresa": "Transportes Demo", "ingreso": desde, "egreso": hasta},
    ]


def lambda_handler(event, context):
    sede_id = event.get("sede_id")
    desde = event.get("desde")
    hasta = event.get("hasta")
    if not (sede_id and desde and hasta):
        return {"ok": False, "error": "sede_id, desde y hasta son obligatorios"}

    filas = _consultar_rds_mock(sede_id, desde, hasta)

    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=["solicitud_id", "empresa", "ingreso", "egreso"])
    writer.writeheader()
    writer.writerows(filas)

    key = f"{PREFIX}{sede_id}/{desde}_{hasta}_{uuid.uuid4().hex[:8]}.csv"
    s3.put_object(Bucket=BUCKET, Key=key, Body=buffer.getvalue().encode("utf-8"), ContentType="text/csv")

    url = s3.generate_presigned_url("get_object", Params={"Bucket": BUCKET, "Key": key}, ExpiresIn=TTL)
    print(json.dumps({"level": "INFO", "msg": "reporte_generado", "key": key, "filas": len(filas)}))
    return {"ok": True, "url": url, "filas": len(filas)}
