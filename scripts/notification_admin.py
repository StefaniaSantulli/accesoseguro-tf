"""Helpers CLI; no credenciales ni llamadas AWS al importar."""
import json
import subprocess
import tempfile
from pathlib import Path


def outputs():
    infra = Path(__file__).resolve().parents[1] / "terraform" / "infra"
    raw = subprocess.check_output(["terraform", f"-chdir={infra}", "output", "-json"], text=True)
    return {key: item["value"] for key, item in json.loads(raw).items()}


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
