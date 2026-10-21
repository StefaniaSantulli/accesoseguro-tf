#!/usr/bin/env python3
"""Activa o repara la suscripción de correo de un usuario verificado en Cognito."""
import argparse
import json
import subprocess

from notification_admin import invoke, outputs, usuario_verificado


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--usuario", required=True)
    parser.add_argument("--region", default="us-east-1")
    args = parser.parse_args()
    config = outputs()
    user, attrs = usuario_verificado(args.region, config["cognito_user_pool_id"], args.usuario)
    response = invoke(args.region, config["lambda_functions"]["gestionar-usuarios"], {
        "accion": "sincronizar_notificaciones", "username": user["Username"], "sub": attrs["sub"],
    })
    print(json.dumps(response, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        raise SystemExit(getattr(exc, "stderr", None) or str(exc))
