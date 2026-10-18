# Módulo `s3-bucket` (propio)

Bucket S3 compatible con el AWS Learner Lab.

**Por qué no usa `aws_s3_bucket`:** en el Learner Lab una *service control policy* prohíbe
`s3:GetBucketObjectLockConfiguration`, y el recurso `aws_s3_bucket` del provider (v5 y v6)
lo lee siempre al refrescar el bucket, así que falla con `AccessDenied`. Por eso:

- El bucket "pelado" se crea con la AWS CLI desde un `terraform_data` con `local-exec`
  (`bucket.sh create`). Si ya existe, se reutiliza (idempotente).
- Al destruir, un provisioner `when = destroy` lo vacía (incluidas todas las versiones) y lo
  borra, solo si `force_destroy = true`.
- Todo lo demás es Terraform normal: `aws_s3_bucket_ownership_controls`, `public_access_block`,
  `versioning`, `server_side_encryption_configuration`, `policy`, `website_configuration`,
  `lifecycle_configuration` y `cors_configuration`.

| Variable | Descripción |
|---|---|
| `name`, `region` | Bucket y región |
| `force_destroy` | Borrar el bucket con todo su contenido en un destroy |
| `versioning` | Versionado |
| `kms_key_arn` | SSE-KMS con esa clave; `null` = SSE-S3 (obligatorio en sitios públicos) |
| `website` | Website hosting + lectura pública (sin la policy de "solo HTTPS", porque el endpoint de website es HTTP) |
| `lifecycle_rules` | Lista tipada: `id`, `prefix`, `transitions`, `expiration_days`, `noncurrent_expiration_days` |
| `cors_rules` | Reglas CORS |
| `extra_policy_statements` | Statements extra para la bucket policy |

Salidas: `id`, `arn`, `website_endpoint`.

Requiere la AWS CLI instalada donde corre `terraform apply`.
