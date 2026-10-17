#!/usr/bin/env bash
# Construye la imagen de la API, la sube a ECR y fuerza un nuevo despliegue en ECS.
# Los datos (repo, tag, cluster, servicio) salen de los outputs de Terraform.
set -euo pipefail

INFRA_DIR="$(cd "$(dirname "$0")/../terraform/infra" && pwd)"
cd "$INFRA_DIR"

eval "$(terraform output -raw docker_push_commands | sed 's/^/set -x; /' | sed 's/$/;/')"
