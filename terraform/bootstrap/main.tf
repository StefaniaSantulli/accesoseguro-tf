data "aws_caller_identity" "current" {}

locals {
  # coalesce() devuelve el primer valor no vacío: el nombre que pasó el
  # usuario o, si no pasó ninguno, uno armado con el account id.
  state_bucket_name = coalesce(
    var.state_bucket_name,
    format("%s-tfstate-%s", var.project_name, data.aws_caller_identity.current.account_id)
  )

  # Key (ruta dentro del bucket) del state del Terraform principal.
  infra_state_key = format("%s/%s/infra.tfstate", var.project_name, var.environment)

  lock_table_name = "${var.project_name}-tf-locks"
}

# -----------------------------------------------------------------------------
# Bucket del state
# -----------------------------------------------------------------------------
resource "aws_s3_bucket" "tfstate" {
  bucket = local.state_bucket_name

  # Nunca se borra el state por accidente: un "terraform destroy" de este
  # proyecto falla a propósito. Para borrarlo hay que sacar esta línea a mano.
  lifecycle {
    prevent_destroy = true
  }
}

# Versionado: cada "apply" deja una versión nueva del state. Si el state se
# corrompe o alguien lo pisa, se recupera la versión anterior.
resource "aws_s3_bucket_versioning" "tfstate" {
  bucket = aws_s3_bucket.tfstate.id

  versioning_configuration {
    status = "Enabled"
  }
}

# Cifrado en reposo. El state tiene datos sensibles (por ejemplo la contraseña
# de RDS en texto plano), por eso el bucket va cifrado y privado.
resource "aws_s3_bucket_server_side_encryption_configuration" "tfstate" {
  bucket = aws_s3_bucket.tfstate.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
    bucket_key_enabled = true
  }
}

# Bloqueo total de acceso público.
resource "aws_s3_bucket_public_access_block" "tfstate" {
  bucket = aws_s3_bucket.tfstate.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Sin ACLs: los permisos se manejan solo con políticas (IAM / bucket policy).
resource "aws_s3_bucket_ownership_controls" "tfstate" {
  bucket = aws_s3_bucket.tfstate.id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

# Las versiones viejas del state se borran después de N días para no acumular.
resource "aws_s3_bucket_lifecycle_configuration" "tfstate" {
  bucket = aws_s3_bucket.tfstate.id

  rule {
    id     = "expirar-versiones-viejas"
    status = "Enabled"

    filter {}

    noncurrent_version_expiration {
      noncurrent_days = var.noncurrent_version_retention_days
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }

  # El lifecycle necesita que el versionado ya esté activo.
  depends_on = [aws_s3_bucket_versioning.tfstate]
}

# Solo se aceptan pedidos por HTTPS.
resource "aws_s3_bucket_policy" "tfstate" {
  bucket = aws_s3_bucket.tfstate.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "DenyInsecureTransport"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:*"
        Resource = [
          aws_s3_bucket.tfstate.arn,
          "${aws_s3_bucket.tfstate.arn}/*",
        ]
        Condition = {
          Bool = { "aws:SecureTransport" = "false" }
        }
      }
    ]
  })

  # Si la policy se aplica antes que el bloqueo de acceso público, AWS puede
  # rechazarla. Se fuerza el orden.
  depends_on = [aws_s3_bucket_public_access_block.tfstate]
}

# -----------------------------------------------------------------------------
# (Opcional) Tabla DynamoDB para el lock clásico.
# count = 0 o 1: el recurso existe solo si se pide.
# -----------------------------------------------------------------------------
resource "aws_dynamodb_table" "tf_locks" {
  count = var.create_dynamodb_lock_table ? 1 : 0

  name         = local.lock_table_name
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "LockID"

  attribute {
    name = "LockID"
    type = "S"
  }

  server_side_encryption {
    enabled = true
  }
}

# -----------------------------------------------------------------------------
# Genera infra/backend.hcl para no copiar el nombre del bucket a mano.
# -----------------------------------------------------------------------------
resource "local_file" "backend_config" {
  count = var.generate_backend_file ? 1 : 0

  filename        = "${path.module}/../infra/backend.hcl"
  file_permission = "0644"

  content = templatefile("${path.module}/backend.hcl.tftpl", {
    bucket     = aws_s3_bucket.tfstate.bucket
    key        = local.infra_state_key
    region     = var.region
    lock_table = var.create_dynamodb_lock_table ? aws_dynamodb_table.tf_locks[0].name : ""
  })
}
