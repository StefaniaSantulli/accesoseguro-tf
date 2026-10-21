#!/usr/bin/env bash
# -----------------------------------------------------------------------------
# Construye la imagen de la API (backend/) y la sube a ECR.
#
# El tag sale de api_image_tag en terraform/infra/terraform.tfvars (o se pasa
# como argumento: scripts/push-image.sh v1.2). Los tags son inmutables, así
# que cada cambio de la API necesita un tag nuevo.
#
# Herramienta individual: solo publica una imagen. Para desplegar también
# infraestructura y esquema de RDS usar "make deploy" o el workflow completo.
# -----------------------------------------------------------------------------
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
INFRA="$ROOT/terraform/infra"

TAG="${1:-}"
if [ -z "$TAG" ] && [ -f "$INFRA/terraform.tfvars" ]; then
  TAG="$(sed -nE 's/^[[:space:]]*api_image_tag[[:space:]]*=[[:space:]]*"([^"]+)".*/\1/p' "$INFRA/terraform.tfvars" | head -1)"
fi
TAG="${TAG:-v1.1}"

REPO_URL="$(cd "$INFRA" && terraform output -raw ecr_repository_url)"
REGISTRY="${REPO_URL%%/*}"
REGION="$(echo "$REGISTRY" | cut -d. -f4)"

echo "==> Imagen: $REPO_URL:$TAG"

if aws ecr describe-images --repository-name "${REPO_URL##*/}" --image-ids imageTag="$TAG" --region "$REGION" >/dev/null 2>&1; then
  echo "La versión $TAG ya existe en ECR (los tags son inmutables). Cambiá api_image_tag en terraform.tfvars por una nueva, por ejemplo v1.2."
  exit 1
fi

aws ecr get-login-password --region "$REGION" | docker login --username AWS --password-stdin "$REGISTRY"
docker build --platform linux/amd64 -t "$REPO_URL:$TAG" "$ROOT/backend"
docker push "$REPO_URL:$TAG"

echo
echo "==> Imagen publicada. Esto no inicializa RDS ni actualiza ECS; para el despliegue completo usar make deploy."
