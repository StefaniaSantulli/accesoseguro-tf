# =============================================================================
# RED: VPC en 3 capas x 2 AZ (módulo propio), Security Groups encadenados y
# VPC Endpoints (no hay NAT Gateway).
# =============================================================================

module "network" {
  source = "../modules/network"

  name         = local.name_prefix
  vpc_cidr     = var.vpc_cidr
  azs          = local.azs
  subnet_tiers = local.subnet_tiers

  # Segunda barrera en datos: solo entra el 5432 desde las subredes de app.
  isolated_tiers = {
    datos = { allow_from_tier = "app", port = 5432 }
  }

  tags = local.common_tags
}

# -----------------------------------------------------------------------------
# Security Groups
# -----------------------------------------------------------------------------
resource "aws_security_group" "this" {
  for_each = local.security_groups

  # name_prefix + create_before_destroy: si hay que reemplazar un SG, primero
  # se crea el nuevo (con otro sufijo) y después se borra el viejo. Si no, AWS
  # no deja borrar un SG que todavía está asociado a una ENI.
  name_prefix = "${local.name_prefix}-${each.key}-"
  description = each.value
  vpc_id      = module.network.vpc_id

  tags = { Name = "${local.name_prefix}-sg-${each.key}" }

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_vpc_security_group_ingress_rule" "this" {
  for_each = local.sg_ingress_rules

  security_group_id            = aws_security_group.this[each.value.sg].id
  description                  = each.value.description
  ip_protocol                  = "tcp"
  from_port                    = each.value.port
  to_port                      = each.value.port
  cidr_ipv4                    = each.value.cidr
  referenced_security_group_id = each.value.source_sg == null ? null : aws_security_group.this[each.value.source_sg].id

  tags = { Name = each.key }
}

resource "aws_vpc_security_group_egress_rule" "this" {
  for_each = local.sg_egress_rules

  security_group_id            = aws_security_group.this[each.value.sg].id
  description                  = each.value.description
  ip_protocol                  = each.value.protocol
  from_port                    = each.value.port
  to_port                      = each.value.port
  cidr_ipv4                    = each.value.cidr
  referenced_security_group_id = each.value.dest_sg == null ? null : aws_security_group.this[each.value.dest_sg].id

  tags = { Name = each.key }
}

# -----------------------------------------------------------------------------
# VPC Endpoints
# -----------------------------------------------------------------------------
# Gateway (gratis): S3. Es una ruta en las tablas de app y datos. Lo usan ECS
# (las capas de las imágenes de ECR están en S3) y las Lambdas de la VPC.
resource "aws_vpc_endpoint" "s3" {
  vpc_id            = module.network.vpc_id
  service_name      = "com.amazonaws.${var.region}.s3"
  vpc_endpoint_type = "Gateway"

  route_table_ids = [
    module.network.route_table_ids_by_tier["app"],
    module.network.route_table_ids_by_tier["datos"],
  ]

  tags = { Name = "${local.name_prefix}-vpce-s3" }
}

# Interface (PrivateLink): una ENI por AZ en las subredes de app.
resource "aws_vpc_endpoint" "interface" {
  for_each = toset(var.interface_endpoints)

  vpc_id              = module.network.vpc_id
  service_name        = "com.amazonaws.${var.region}.${each.key}"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = module.network.subnet_ids_by_tier["app"]
  security_group_ids  = [aws_security_group.this["endpoints"].id]
  private_dns_enabled = true

  tags = { Name = "${local.name_prefix}-vpce-${replace(each.key, ".", "-")}" }
}
