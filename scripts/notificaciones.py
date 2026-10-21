#!/usr/bin/env python3
"""Activa/repara avisos o vincula solicitudes históricas (revisión explícita)."""
import argparse
import json
import subprocess

from notification_admin import invoke, outputs


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--usuario", required=True)
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--solicitudes", type=int, nargs="+", help="IDs históricos cuya titularidad confirmó el operador")
    args = parser.parse_args()
    config = outputs()
    result = subprocess.run([
        "aws", "cognito-idp", "admin-get-user", "--user-pool-id", config["cognito_user_pool_id"],
        "--username", args.usuario, "--region", args.region, "--output", "json", "--no-cli-pager",
    ], check=True, capture_output=True, text=True)
    user = json.loads(result.stdout)
    attrs = {a["Name"]: a["Value"] for a in user["UserAttributes"]}
    if not user.get("Enabled") or attrs.get("email_verified") != "true":
        raise ValueError("Se requiere una cuenta habilitada con correo verificado")
    if args.solicitudes:
        response = invoke(args.region, config["lambda_functions"]["vencer-permisos"], {
            "accion": "vincular_solicitudes", "sub": attrs["sub"], "email": attrs["email"],
            "solicitudes": args.solicitudes,
        })
    else:
        response = invoke(args.region, config["lambda_functions"]["gestionar-usuarios"], {
            "accion": "sincronizar_notificaciones", "username": user["Username"], "sub": attrs["sub"],
        })
    print(json.dumps(response, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        raise SystemExit(getattr(exc, "stderr", None) or str(exc))
