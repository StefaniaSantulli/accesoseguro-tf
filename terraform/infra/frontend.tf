# =============================================================================
# FRONT: sitio estático en S3 (HTTP, lectura pública solo en este bucket).
# CloudFront no está disponible en el Learner Lab.
# =============================================================================

resource "aws_s3_bucket" "sitio" {
  bucket        = local.bucket_names["sitio"]
  force_destroy = var.force_destroy_buckets

  tags = { Name = local.bucket_names["sitio"] }
}

resource "aws_s3_bucket_website_configuration" "sitio" {
  bucket = aws_s3_bucket.sitio.id

  index_document {
    suffix = "index.html"
  }

  error_document {
    key = "index.html"
  }
}

# Es el único bucket con acceso público habilitado.
resource "aws_s3_bucket_public_access_block" "sitio" {
  bucket = aws_s3_bucket.sitio.id

  block_public_acls       = true
  ignore_public_acls      = true
  block_public_policy     = false
  restrict_public_buckets = false
}

resource "aws_s3_bucket_ownership_controls" "sitio" {
  bucket = aws_s3_bucket.sitio.id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

# SSE-S3 y no KMS: el visitante es anónimo y no puede pedir kms:Decrypt.
# Con KMS los objetos devolverían error.
resource "aws_s3_bucket_server_side_encryption_configuration" "sitio" {
  bucket = aws_s3_bucket.sitio.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# Cualquiera puede LEER objetos. Nadie puede listar, escribir ni borrar.
resource "aws_s3_bucket_policy" "sitio" {
  bucket = aws_s3_bucket.sitio.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "LecturaPublicaDelSitio"
        Effect    = "Allow"
        Principal = "*"
        Action    = "s3:GetObject"
        Resource  = "${aws_s3_bucket.sitio.arn}/*"
      }
    ]
  })

  # Con block_public_policy todavía en true, AWS rechaza una policy pública.
  depends_on = [aws_s3_bucket_public_access_block.sitio]
}

# -----------------------------------------------------------------------------
# Archivos del sitio: un aws_s3_object por archivo de ../frontend.
# -----------------------------------------------------------------------------
resource "aws_s3_object" "sitio" {
  for_each = local.frontend_files

  bucket = aws_s3_bucket.sitio.id
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
  bucket       = aws_s3_bucket.sitio.id
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
