# =============================================================================
# ALMACENAMIENTO: buckets privados de documentos y auditoría.
# Módulo propio s3-bucket con for_each: una sola llamada crea los dos.
# =============================================================================

module "buckets" {
  source   = "../modules/s3-bucket"
  for_each = local.private_buckets

  name          = local.bucket_names[each.key]
  region        = var.region
  force_destroy = var.force_destroy_buckets
  versioning    = each.value.versioning

  # Privado y cifrado con la clave KMS del proyecto. La policy que exige
  # HTTPS la agrega el módulo.
  kms_key_arn = aws_kms_key.main.arn

  lifecycle_rules = local.bucket_lifecycle_rules[each.key]
  cors_rules      = local.bucket_cors_rules[each.key]
}

# -----------------------------------------------------------------------------
# Cada PDF nuevo genera un mensaje en cola-documentos (dispara la validación).
# -----------------------------------------------------------------------------
resource "aws_s3_bucket_notification" "documentos" {
  bucket = module.buckets["documentos"].id

  queue {
    id            = "pdf-nuevo"
    queue_arn     = module.sqs["documentos"].queue_arn
    events        = ["s3:ObjectCreated:*"]
    filter_prefix = "empresas/"
    filter_suffix = ".pdf"
  }

  # Al crear la notificación S3 prueba que puede escribir en la cola. Si la
  # policy de la cola todavía no existe, falla. Dependencia explícita.
  depends_on = [aws_sqs_queue_policy.this]
}
