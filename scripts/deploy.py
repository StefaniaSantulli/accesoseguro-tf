#!/usr/bin/env python3
"""Despliegue completo del Lab. Requiere --apply; no modifica la arquitectura."""
import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
REGION = "us-east-1"


def run(args, *, capture=False, input_text=None, check=True):
    # No imprimir entrada/salida de login, secretos, outputs completos ni state.
    result = subprocess.run(args, cwd=ROOT, text=True, input=input_text,
                            stdout=subprocess.PIPE if capture else None,
                            stderr=subprocess.PIPE if capture else None, check=False)
    if check and result.returncode:
        raise RuntimeError(f"Falló {args[0]} {args[1]} (código {result.returncode}); revisar el paso y CloudWatch")
    return result


def aws(*args, check=True):
    return run(["aws", *args, "--region", REGION, "--output", "json", "--no-cli-pager"],
               capture=True, check=check)


def aws_json(*args):
    output = aws(*args).stdout
    return json.loads(output) if output.strip() else {}


def tf(stack, *args, capture=False):
    return run(["terraform", f"-chdir=terraform/{stack}", *args], capture=capture)


def outputs():
    return {k: v["value"] for k, v in json.loads(tf("infra", "output", "-json", capture=True).stdout).items()}


def bucket_exists(bucket, account):
    response = aws("s3api", "head-bucket", "--bucket", bucket, "--expected-bucket-owner", account, check=False)
    if response.returncode == 0:
        return True
    if "(404)" in response.stderr or "(NoSuchBucket)" in response.stderr:
        return False
    raise RuntimeError("No se pudo verificar el bucket de state: revisar credenciales, propietario y permisos")


def bootstrap(bucket, account):
    """Primer apply local; inmediatamente migra el state al mismo S3 con lock."""
    directory = ROOT / "terraform/bootstrap"
    override = directory / "backend_override.tf"
    config = [f"-backend-config=bucket={bucket}", "-backend-config=key=accesoseguro/dev/bootstrap.tfstate",
              f"-backend-config=region={REGION}", "-backend-config=encrypt=true", "-backend-config=use_lockfile=true"]

    def remote_init(migrate):
        override.write_text('terraform {\n  backend "s3" {}\n}\n')
        mode = ["-migrate-state", "-force-copy"] if migrate else ["-reconfigure"]
        tf("bootstrap", "init", "-input=false", *mode, *config)

    exists = bucket_exists(bucket, account)
    if exists:
        local_state = directory / "terraform.tfstate"
        remote = aws("s3api", "head-object", "--bucket", bucket,
                     "--key", "accesoseguro/dev/bootstrap.tfstate", "--expected-bucket-owner", account, check=False)
        if remote.returncode and "(404)" not in remote.stderr and "(NoSuchKey)" not in remote.stderr:
            raise RuntimeError("No se pudo comprobar el state remoto de bootstrap")
        # Si ya hay state remoto es la autoridad: no pisarlo con un respaldo local viejo.
        remote_init(local_state.exists() and remote.returncode != 0)
    else:
        if override.exists():
            raise RuntimeError("El backend previo apunta a un bucket que ya no existe; revisar el state antes de continuar")
        tf("bootstrap", "init", "-input=false")
    try:
        tf("bootstrap", "validate")
        tf("bootstrap", "plan", "-input=false", "-lock-timeout=120s", "-out=bootstrap.tfplan",
           f"-var=state_bucket_name={bucket}", "-var=project_name=accesoseguro", "-var=environment=dev",
           "-var=region=us-east-1", "-var=generate_backend_file=true")
        tf("bootstrap", "apply", "-input=false", "bootstrap.tfplan")
    finally:
        # Incluso un apply parcial conserva los recursos creados si ya existe el bucket.
        if not exists and (directory / "terraform.tfstate").exists() and bucket_exists(bucket, account):
            remote_init(True)


