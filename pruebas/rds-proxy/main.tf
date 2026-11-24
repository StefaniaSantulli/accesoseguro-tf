# =============================================================================
# PRUEBA MÍNIMA: ¿funciona RDS Proxy en el Learner Lab?
#
# Independiente del proyecto: state local, VPC por defecto, base chica.
#   terraform init
#   terraform apply      (15-25 min)
#   ...revisar el target...
#   terraform destroy    (borrar todo al terminar)
# =============================================================================

terraform {
  required_providers {
    aws    = { source = "hashicorp/aws", version = "~> 5.0" }
    random = { source = "hashicorp/random", version = "~> 3.0" }
  }
}

provider "aws" {
  region = "us-east-1"
}

# --- Datos existentes --------------------------------------------------------
data "aws_vpc" "default" {
  default = true
}

data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
  filter {
    name   = "availability-zone"
    values = ["us-east-1a", "us-east-1b"]
  }
}

data "aws_iam_role" "lab_role" {
  name = "LabRole"
}

# --- Red: un SG para la base y el proxy (se permiten entre sí) --------------
resource "aws_security_group" "prueba" {
  name_prefix = "prueba-rds-proxy-"
  description = "Prueba RDS Proxy"
  vpc_id      = data.aws_vpc.default.id

  ingress {
    description = "PostgreSQL entre proxy y base"
    from_port   = 5432
    to_port     = 5432
    protocol    = "tcp"
    self        = true
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_db_subnet_group" "prueba" {
  name_prefix = "prueba-rds-proxy-"
  subnet_ids  = data.aws_subnets.default.ids
}

# --- Base de prueba ----------------------------------------------------------
resource "random_password" "db" {
  length           = 20
  special          = true
  override_special = "!#$%^&*()-_=+"
}

resource "aws_db_instance" "prueba" {
  identifier_prefix      = "prueba-proxy-"
  engine                 = "postgres"
  instance_class         = "db.t3.micro"
  allocated_storage      = 20
  storage_type           = "gp2"
  db_name                = "prueba"
  username               = "prueba_admin"
  password               = random_password.db.result
  db_subnet_group_name   = aws_db_subnet_group.prueba.name
  vpc_security_group_ids = [aws_security_group.prueba.id]
  publicly_accessible    = false
  multi_az               = false
  skip_final_snapshot    = true
  apply_immediately      = true
  monitoring_interval    = 0 # Enhanced monitoring no está soportado en el lab
}

# --- Secret ------------------------------------------------------------------
resource "aws_secretsmanager_secret" "db" {
  name_prefix             = "prueba-rds-proxy-"
  recovery_window_in_days = 0 # borrado inmediato en el destroy
}

resource "aws_secretsmanager_secret_version" "db" {
  secret_id = aws_secretsmanager_secret.db.id
  secret_string = jsonencode({
    username = aws_db_instance.prueba.username
    password = random_password.db.result
  })
}

# --- Proxy -------------------------------------------------------------------
resource "aws_db_proxy" "prueba" {
  name                   = "prueba-rds-proxy"
  engine_family          = "POSTGRESQL"
  role_arn               = data.aws_iam_role.lab_role.arn
  vpc_subnet_ids         = data.aws_subnets.default.ids
  vpc_security_group_ids = [aws_security_group.prueba.id]
  require_tls            = true

  auth {
    auth_scheme = "SECRETS"
    secret_arn  = aws_secretsmanager_secret.db.arn
    iam_auth    = "DISABLED"
  }

  depends_on = [aws_secretsmanager_secret_version.db]
}

resource "aws_db_proxy_default_target_group" "prueba" {
  db_proxy_name = aws_db_proxy.prueba.name
}

resource "aws_db_proxy_target" "prueba" {
  db_proxy_name          = aws_db_proxy.prueba.name
  target_group_name      = aws_db_proxy_default_target_group.prueba.name
  db_instance_identifier = aws_db_instance.prueba.identifier
}

output "comando_para_verificar" {
  value = "aws rds describe-db-proxy-targets --db-proxy-name ${aws_db_proxy.prueba.name} --query 'Targets[].TargetHealth'"
}
