"""vencer-permisos: corre cada 15 minutos (EventBridge).

1. Marca como vencidos los permisos cuya vigencia terminó y graba la
   auditoría en la misma transacción.
2. Publica en SNS un evento por cada permiso vencido.
3. Republica los eventos con notificado = false (mitigación de eventos
   perdidos si una task se cayó entre el commit y la publicación).

NOTA: la consulta a RDS está simulada (mock data). Para conectarse de verdad
hace falta un driver de PostgreSQL en una Lambda Layer.
"""
import json
import os
from datetime import datetime, timezone

import boto3

sns = boto3.client("sns")
TOPIC_ARN = os.environ["SNS_TOPIC_ARN"]


def _permisos_vencidos_mock():
    # TODO: UPDATE vigencias SET estado='vencido' WHERE hasta < now() AND estado='vigente' RETURNING ...
    return []


def _eventos_pendientes_mock():
    # TODO: SELECT ... FROM solicitudes WHERE notificado = false
    return []


def lambda_handler(event, context):
    ahora = datetime.now(timezone.utc).isoformat()
    publicados = 0

    for permiso in _permisos_vencidos_mock() + _eventos_pendientes_mock():
        sns.publish(
            TopicArn=TOPIC_ARN,
            Message=json.dumps({"tipo": "permiso_vencido", "fecha": ahora, **permiso}),
        )
        publicados += 1

    print(json.dumps({"level": "INFO", "msg": "vencer_permisos_ok", "publicados": publicados}))
    return {"publicados": publicados}