def apply_phase(tag, ready):
    plan = "ready.tfplan" if ready else "prepare.tfplan"
    tf("infra", "plan", "-input=false", "-lock-timeout=120s", f"-out={plan}",
       f"-var=api_image_tag={tag}", f"-var=deployment_ready={str(ready).lower()}",
       "-var=project_name=accesoseguro", "-var=environment=dev", "-var=region=us-east-1")
    tf("infra", "apply", "-input=false", plan)


def pause_existing(out):
    if not out.get("ecs_service_name"):
        return
    cluster, service = out["ecs_cluster_name"], out["ecs_service_name"]
    # Un apply parcial puede haber creado ECS sin llegar a crear su Auto Scaling.
    resource_id = f"service/{cluster}/{service}"
    targets = aws_json("application-autoscaling", "describe-scalable-targets", "--service-namespace", "ecs",
                       "--resource-ids", resource_id, "--scalable-dimension", "ecs:service:DesiredCount")
    if targets["ScalableTargets"]:
        aws_json("application-autoscaling", "register-scalable-target", "--service-namespace", "ecs",
                 "--resource-id", resource_id, "--scalable-dimension", "ecs:service:DesiredCount",
                 "--min-capacity", "0", "--suspended-state",
                 "DynamicScalingInSuspended=true,DynamicScalingOutSuspended=true,ScheduledScalingSuspended=true")
    aws_json("ecs", "update-service", "--cluster", cluster, "--service", service, "--desired-count", "0")
    aws_json("ecs", "wait", "services-stable", "--cluster", cluster, "--services", service)
    # Descubrir por nombre también permite actualizar desde la versión sin output deployment.
    functions = out.get("lambda_functions", {})
    for name in ("validar-documento", "notificar-mail"):
        if name in functions:
            mappings = aws_json("lambda", "list-event-source-mappings", "--function-name", functions[name])
            for mapping in mappings["EventSourceMappings"]:
                if mapping["State"] != "Disabled":
                    if mapping["State"] != "Disabling":
                        aws_json("lambda", "update-event-source-mapping", "--uuid", mapping["UUID"], "--no-enabled")
                    wait_mapping_disabled(mapping["UUID"])
    # La regla también puede faltar tras un apply parcial.
    rule = "accesoseguro-dev-vencer-permisos"
    rules = aws_json("events", "list-rules", "--name-prefix", rule)
    if any(item["Name"] == rule for item in rules["Rules"]):
        aws_json("events", "disable-rule", "--name", rule)


def wait_mapping_disabled(uuid):
    for _ in range(60):
        state = aws_json("lambda", "get-event-source-mapping", "--uuid", uuid)["State"]
        if state == "Disabled":
            return
        time.sleep(2)
    raise RuntimeError("No se pudo detener el consumidor antes de migrar")


def push_image(tag, repository):
    registry = repository.split("/", 1)[0]
    password = run(["aws", "ecr", "get-login-password", "--region", REGION], capture=True).stdout
    run(["docker", "login", "--username", "AWS", "--password-stdin", registry], input_text=password, capture=True)
    name = repository.split("/", 1)[1]
    response = aws("ecr", "describe-images", "--repository-name", name, "--image-ids", f"imageTag={tag}", check=False)
    if response.returncode == 0:
        print("La imagen inmutable de este commit ya existe; se reutiliza.", flush=True)
        return
    if "ImageNotFoundException" not in response.stderr:
        raise RuntimeError("No se pudo comprobar el tag en ECR")
    run(["docker", "tag", f"accesoseguro-build:{tag}", f"{repository}:{tag}"])
    run(["docker", "push", f"{repository}:{tag}"])


def migrate(out):
    deployment = out["deployment"]
    response = aws_json("ecs", "run-task", "--cluster", out["ecs_cluster_name"], "--launch-type", "FARGATE",
                        "--task-definition", deployment["task_definition"], "--count", "1",
                        "--network-configuration", json.dumps(deployment["network_configuration"]),
                        "--overrides", json.dumps({"containerOverrides": [
                            {"name": "api", "command": ["python", "manage_db.py", "upgrade"]}]}))
    if response.get("failures") or len(response.get("tasks", [])) != 1:
        raise RuntimeError("ECS no pudo iniciar la tarea de base de datos")
    task = response["tasks"][0]["taskArn"]
    aws_json("ecs", "wait", "tasks-stopped", "--cluster", out["ecs_cluster_name"], "--tasks", task)
    result = aws_json("ecs", "describe-tasks", "--cluster", out["ecs_cluster_name"], "--tasks", task)
    tasks = result.get("tasks", [])
    containers = tasks[0].get("containers", []) if len(tasks) == 1 else []
    if result.get("failures") or not containers or any(c.get("exitCode") != 0 for c in containers):
        raise RuntimeError(f"Falló la inicialización de RDS. Revisar la tarea {task} y sus logs en CloudWatch")


