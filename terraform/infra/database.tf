# =============================================================================
# DATOS: RDS PostgreSQL Multi-AZ, la fuente de verdad.
# =============================================================================

resource "aws_db_subnet_group" "main" {
  name        = "${local.name_prefix}-datos"
  description = "Subredes de datos (una por AZ)"
  subnet_ids  = module.network.subnet_ids_by_tier["datos"]
}

# Obliga a que toda conexión a la base vaya cifrada (TLS).
resource "aws_db_parameter_group" "main" {
  name_prefix = "${local.name_prefix}-pg${var.db_engine_version}-"
  family      = "postgres${var.db_engine_version}"
  description = "Parametros de ${local.name_prefix}"

  parameter {
    name  = "rds.force_ssl"
    value = "1"
  }

  parameter {
    name  = "log_min_duration_statement"
    value = "1000" # loguea consultas de más de 1 segundo
  }

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_db_instance" "main" {
  identifier     = "${local.name_prefix}-postgres"
  engine         = "postgres"
  engine_version = var.db_engine_version
  instance_class = var.db_instance_class

  db_name  = var.db_name
  username = var.db_username
  password = random_password.db.result
  port     = 5432

  allocated_storage     = var.db_allocated_storage
  max_allocated_storage = var.db_max_allocated_storage
  storage_type          = "gp3"
  storage_encrypted     = true
  kms_key_id            = aws_kms_key.main.arn

  # Standby sincrónica en la otra AZ. No se lee: solo toma el lugar de la
  # primaria si falla.
  multi_az               = var.db_multi_az
  db_subnet_group_name   = aws_db_subnet_group.main.name
  vpc_security_group_ids = [aws_security_group.this["rds"].id]
  parameter_group_name   = aws_db_parameter_group.main.name
  publicly_accessible    = false

  backup_retention_period    = var.db_backup_retention_days
  backup_window              = "06:00-07:00" # 03:00-04:00 en Argentina
  maintenance_window         = "sun:07:30-sun:08:30"
  auto_minor_version_upgrade = true
  copy_tags_to_snapshot      = true
  apply_immediately          = true

  deletion_protection       = var.db_deletion_protection
  skip_final_snapshot       = var.environment != "prod"
  final_snapshot_identifier = var.environment == "prod" ? "${local.name_prefix}-final-${formatdate("YYYYMMDDhhmm", timestamp())}" : null

  lifecycle {
    # timestamp() cambia en cada plan: se ignora para que no proponga cambios.
    ignore_changes = [final_snapshot_identifier]
  }

  tags = { Name = "${local.name_prefix}-postgres" }
}
