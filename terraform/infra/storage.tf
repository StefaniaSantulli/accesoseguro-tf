# =============================================================================
# ALMACENAMIENTO: buckets privados de documentos y auditoría.
# Módulo EXTERNO del Terraform Registry: terraform-aws-modules/s3-bucket/aws.
# Un solo bloque module con for_each crea los dos buckets.
# =============================================================================

module "buckets" {
  source  = "terraform-aws-modules/s3-bucket/aws"
  version = "~> 5.0"

  for_each = local.private_buckets

  bucket        = local.bucket_names[each.key]
  force_destroy = var.force_destroy_buckets

  # Privado: los 4 bloqueos de acceso público y sin ACLs.
  block_public_acls        = true
  block_public_policy      = true
  ignore_public_acls       = true
  restrict_public_buckets  = true
  control_object_ownership = true
  object_ownership         = "BucketOwnerEnforced"

  # Política que rechaza cualquier pedido que no venga por HTTPS.
  attach_deny_insecure_transport_policy = true

  versioning = {
    enabled = each.value.versioning
  }

  # Cifrado con la clave KMS del proyecto.
  server_side_encryption_configuration = {
    rule = {
      apply_server_side_encryption_by_default = {
        sse_algorithm     = "aws:kms"
        kms_master_key_id = aws_kms_key.main.arn
      }
      bucket_key_enabled = true
    }
  }

  # Reglas propias de cada bucket (definidas en locals.tf).
  lifecycle_rule = local.bucket_lifecycle_rules[each.key]
  cors_rule      = local.bucket_cors_rules[each.key]

  tags = {
    Name        = local.bucket_names[each.key]
    Description = each.value.description
  }
}

# -----------------------------------------------------------------------------
# Cada PDF nuevo genera un mensaje en cola-documentos (dispara la validación).
# -----------------------------------------------------------------------------
resource "aws_s3_bucket_notification" "documentos" {
  bucket = module.buckets["documentos"].s3_bucket_id

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
