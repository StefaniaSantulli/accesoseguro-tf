"""Helpers compartidos por los scripts del operador; no llaman a AWS al importar."""
import json
import subprocess
import tempfile
from pathlib import Path


def outputs():
    infra = Path(__file__).resolve().parents[1] / "terraform" / "infra"
    raw = subprocess.check_output(["terraform", f"-chdir={infra}", "output", "-json"], text=True)
    return {key: item["value"] for key, item in json.loads(raw).items()}


def aws(region, *args):
    """Ejecuta la AWS CLI y devuelve la respuesta JSON (vacía si no hay salida)."""
    result = subprocess.run(
        ["aws", *args, "--region", region, "--output", "json", "--no-cli-pager"],
        check=True, capture_output=True, text=True,
    )
    return json.loads(result.stdout) if result.stdout.strip() else {}


def usuario_verificado(region, pool, usuario):
    """Usuario de Cognito habilitado y con correo verificado; si no, ValueError."""
    user = aws(region, "cognito-idp", "admin-get-user", "--user-pool-id", pool, "--username", usuario)
    attrs = {a["Name"]: a["Value"] for a in user["UserAttributes"]}
    if not user.get("Enabled") or attrs.get("email_verified") != "true":
        raise ValueError("La cuenta debe estar habilitada y tener correo verificado")
    return user, attrs


def invoke(region, function, payload):
    with tempfile.TemporaryDirectory(prefix="accesoseguro-notificaciones-") as tmp:
        source, target = Path(tmp) / "input.json", Path(tmp) / "output.json"
        source.write_text(json.dumps(payload))
        result = subprocess.run([
            "aws", "lambda", "invoke", "--function-name", function, "--region", region,
            "--payload", f"fileb://{source}", "--output", "json", "--no-cli-pager", str(target),
        ], check=True, capture_output=True, text=True)
        metadata = json.loads(result.stdout)
        response = json.loads(target.read_text())
        if metadata.get("FunctionError") or response.get("ok") is False:
            raise RuntimeError(response.get("error") or response.get("errorMessage") or "La Lambda rechazó la operación")
        return response
