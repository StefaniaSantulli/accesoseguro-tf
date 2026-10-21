# -----------------------------------------------------------------------------
# Módulo propio: bucket S3 compatible con el Learner Lab.
# El bucket se crea con la AWS CLI (ver bucket.sh) y todo lo demás con los
# recursos aws_s3_bucket_* de Terraform.
# -----------------------------------------------------------------------------

locals {
  arn = "arn:aws:s3:::${var.name}"

  # Statement que rechaza todo pedido que no sea HTTPS. No se aplica a los
  # sitios públicos: el endpoint de website de S3 solo habla HTTP.
  deny_insecure_transport = {
    Sid       = "DenyInsecureTransport"
    Effect    = "Deny"
    Principal = "*"
    Action    = "s3:*"
    Resource  = [local.arn, "${local.arn}/*"]
    Condition = { Bool = { "aws:SecureTransport" = "false" } }
  }

  public_read = {
    Sid       = "LecturaPublicaDelSitio"
    Effect    = "Allow"
    Principal = "*"
    Action    = "s3:GetObject"
    Resource  = "${local.arn}/*"
  }

  # Se filtra con "for ... if" en vez de un condicional (a ? b : c) porque los
  # dos statements tienen formas distintas y el condicional exige el mismo tipo.
  policy_statements = concat(
    [for s in [local.public_read] : s if var.website],
    [for s in [local.deny_insecure_transport] : s if !var.website],
    var.extra_policy_statements
  )
}

# Crea y borra el bucket con la CLI; solo vacía su contenido si force_destroy.
resource "terraform_data" "bucket" {
  # Si cambia el nombre, es otro bucket: se reemplaza.
  triggers_replace = [var.name]

  # Un provisioner de destroy solo puede leer "self": todo lo que necesita
  # viaja en input.
  input = {
    name          = var.name
    region        = var.region
    force_destroy = var.force_destroy
    script        = "${path.module}/bucket.sh"
  }

  provisioner "local-exec" {
    command = "bash '${self.input.script}' create '${self.input.name}' '${self.input.region}'"
  }

  provisioner "local-exec" {
    when    = destroy
    command = "bash '${self.input.script}' delete '${self.input.name}' '${self.input.region}' '${self.input.force_destroy}'"
  }
}

resource "aws_s3_bucket_ownership_controls" "this" {
  bucket = terraform_data.bucket.output.name

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_public_access_block" "this" {
  bucket = terraform_data.bucket.output.name

  block_public_acls       = true
  ignore_public_acls      = true
  block_public_policy     = !var.website
  restrict_public_buckets = !var.website
}

resource "aws_s3_bucket_versioning" "this" {
  bucket = terraform_data.bucket.output.name

  versioning_configuration {
    status = var.versioning ? "Enabled" : "Suspended"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "this" {
  bucket = terraform_data.bucket.output.name

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = var.kms_key_arn == null ? "AES256" : "aws:kms"
      kms_master_key_id = var.kms_key_arn
    }
    bucket_key_enabled = var.kms_key_arn != null
  }
}

resource "aws_s3_bucket_policy" "this" {
  bucket = terraform_data.bucket.output.name
  policy = jsonencode({ Version = "2012-10-17", Statement = local.policy_statements })

  # Con block_public_policy todavía en true, AWS rechaza una policy pública.
  depends_on = [aws_s3_bucket_public_access_block.this]
}

resource "aws_s3_bucket_website_configuration" "this" {
  count = var.website ? 1 : 0

  bucket = terraform_data.bucket.output.name

  index_document {
    suffix = "index.html"
  }

  error_document {
    key = "index.html"
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "this" {
  count = length(var.lifecycle_rules) > 0 ? 1 : 0

  bucket = terraform_data.bucket.output.name

  dynamic "rule" {
    for_each = var.lifecycle_rules
    content {
      id     = rule.value.id
      status = "Enabled"

      filter {
        prefix = rule.value.prefix
      }

      dynamic "transition" {
        for_each = rule.value.transitions
        content {
          days          = transition.value.days
          storage_class = transition.value.storage_class
        }
      }

      dynamic "expiration" {
        for_each = rule.value.expiration_days == null ? [] : [rule.value.expiration_days]
        content {
          days = expiration.value
        }
      }

      dynamic "noncurrent_version_expiration" {
        for_each = rule.value.noncurrent_expiration_days == null ? [] : [rule.value.noncurrent_expiration_days]
        content {
          noncurrent_days = noncurrent_version_expiration.value
        }
      }
    }
  }

  # El lifecycle con versiones necesita el versionado ya configurado.
  depends_on = [aws_s3_bucket_versioning.this]
}

resource "aws_s3_bucket_cors_configuration" "this" {
  count = length(var.cors_rules) > 0 ? 1 : 0

  bucket = terraform_data.bucket.output.name

  dynamic "cors_rule" {
    for_each = var.cors_rules
    content {
      allowed_methods = cors_rule.value.allowed_methods
      allowed_origins = cors_rule.value.allowed_origins
      allowed_headers = cors_rule.value.allowed_headers
      expose_headers  = cors_rule.value.expose_headers
      max_age_seconds = cors_rule.value.max_age_seconds
    }
  }
}
