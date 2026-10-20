"""Perfil confiable y alta como transportista; no permite autoasignar personal.

Flask invoca esta Lambda fuera de la VPC con el username/sub del access token
ya verificado. Cognito conserva email, sede y empresa. Solo el operador AWS
asigna porteros/responsables_sede; estos grupos no son roles IAM.
"""
import json
import os

import boto3

cognito = boto3.client("cognito-idp")
sns = boto3.client("sns")
USER_POOL_ID = os.environ["USER_POOL_ID"]
TOPIC_ARN = os.environ.get("NOTIFICACIONES_TOPIC_ARN", "")


def _perfil(username, sub):
    usuario = cognito.admin_get_user(UserPoolId=USER_POOL_ID, Username=username)
    atributos = {a["Name"]: a["Value"] for a in usuario["UserAttributes"]}
    if not usuario.get("Enabled") or atributos.get("sub") != sub:
        return None
    if atributos.get("email_verified") != "true" or not atributos.get("email"):
        return None
    return {
        "email": atributos["email"],
        "empresa_id": atributos.get("custom:empresa_id"),
        "sede_id": atributos.get("custom:sede_id"),
    }


def lambda_handler(event, context):
    accion = event.get("accion")
    username, sub = event.get("username"), event.get("sub")
    if not username or not sub or accion not in {"perfil", "alta_transportista"}:
        return {"ok": False, "error": "Operación no permitida"}
    if accion == "alta_transportista" and event.get("grupo") != "transportistas":
        return {"ok": False, "error": "Solo se permite el alta como transportista"}

    perfil = _perfil(username, sub)
    if perfil is None:
        return {"ok": False, "error": "Perfil no verificado o usuario inhabilitado"}
    actuales = cognito.admin_list_groups_for_user(UserPoolId=USER_POOL_ID, Username=username)["Groups"]
    if accion == "perfil":
        return {"ok": True, "usuario": perfil, "grupos": [grupo["GroupName"] for grupo in actuales]}

    if actuales:
        return {"ok": False, "error": "El usuario ya tiene un grupo; consultá al operador"}
    cognito.admin_add_user_to_group(UserPoolId=USER_POOL_ID, Username=username, GroupName="transportistas")

    suscripto = False
    if TOPIC_ARN:
        try:
            sns.subscribe(
                TopicArn=TOPIC_ARN, Protocol="email", Endpoint=perfil["email"],
                Attributes={"FilterPolicyScope": "MessageAttributes",
                            "FilterPolicy": json.dumps({"destinatario": [perfil["email"].lower()]})},
            )
            suscripto = True
        except Exception:
            print(json.dumps({"level": "ERROR", "msg": "suscripcion_sns_pendiente"}))
    return {"ok": True, "grupo": "transportistas", "suscripto": suscripto}
