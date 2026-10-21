# Módulo `s3-bucket` (propio)

Bucket S3 compatible con el AWS Learner Lab.

**Por qué no usa `aws_s3_bucket`:** conserva el workaround del Lab utilizado para evitar
la lectura restringida de `s3:GetBucketObjectLockConfiguration`. Por eso:

- El bucket "pelado" se crea con la AWS CLI desde un `terraform_data` con `local-exec`
  (`bucket.sh create`). Si ya existe, se reutiliza (idempotente).
- Al destruir, un provisioner `when = destroy` borra el bucket. Con `force_destroy = true`
  primero lo vacía, incluidas todas las versiones.
- Un error de permisos no se interpreta como bucket inexistente. Se comprueban también
  los errores por objeto que S3 puede devolver dentro de una respuesta exitosa de
  [DeleteObjects](https://docs.aws.amazon.com/cli/latest/reference/s3api/delete-objects.html).
- Con `force_destroy = false` solo se puede borrar si está vacío. Si tiene objetos,
  S3 rechaza el borrado y el recurso permanece en el state.
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

Requiere Bash, Python 3 y AWS CLI donde corre `terraform apply` o `terraform destroy`.
