#!/usr/bin/env python3
"""Asignación operativa de un único rol. Ejecutar con credenciales del Lab.

Ejemplo: python3 scripts/asignar-rol.py --usuario persona@example.com \
    --grupo porteros --sede 1
No es una API pública ni lo ejecuta el navegador.
"""
import argparse
import json
import subprocess
from pathlib import Path
from notification_admin import aws, invoke, outputs, usuario_verificado

GRUPOS = {"transportistas", "porteros", "responsables_sede"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--usuario", required=True, help="Username o email de un usuario registrado en Cognito")
    parser.add_argument("--grupo", required=True, choices=sorted(GRUPOS))
    parser.add_argument("--sede", type=int, help="ID de una sede existente; obligatorio para personal")
    parser.add_argument("--pool", help="User Pool; por defecto se lee del output de Terraform")
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--notificaciones-lambda", help="Nombre de gestionar-usuarios; por defecto output de Terraform")
    args = parser.parse_args()
    if args.grupo != "transportistas" and (args.sede is None or args.sede <= 0):
        parser.error("Porteros y responsables necesitan --sede con un ID positivo existente")
    if args.grupo == "transportistas" and args.sede is not None:
        parser.error("El transportista no tiene una sede fija")
    pool = args.pool
    if not pool:
        infra = Path(__file__).resolve().parents[1] / "terraform" / "infra"
        pool = subprocess.check_output(
            ["terraform", f"-chdir={infra}", "output", "-raw", "cognito_user_pool_id"], text=True,
        ).strip()
    common = ["--user-pool-id", pool, "--username", args.usuario]
    try:
        user, attrs = usuario_verificado(args.region, pool, args.usuario)
    except ValueError as exc:
        parser.error(str(exc))
    function = args.notificaciones_lambda or outputs()["lambda_functions"]["gestionar-usuarios"]
    # Comprobar que existe el grupo antes de retirar permisos anteriores.
    aws(args.region, "cognito-idp", "get-group", "--user-pool-id", pool, "--group-name", args.grupo)
    groups = aws(args.region, "cognito-idp", "admin-list-groups-for-user", *common)["Groups"]
    for group in groups:
        if group["GroupName"] in GRUPOS:
            aws(args.region, "cognito-idp", "admin-remove-user-from-group", *common, "--group-name", group["GroupName"])
    if args.sede is not None:
        updates = json.dumps([{"Name": "custom:sede_id", "Value": str(args.sede)}])
        aws(args.region, "cognito-idp", "admin-update-user-attributes", *common, "--user-attributes", updates)
    aws(args.region, "cognito-idp", "admin-add-user-to-group", *common, "--group-name", args.grupo)
    print(f"Asignado {args.grupo}; sede={args.sede or 'sin sede fija'}. Volver a iniciar sesión.")
    result = invoke(args.region, function, {"accion": "sincronizar_notificaciones",
                    "username": user["Username"], "sub": attrs["sub"]})
    print(result["mensaje"])


if __name__ == "__main__":
    try:
        main()
    except (subprocess.CalledProcessError, RuntimeError) as exc:
        raise SystemExit((getattr(exc, "stderr", None) or str(exc)) +
                         "\nSi el rol ya se asignó, reparar correos con scripts/notificaciones.py --usuario EMAIL.")
