#!/usr/bin/env bash
# -----------------------------------------------------------------------------
# Crea o borra un bucket S3 con la AWS CLI.
#
# Por qué existe: en el Learner Lab una service control policy prohíbe
# s3:GetBucketObjectLockConfiguration, y el recurso aws_s3_bucket del provider
# de AWS lo lee SIEMPRE (en cualquier versión). Por eso el bucket "pelado" se
# crea acá y toda su configuración (cifrado, versionado, políticas, lifecycle,
# website, CORS) sigue en Terraform con los recursos aws_s3_bucket_*.
#
# Uso: bucket.sh create <bucket> <region>
#      bucket.sh delete <bucket> <region> <true|false>
# -----------------------------------------------------------------------------
set -euo pipefail

action="$1"
bucket="$2"
region="$3"
force="${4:-false}"

exists() {
  aws s3api head-bucket --bucket "$bucket" --region "$region" >/dev/null 2>&1
}

case "$action" in
  create)
    if exists; then
      echo "El bucket $bucket ya existe: se reutiliza."
      exit 0
    fi
    if [ "$region" = "us-east-1" ]; then
      aws s3api create-bucket --bucket "$bucket" --region "$region" >/dev/null
    else
      aws s3api create-bucket --bucket "$bucket" --region "$region" \
        --create-bucket-configuration LocationConstraint="$region" >/dev/null
    fi
    aws s3api wait bucket-exists --bucket "$bucket" --region "$region"
    echo "Bucket $bucket creado."
    ;;

  delete)
    if ! exists; then
      echo "El bucket $bucket no existe: nada que borrar."
      exit 0
    fi
    if [ "$force" != "true" ]; then
      echo "force_destroy = false: el bucket $bucket se conserva (solo sale del state)."
      exit 0
    fi
    # Vaciar todas las versiones y marcas de borrado (los buckets versionados
    # no se pueden borrar si queda alguna).
    for kind in Versions DeleteMarkers; do
      while :; do
        batch=$(aws s3api list-object-versions --bucket "$bucket" --region "$region" \
          --no-paginate --max-keys 1000 \
          --query "{Objects: $kind[].{Key:Key,VersionId:VersionId}}" --output json)
        echo "$batch" | grep -q '"VersionId"' || break
        aws s3api delete-objects --bucket "$bucket" --region "$region" --delete "$batch" >/dev/null
      done
    done
    aws s3 rm "s3://$bucket" --recursive --region "$region" >/dev/null || true
    aws s3api delete-bucket --bucket "$bucket" --region "$region"
    echo "Bucket $bucket borrado."
    ;;

  *)
    echo "Acción desconocida: $action" >&2
    exit 1
    ;;
esac
