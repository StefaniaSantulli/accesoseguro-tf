"""auth-callback: puente HTTPS del login.

Cognito redirige a GET /callback?code=... (API Gateway). Esta función cambia
el code por los tokens en el endpoint /oauth2/token de Cognito y devuelve al
usuario al sitio con los tokens después del "#" (el fragmento no se manda a
ningún servidor ni queda en los logs del ALB).
"""
import base64
import json
import os
import re
import urllib.parse
import urllib.request

import boto3

_secrets = boto3.client("secretsmanager")
_client_secret = None


def _get_client_secret():
    # Se cachea entre invocaciones "warm" para no leer el secreto cada vez.
    global _client_secret
    if _client_secret is None:
        resp = _secrets.get_secret_value(SecretId=os.environ["COGNITO_CLIENT_SECRET_ID"])
        _client_secret = resp["SecretString"]
    return _client_secret


def _redirect(url):
    return {"statusCode": 302, "headers": {"Location": url, "Cache-Control": "no-store"}}


def lambda_handler(event, context):
    frontend = os.environ["FRONTEND_URL"]
    params = event.get("queryStringParameters") or {}
    code = params.get("code")
    state = params.get("state", "")
    # El navegador compara este valor con el que guardó antes de iniciar login.
    if not re.fullmatch(r"[a-f0-9]{64}", state):
        return _redirect(f"{frontend}/#error=invalid_state")

    if not code:
        error = params.get("error", "missing_code")
        return _redirect(f"{frontend}/#error={urllib.parse.quote(error)}&state={state}")

    client_id = os.environ["COGNITO_CLIENT_ID"]
    basic = base64.b64encode(f"{client_id}:{_get_client_secret()}".encode()).decode()

    body = urllib.parse.urlencode({
        "grant_type": "authorization_code",
        "client_id": client_id,
        "code": code,
        "redirect_uri": os.environ["REDIRECT_URI"],
    }).encode()

    req = urllib.request.Request(
        f"{os.environ['COGNITO_DOMAIN']}/oauth2/token",
        data=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Authorization": f"Basic {basic}",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            tokens = json.loads(resp.read())
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"level": "ERROR", "msg": "token_exchange_failed", "error": str(exc)}))
        return _redirect(f"{frontend}/#error=token_exchange_failed&state={state}")

    fragment = urllib.parse.urlencode({
        "id_token": tokens["id_token"],
        "access_token": tokens["access_token"],
        "expires_in": tokens.get("expires_in", 3600),
        "state": state,
    })
    return _redirect(f"{frontend}/#{fragment}")
