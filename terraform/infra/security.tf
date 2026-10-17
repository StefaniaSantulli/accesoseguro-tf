# =============================================================================
# SEGURIDAD: KMS, Secrets Manager y CloudTrail.
# IAM: en el Learner Lab no se crean roles; ECS y Lambdas usan LabRole
# (data.aws_iam_role.lab_role). Sí se escriben políticas sobre los recursos
# (buckets, tópico y colas). Shield Standard viene activado solo en el ALB.
# =============================================================================

# -----------------------------------------------------------------------------
# KMS: una clave del proyecto para RDS, buckets privados, colas, tópico,
# ECR y secretos.
# -----------------------------------------------------------------------------
resource "aws_kms_key" "main" {
  description             = "Clave de ${local.name_prefix}: RDS, S3, SQS, SNS, ECR y Secrets Manager"
  deletion_window_in_days = 7
  enable_key_rotation     = true

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # La cuenta administra la clave y las políticas IAM (LabRole) aplican.
        Sid       = "PermisosDeLaCuenta"
        Effect    = "Allow"
        Principal = { AWS = "arn:aws:iam::${local.account_id}:root" }
        Action    = "kms:*"
        Resource  = "*"
      },
      {
        # S3 (notificaciones a SQS), SNS (entrega a SQS), EventBridge y
        # CloudWatch (alarmas a SNS) escriben en recursos cifrados con esta
        # clave, así que necesitan poder usarla.
        Sid    = "ServiciosQuePublicanEnRecursosCifrados"
        Effect = "Allow"
        Principal = {
          Service = [
            "s3.amazonaws.com",
            "sns.amazonaws.com",
            "events.amazonaws.com",
            "cloudwatch.amazonaws.com",
          ]
        }
        Action   = ["kms:GenerateDataKey*", "kms:Decrypt"]
        Resource = "*"
      }
    ]
  })
}

resource "aws_kms_alias" "main" {
  name          = "alias/${local.name_prefix}"
  target_key_id = aws_kms_key.main.key_id
}

# -----------------------------------------------------------------------------
# Contraseña de RDS: se genera al azar y se guarda en Secrets Manager, nunca
# en el código ni en la imagen.
# -----------------------------------------------------------------------------
resource "random_password" "db" {
  length           = 24
  special          = true
  override_special = "!#$%^&*()-_=+[]{}<>:?" # RDS no acepta / @ " ni espacios
}

resource "aws_secretsmanager_secret" "db" {
  name_prefix             = "${local.name_prefix}-rds-"
  description             = "Credenciales de RDS PostgreSQL de ${local.name_prefix}"
  kms_key_id              = aws_kms_key.main.arn
  recovery_window_in_days = var.secrets_recovery_window_days
}

resource "aws_secretsmanager_secret_version" "db" {
  secret_id = aws_secretsmanager_secret.db.id

  secret_string = jsonencode({
    engine   = "postgres"
    username = var.db_username
    password = random_password.db.result
    host     = aws_db_instance.main.address
    port     = aws_db_instance.main.port
    dbname   = var.db_name
  })
}

# -----------------------------------------------------------------------------
# CloudTrail (opcional, count = 0 o 1)
# -----------------------------------------------------------------------------
resource "aws_s3_bucket" "cloudtrail" {
  count = var.enable_cloudtrail ? 1 : 0

  bucket        = format("%s-cloudtrail-%s", local.name_prefix, local.account_id)
  force_destroy = var.force_destroy_buckets
}

resource "aws_s3_bucket_public_access_block" "cloudtrail" {
  count = var.enable_cloudtrail ? 1 : 0

  bucket                  = aws_s3_bucket.cloudtrail[0].id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_policy" "cloudtrail" {
  count = var.enable_cloudtrail ? 1 : 0

  bucket = aws_s3_bucket.cloudtrail[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "CloudTrailAclCheck"
        Effect    = "Allow"
        Principal = { Service = "cloudtrail.amazonaws.com" }
        Action    = "s3:GetBucketAcl"
        Resource  = aws_s3_bucket.cloudtrail[0].arn
      },
      {
        Sid       = "CloudTrailWrite"
        Effect    = "Allow"
        Principal = { Service = "cloudtrail.amazonaws.com" }
        Action    = "s3:PutObject"
        Resource  = "${aws_s3_bucket.cloudtrail[0].arn}/AWSLogs/${local.account_id}/*"
        Condition = {
          StringEquals = { "s3:x-amz-acl" = "bucket-owner-full-control" }
        }
      }
    ]
  })
}

resource "aws_cloudtrail" "main" {
  count = var.enable_cloudtrail ? 1 : 0

  name                          = "${local.name_prefix}-trail"
  s3_bucket_name                = aws_s3_bucket.cloudtrail[0].id
  include_global_service_events = true
  is_multi_region_trail         = false
  enable_log_file_validation    = true

  # CloudTrail valida que puede escribir en el bucket al crearse: la policy
  # tiene que existir antes. Terraform no lo deduce solo.
  depends_on = [aws_s3_bucket_policy.cloudtrail]
}
