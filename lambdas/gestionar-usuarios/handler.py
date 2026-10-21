"""Perfil confiable y alta como transportista; no permite autoasignar personal.

Flask invoca esta Lambda fuera de la VPC con el username/sub del access token
ya verificado. Cognito conserva email, sede y empresa. Solo el operador AWS
asigna porteros/responsables_sede; estos grupos no son roles IAM.
"""
import json
import os

import boto3
from botocore.config import Config

cognito = boto3.client("cognito-idp")
sns = boto3.client("sns", config=Config(connect_timeout=2, read_timeout=3, retries={"total_max_attempts": 2}))
USER_POOL_ID = os.environ["USER_POOL_ID"]
TOPIC_ARN = os.environ.get("NOTIFICACIONES_TOPIC_ARN", "")
DLQ_ARN = os.environ.get("NOTIFICACIONES_DLQ_ARN", "")


def sincronizar_notificaciones(sub, perfil, grupos):
    """Un solo administrador de suscripciones: esta función, no Terraform.

    Tanto la web como el script usan identidad/rol verificados en Cognito.
    Si la suscripción ya existe se actualiza en el mismo ARN (cambio de rol o sede).
    """
    if not TOPIC_ARN or not DLQ_ARN:
        raise ValueError("Falta configurar el tópico")
    roles = set(grupos) & {"transportistas", "porteros", "responsables_sede"}
    if len(roles) != 1:
        raise ValueError("Asignar un único rol antes de activar correos")
    recipient = f"usuario:{sub}"
    filters = [recipient]
    if "responsables_sede" in roles:
        site = int(perfil.get("sede_id") or 0)
        if site <= 0:
            raise ValueError("El responsable necesita una sede")
        filters.append(f"responsable-sede-{site}")
    email = perfil["email"].lower()
    existing = None
    for page in sns.get_paginator("list_subscriptions_by_topic").paginate(TopicArn=TOPIC_ARN):
        for item in page["Subscriptions"]:
            if item["Protocol"] != "email":
                continue
            arn = item["SubscriptionArn"]
            if not arn.startswith("arn:"):
                continue
            attrs = sns.get_subscription_attributes(SubscriptionArn=arn)["Attributes"]
            policy = json.loads(attrs.get("FilterPolicy", "{}"))
            if item["Endpoint"].lower() == email:
                existing = (arn, attrs)
            elif recipient in policy.get("destinatario_id", []):
                # El usuario cambió de correo: retirar su dirección anterior.
                sns.unsubscribe(SubscriptionArn=arn)
    policy = json.dumps({"destinatario_id": filters})
    redrive = json.dumps({"deadLetterTargetArn": DLQ_ARN})
    if existing and existing[1].get("PendingConfirmation") != "true":
        arn, attrs = existing
        if json.loads(attrs.get("FilterPolicy", "{}")) != json.loads(policy):
            sns.set_subscription_attributes(SubscriptionArn=arn, AttributeName="FilterPolicy", AttributeValue=policy)
        if attrs.get("FilterPolicyScope", "MessageAttributes") != "MessageAttributes":
            sns.set_subscription_attributes(SubscriptionArn=arn, AttributeName="FilterPolicyScope", AttributeValue="MessageAttributes")
        if json.loads(attrs.get("RedrivePolicy", "{}")) != json.loads(redrive):
            sns.set_subscription_attributes(SubscriptionArn=arn, AttributeName="RedrivePolicy", AttributeValue=redrive)
        return {"ok": True, "estado": "confirmada", "mensaje": "Correo confirmado. Los cambios de destinatarios pueden tardar hasta 15 minutos."}
    sns.subscribe(TopicArn=TOPIC_ARN, Protocol="email", Endpoint=email, ReturnSubscriptionArn=True,
                  Attributes={"FilterPolicyScope": "MessageAttributes", "FilterPolicy": policy, "RedrivePolicy": redrive})
    return {"ok": True, "estado": "pendiente", "mensaje": "Revisá tu correo y confirmá la suscripción de Amazon SNS. Después esperá hasta 15 minutos antes de probar los avisos."}


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
    if not username or not sub or accion not in {"perfil", "alta_transportista", "sincronizar_notificaciones"}:
        return {"ok": False, "error": "Operación no permitida"}
    if accion == "alta_transportista" and event.get("grupo") != "transportistas":
        return {"ok": False, "error": "Solo se permite el alta como transportista"}

    perfil = _perfil(username, sub)
    if perfil is None:
        return {"ok": False, "error": "Perfil no verificado o usuario inhabilitado"}
    actuales = []
    token = None
    while True:
        page = cognito.admin_list_groups_for_user(UserPoolId=USER_POOL_ID, Username=username,
                                                **({"NextToken": token} if token else {}))
        actuales.extend(page["Groups"])
        token = page.get("NextToken")
        if not token:
            break
    if accion == "perfil":
        return {"ok": True, "usuario": perfil, "grupos": [grupo["GroupName"] for grupo in actuales]}

    if accion == "sincronizar_notificaciones":
        try:
            return sincronizar_notificaciones(sub, perfil, [g["GroupName"] for g in actuales])
        except Exception as exc:
            print(json.dumps({"level": "ERROR", "msg": "suscripcion_sns_fallida", "error": type(exc).__name__}))
            return {"ok": False, "estado": "error", "error": "No se pudieron activar los correos. Reintentá desde el inicio o consultá al operador."}

    if actuales:
        return {"ok": False, "error": "El usuario ya tiene un grupo; consultá al operador"}
    cognito.admin_add_user_to_group(UserPoolId=USER_POOL_ID, Username=username, GroupName="transportistas")

    try:
        subscription = sincronizar_notificaciones(sub, perfil, ["transportistas"])
    except Exception as exc:
        print(json.dumps({"level": "ERROR", "msg": "suscripcion_sns_fallida", "error": type(exc).__name__}))
        subscription = {"estado": "error", "mensaje": "Rol activado. Reintentá activar los correos desde el inicio."}
    return {"ok": True, "grupo": "transportistas", "notificaciones": subscription}