def activate(out):
    aws_json("ecs", "update-service", "--cluster", out["ecs_cluster_name"], "--service", out["ecs_service_name"],
             "--desired-count", str(out["deployment"]["desired_count"]), "--force-new-deployment")
    aws_json("ecs", "wait", "services-stable", "--cluster", out["ecs_cluster_name"], "--services", out["ecs_service_name"])
    # El waiter por sí solo no comprueba que la revisión esperada esté atendiendo.
    service = aws_json("ecs", "describe-services", "--cluster", out["ecs_cluster_name"],
                       "--services", out["ecs_service_name"])["services"][0]
    if (service["taskDefinition"] != out["deployment"]["task_definition"] or
            service["runningCount"] < out["deployment"]["desired_count"]):
        raise RuntimeError("ECS no está ejecutando la versión/cantidad esperada")
    with urlopen(out["api_url"] + "/health", timeout=15) as response:
        if json.load(response).get("status") != "ok":
            raise RuntimeError("La API no confirmó acceso al esquema de RDS")
    with urlopen(out["sitio_url"], timeout=15) as response:
        if response.status != 200:
            raise RuntimeError("El sitio no responde")
    # Verifica Layer, secreto, SQL y escritura real del reporte (sin solicitudes ficticias).
    with tempfile.TemporaryDirectory() as tmp:
        payload = Path(tmp) / "report.json"
        result = aws_json("lambda", "invoke", "--function-name", out["lambda_functions"]["generar-reporte"],
                          "--cli-binary-format", "raw-in-base64-out", "--payload", '{"sede_id":1}', str(payload))
        report = json.loads(payload.read_text())
        if result.get("FunctionError") or not report.get("ok"):
            raise RuntimeError("La comprobación de la Lambda de reportes falló")
    print("Despliegue verificado. Sitio:", out["sitio_url"], flush=True)


def deploy(tag, bucket, account):
    # Si fallan el empaquetado o Docker, aún no se detuvo la aplicación existente.
    run([sys.executable, "scripts/build-notifications-layer.py"])
    run(["docker", "build", "--platform", "linux/amd64", "-t", f"accesoseguro-build:{tag}", "backend"])
    bootstrap(bucket, account)
    tf("infra", "init", "-input=false", "-reconfigure", "-backend-config=backend.hcl")
    tf("infra", "validate")
    pause_existing(outputs())
    apply_phase(tag, False)
    out = outputs()
    push_image(tag, out["ecr_repository_url"])
    migrate(out)
    apply_phase(tag, True)
    activate(outputs())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", required=True)
    parser.add_argument("--image-tag", required=True, help="SHA completo del commit limpio")
    parser.add_argument("--state-bucket", default=os.environ.get("TF_STATE_BUCKET", ""))
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9a-f]{40}", args.image_tag):
        parser.error("Usar el SHA completo como tag inmutable")
    if run(["git", "status", "--porcelain"], capture=True).stdout.strip():
        parser.error("El despliegue requiere un checkout limpio; guardar los cambios en un commit")
    if run(["git", "rev-parse", "HEAD"], capture=True).stdout.strip() != args.image_tag:
        parser.error("El tag debe corresponder al commit actual")
    account = aws_json("sts", "get-caller-identity")["Account"]
    bucket = args.state_bucket or f"accesoseguro-tfstate-{account}"
    if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", bucket):
        parser.error("Nombre de bucket inválido")
    deploy(args.image_tag, bucket, account)


if __name__ == "__main__":
    main()
