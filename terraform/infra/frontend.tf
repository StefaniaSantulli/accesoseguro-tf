# =============================================================================
# FRONT: sitio estático en S3 (HTTP, lectura pública solo en este bucket).
# CloudFront no está disponible en el Learner Lab.
# =============================================================================

# Bucket público de solo lectura con website hosting (módulo propio).
# SSE-S3 y no KMS: el visitante es anónimo y no puede pedir kms:Decrypt.
module "sitio" {
  source = "../modules/s3-bucket"

  name          = local.bucket_names["sitio"]
  region        = var.region
  force_destroy = var.force_destroy_buckets
  versioning    = false # el sitio ya está versionado en git
  kms_key_arn   = null
  website       = true
}

# -----------------------------------------------------------------------------
# Archivos del sitio: un aws_s3_object por archivo de ../frontend.
# -----------------------------------------------------------------------------
resource "aws_s3_object" "sitio" {
  for_each = local.frontend_files

  bucket = module.sitio.id
  key    = each.value
  source = "${local.frontend_dir}/${each.value}"

  # La extensión es lo que va después del último punto.
  content_type = lookup(
    local.mime_types,
    lower(element(split(".", each.value), length(split(".", each.value)) - 1)),
    "application/octet-stream"
  )

  # Si el archivo cambia, cambia el hash y se vuelve a subir.
  etag = filemd5("${local.frontend_dir}/${each.value}")
}

# config.js se genera con los valores reales de esta infraestructura.
resource "aws_s3_object" "config_js" {
  bucket       = module.sitio.id
  key          = "config.js"
  content_type = local.mime_types["js"]

  content = templatefile("${local.frontend_dir}/config.js.tftpl", {
    api_url        = "http://${aws_lb.api.dns_name}"
    login_url      = local.cognito_login_url
    cognito_domain = local.cognito_domain_url
    client_id      = aws_cognito_user_pool_client.web.id
    region         = var.region
    environment    = var.environment
  })
}
