#!/usr/bin/env python3
"""Layer reproducible para Lambda Python 3.12/x86_64; no usa binarios de macOS."""
import hashlib
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "terraform/infra/.build"
REQUIREMENTS = ROOT / "lambdas/layers/notificaciones/requirements.txt"
SOURCES = [ROOT / "backend/notifications.py", ROOT / "backend/notification_jobs.py", ROOT / "backend/application_jobs.py", REQUIREMENTS]


def main():
    BUILD.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="accesoseguro-layer-") as tmp:
        target = Path(tmp) / "python"
        subprocess.run([
            sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "--no-compile",
            "--platform", "manylinux2014_x86_64", "--platform", "manylinux_2_28_x86_64",
            "--implementation", "cp", "--python-version", "3.12",
            "--abi", "cp312", "--only-binary=:all:", "--target", str(target), "-r", str(REQUIREMENTS),
        ], check=True)
        for source in SOURCES[:-1]:
            shutil.copyfile(source, target / source.name)
        # curl usa el almacén de confianza del sistema también en macOS.
        # Nunca se deshabilita la validación TLS para obtener certificados.
        subprocess.run(["curl", "--fail", "--silent", "--show-error", "--max-time", "30",
                        "--proto", "=https", "--output", str(target / "rds-ca.pem"),
                        "https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem"], check=True)
        with zipfile.ZipFile(BUILD / "notificaciones-layer.zip", "w", zipfile.ZIP_DEFLATED) as archive:
            for file in sorted(target.rglob("*")):
                if file.is_file() and "__pycache__" not in file.parts:
                    entry = zipfile.ZipInfo(file.relative_to(tmp).as_posix(), (2024, 1, 1, 0, 0, 0))
                    entry.external_attr = 0o100644 << 16
                    entry.compress_type = zipfile.ZIP_DEFLATED
                    archive.writestr(entry, file.read_bytes())
    fingerprint = hashlib.sha256("".join(hashlib.sha256(p.read_bytes()).hexdigest() for p in SOURCES).encode()).hexdigest()
    (BUILD / "notificaciones-layer.sha256").write_text(fingerprint)
    print("Layer lista:", BUILD / "notificaciones-layer.zip")


if __name__ == "__main__":
    main()
