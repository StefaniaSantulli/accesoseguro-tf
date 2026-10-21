#!/usr/bin/env bash
# -----------------------------------------------------------------------------
# Crea o borra un bucket S3 con la AWS CLI.
#
# Por qué existe: evita la lectura de s3:GetBucketObjectLockConfiguration,
# restringida en el Lab usado para esta entrega. El bucket "pelado" se
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
tmp_dir=$(mktemp -d)
trap 'rm -rf "$tmp_dir"' EXIT

exists() {
  if aws s3api head-bucket --bucket "$bucket" --region "$region" > /dev/null 2> "$tmp_dir/head-error"; then
    return 0
  fi
  if grep -Eq '\((404|NoSuchBucket|NotFound)\)' "$tmp_dir/head-error"; then
    return 1
  fi
  # Un 403 o credenciales vencidas no significan que el bucket no exista.
  cat "$tmp_dir/head-error" >&2
  exit 1
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
      # Sin autorización para vaciarlo: S3 rechaza el borrado si contiene datos.
      # El error conserva el recurso en el state, para poder corregir y reintentar.
      aws s3api delete-bucket --bucket "$bucket" --region "$region"
      echo "Bucket vacío $bucket borrado."
      exit 0
    fi
    # Procesar versiones y marcas juntas: una página puede contener solo marcas
    # y dejar versiones en páginas posteriores. También incluye objetos sin versionar.
    while :; do
      aws s3api list-object-versions --bucket "$bucket" --region "$region" \
        --no-paginate --max-keys 1000 \
        --query '{Objects: [Versions,DeleteMarkers][].{Key:Key,VersionId:VersionId}}' \
        --output json > "$tmp_dir/batch.json"
      count=$(python3 -c 'import json,sys; print(len(json.load(sys.stdin)["Objects"]))' < "$tmp_dir/batch.json")
      [ "$count" -eq 0 ] && break
      aws s3api delete-objects --bucket "$bucket" --region "$region" \
        --delete "file://$tmp_dir/batch.json" --output json > "$tmp_dir/deleted.json"
      # S3 puede responder HTTP 200 y aun así devolver errores por objeto.
      errors=$(python3 -c 'import json,sys; print(len(json.load(sys.stdin).get("Errors", [])))' < "$tmp_dir/deleted.json")
      if [ "$errors" -gt 0 ]; then
        cat "$tmp_dir/deleted.json" >&2
        exit 1
      fi
    done
    aws s3api delete-bucket --bucket "$bucket" --region "$region"
    echo "Bucket $bucket borrado."
    ;;

  *)
    echo "Acción desconocida: $action" >&2
    exit 1
    ;;
esac
