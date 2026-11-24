# =============================================================================
# PRUEBA: RDS Proxy delante de RDS PostgreSQL.
#
# Encendido por defecto (enable_rds_proxy = true). Para desplegar sin proxy
# (por ejemplo, para ahorrar crédito del Lab), usar -var enable_rds_proxy=false:
# la API y las Lambdas vuelven a conectarse directo a la instancia.
#
# - Reutiliza el secreto que ya existe (aws_secretsmanager_secret.db).
# - Usa LabRole: en el Learner Lab no se pueden crear roles y RDS puede
#   asumir LabRole.
# - Va en las subredes de app: la NACL de datos solo deja entrar el 5432 desde
#   app, así que desde ahí llega a la base sin tocar la red.
# - Con el proxy activo, la API (DB_HOST en locals.tf) y las Lambdas (host del
#   secreto en security.tf) se conectan al proxy en vez de a la instancia.
# - Probado en el Learner Lab (pruebas/rds-proxy): target AVAILABLE.
# =============================================================================

variable "enable_rds_proxy" {
  description = "Crea un RDS Proxy delante de la base (pooling de conexiones y failover más rápido)."
  type        = bool
  default     = true
}

locals {
  rds_proxy_enabled = var.enable_rds_proxy ? 1 : 0
}

# -----------------------------------------------------------------------------
# Security Group del proxy
# -----------------------------------------------------------------------------
resource "aws_security_group" "rds_proxy" {
  count = local.rds_proxy_enabled

  name_prefix = "${local.name_prefix}-rds-proxy-"
  description = "RDS Proxy: recibe de ECS/Lambdas y sale hacia RDS"
  vpc_id      = module.network.vpc_id

  tags = { Name = "${local.name_prefix}-sg-rds-proxy" }

  lifecycle {
    create_before_destroy = true
  }
}

# Entrada al proxy: los mismos clientes que hoy van directo a la base.
resource "aws_vpc_security_group_ingress_rule" "rds_proxy_from" {
  for_each = var.enable_rds_proxy ? toset(["ecs", "lambda"]) : toset([])

  security_group_id            = aws_security_group.rds_proxy[0].id
  description                  = "PostgreSQL desde ${each.key}"
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
  referenced_security_group_id = aws_security_group.this[each.key].id
}

# Salida del proxy hacia la base.
resource "aws_vpc_security_group_egress_rule" "rds_proxy_to_rds" {
  count = local.rds_proxy_enabled

  security_group_id            = aws_security_group.rds_proxy[0].id
  description                  = "Proxy hacia RDS"
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
  referenced_security_group_id = aws_security_group.this["rds"].id
}

# Salida del proxy hacia los VPC Endpoints (Secrets Manager), por si los usa.
resource "aws_vpc_security_group_egress_rule" "rds_proxy_to_endpoints" {
  count = local.rds_proxy_enabled

  security_group_id            = aws_security_group.rds_proxy[0].id
  description                  = "Proxy hacia VPC Endpoints (Secrets Manager)"
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
  referenced_security_group_id = aws_security_group.this["endpoints"].id
}

resource "aws_vpc_security_group_ingress_rule" "endpoints_from_rds_proxy" {
  count = local.rds_proxy_enabled

  security_group_id            = aws_security_group.this["endpoints"].id
  description                  = "HTTPS a endpoints desde RDS Proxy"
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
  referenced_security_group_id = aws_security_group.rds_proxy[0].id
}

# La base acepta conexiones del proxy (se suma a las de ECS y Lambdas).
resource "aws_vpc_security_group_ingress_rule" "rds_from_proxy" {
  count = local.rds_proxy_enabled

  security_group_id            = aws_security_group.this["rds"].id
  description                  = "PostgreSQL desde RDS Proxy"
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
  referenced_security_group_id = aws_security_group.rds_proxy[0].id
}

# -----------------------------------------------------------------------------
# Proxy
# -----------------------------------------------------------------------------
resource "aws_db_proxy" "main" {
  count = local.rds_proxy_enabled

  name                   = "${local.name_prefix}-proxy"
  engine_family          = "POSTGRESQL"
  role_arn               = data.aws_iam_role.lab_role.arn
  vpc_subnet_ids         = module.network.subnet_ids_by_tier["app"]
  vpc_security_group_ids = [aws_security_group.rds_proxy[0].id]
  require_tls            = true # la base ya exige TLS (rds.force_ssl = 1)
  idle_client_timeout    = 1800
  debug_logging          = false

  auth {
    auth_scheme = "SECRETS"
    secret_arn  = aws_secretsmanager_secret.db.arn
    iam_auth    = "DISABLED"
    description = "Credenciales de RDS desde Secrets Manager"
  }

  tags = { Name = "${local.name_prefix}-proxy" }

  # Sin depends_on a la versión del secreto: esa versión guarda el endpoint
  # del proxy (host para las Lambdas) y se armaría un ciclo. El proxy solo
  # necesita el ARN; el target espera a que el contenido exista.
}

resource "aws_db_proxy_default_target_group" "main" {
  count = local.rds_proxy_enabled

  db_proxy_name = aws_db_proxy.main[0].name

  connection_pool_config {
    max_connections_percent      = 90
    max_idle_connections_percent = 50
    connection_borrow_timeout    = 120
  }
}

resource "aws_db_proxy_target" "main" {
  count = local.rds_proxy_enabled

  db_proxy_name          = aws_db_proxy.main[0].name
  target_group_name      = aws_db_proxy_default_target_group.main[0].name
  db_instance_identifier = aws_db_instance.main.identifier

  depends_on = [aws_secretsmanager_secret_version.db]
}

output "rds_proxy_endpoint" {
  description = "Endpoint del RDS Proxy (null si enable_rds_proxy = false)."
  value       = var.enable_rds_proxy ? aws_db_proxy.main[0].endpoint : null
}

output "rds_proxy_name" {
  description = "Nombre del RDS Proxy, para revisar el estado del target."
  value       = var.enable_rds_proxy ? aws_db_proxy.main[0].name : null
}
