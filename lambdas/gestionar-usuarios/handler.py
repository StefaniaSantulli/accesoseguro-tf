"""gestionar-usuarios: asigna el rol que el usuario eligió en su primer ingreso.

La invoca Flask (por el VPC Endpoint de Lambda) cuando un usuario elige su rol.
Corre fuera de la VPC porque Cognito es un servicio público y las tasks de ECS
no tienen salida a internet. Hace dos cosas:

1. Lo agrega al grupo de Cognito que eligió (transportistas o
   administradores), solo si todavía no tiene grupo: el rol se elige una vez.
2. Suscribe su mail al tópico SNS de notificaciones, con la filter policy de
   su rol, para que los avisos le lleguen solos:
   - transportistas  -> los avisos dirigidos a su mail (aprobada, rechazada...)
   - administradores -> además los dirigidos a "personal" (solicitud nueva)
   AWS le manda un mail para confirmar la suscripción.
"""
import json
import os

import boto3

cognito = boto3.client("cognito-idp")
sns = boto3.client("sns")
USER_POOL_ID = os.environ["USER_POOL_ID"]
GRUPOS_PERMITIDOS = set(os.environ.get("GRUPOS_PERMITIDOS", "").split(","))
TOPIC_ARN = os.environ.get("NOTIFICACIONES_TOPIC_ARN", "")
GRUPOS_PERSONAL = {"administradores"}


def _suscribir(email, grupo):
    destinatarios = [email.lower()] + (["personal"] if grupo in GRUPOS_PERSONAL else [])
    sns.subscribe(
        TopicArn=TOPIC_ARN,
        Protocol="email",
        Endpoint=email,
        Attributes={
            "FilterPolicyScope": "MessageAttributes",
            "FilterPolicy": json.dumps({"destinatario": destinatarios}),
        },
    )


def lambda_handler(event, context):
    username = event.get("username")
    email = event.get("email")
    grupo = event.get("grupo")

    if not username or grupo not in GRUPOS_PERMITIDOS:
        return {"ok": False, "error": "Rol no permitido"}

    actuales = cognito.admin_list_groups_for_user(UserPoolId=USER_POOL_ID, Username=username)["Groups"]
    if actuales:
        return {"ok": False, "error": "El usuario ya tiene un rol asignado"}

    cognito.admin_add_user_to_group(UserPoolId=USER_POOL_ID, Username=username, GroupName=grupo)

    suscripto = False
    if TOPIC_ARN and email:
        try:
            _suscribir(email, grupo)
            suscripto = True
        except Exception as exc:  # el rol ya quedó asignado: no se revierte por esto
            print(json.dumps({"level": "ERROR", "msg": "suscripcion_sns", "error": str(exc)}))

    print(json.dumps({"level": "INFO", "msg": "rol_asignado", "username": username, "grupo": grupo, "suscripto": suscripto}))
    return {"ok": True, "grupo": grupo, "suscripto": suscripto}
