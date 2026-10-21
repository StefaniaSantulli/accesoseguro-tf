#!/usr/bin/env bash
# -----------------------------------------------------------------------------
# Construye la imagen de la API (backend/) y la sube a ECR.
#
# El tag sale de api_image_tag en terraform/infra/terraform.tfvars (o se pasa
# como argumento: scripts/push-image.sh v1.2). Los tags son inmutables, así
# que cada cambio de la API necesita un tag nuevo.
#
# Después de subirla: "make plan" y "make apply". Terraform crea una revisión
# nueva de la task definition y ECS hace el rolling update.
# -----------------------------------------------------------------------------
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
INFRA="$ROOT/terraform/infra"

TAG="${1:-$(sed -nE 's/^[[:space:]]*api_image_tag[[:space:]]*=[[:space:]]*"([^"]+)".*/\1/p' "$INFRA/terraform.tfvars" 2>/dev/null | head -1)}"
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
echo "==> Listo. Ahora: make plan  y  make apply  (despliega $TAG en ECS)."
