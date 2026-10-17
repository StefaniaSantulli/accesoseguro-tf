# -----------------------------------------------------------------------------
# Módulo propio: red en N capas x N zonas de disponibilidad.
# VPC + subredes + Internet Gateway + tablas de ruteo + NACL de capas aisladas.
# No crea NAT Gateway: las capas privadas no tienen salida a internet.
# -----------------------------------------------------------------------------

locals {
  # Producto cartesiano capa x AZ. Para la VPC 10.0.0.0/16 queda:
  #   publica-a 10.0.1.0/24   publica-b 10.0.2.0/24
  #   app-a     10.0.11.0/24  app-b     10.0.12.0/24
  #   datos-a   10.0.21.0/24  datos-b   10.0.22.0/24
  subnets = merge([
    for tier, cfg in var.subnet_tiers : {
      for idx, az in var.azs :
      # substr(az, -1, 1) toma la última letra de la AZ: "us-east-1a" -> "a"
      "${tier}-${substr(az, -1, 1)}" => {
        tier   = tier
        az     = az
        public = cfg.public
        cidr   = cidrsubnet(var.vpc_cidr, cfg.newbits, cfg.netnum_offset + idx)
      }
    }
  ]...)

  public_tiers = { for tier, cfg in var.subnet_tiers : tier => cfg if cfg.public }
}

resource "aws_vpc" "this" {
  cidr_block = var.vpc_cidr

  # Necesario para que los VPC Endpoints de tipo interface resuelvan con el
  # nombre normal del servicio (ej: secretsmanager.us-east-1.amazonaws.com).
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = merge(var.tags, { Name = "${var.name}-vpc" })
}

resource "aws_subnet" "this" {
  for_each = local.subnets

  vpc_id            = aws_vpc.this.id
  cidr_block        = each.value.cidr
  availability_zone = each.value.az

  # Ni siquiera las públicas reparten IP pública: lo único que vive ahí es el
  # ALB, que tiene sus propias IPs.
  map_public_ip_on_launch = false

  tags = merge(var.tags, {
    Name = "${var.name}-${each.key}"
    Tier = each.value.tier
  })
}

# Puerta de entrada desde internet. Solo la usan las subredes públicas.
resource "aws_internet_gateway" "this" {
  count = length(local.public_tiers) > 0 ? 1 : 0

  vpc_id = aws_vpc.this.id
  tags   = merge(var.tags, { Name = "${var.name}-igw" })
}

# Una tabla de ruteo por capa. Todas traen la ruta "local" (10.0.0.0/16).
resource "aws_route_table" "this" {
  for_each = var.subnet_tiers

  vpc_id = aws_vpc.this.id
  tags   = merge(var.tags, { Name = "${var.name}-rt-${each.key}" })
}

# Solo las capas públicas tienen 0.0.0.0/0 -> Internet Gateway.
# Las privadas no tienen ninguna ruta a internet (ni IGW ni NAT).
resource "aws_route" "internet" {
  for_each = local.public_tiers

  route_table_id         = aws_route_table.this[each.key].id
  destination_cidr_block = "0.0.0.0/0"
  gateway_id             = aws_internet_gateway.this[0].id
}

resource "aws_route_table_association" "this" {
  for_each = local.subnets

  subnet_id      = aws_subnet.this[each.key].id
  route_table_id = aws_route_table.this[each.value.tier].id
}

# -----------------------------------------------------------------------------
# NACL de las capas aisladas (en AccesoSeguro: datos).
# Es stateless: hay que permitir la ida y también la vuelta (puertos efímeros).
# -----------------------------------------------------------------------------
resource "aws_network_acl" "isolated" {
  for_each = var.isolated_tiers

  vpc_id = aws_vpc.this.id
  subnet_ids = [
    for key, s in local.subnets : aws_subnet.this[key].id if s.tier == each.key
  ]

  # Entrada: solo el puerto de la base, solo desde la capa permitida.
  dynamic "ingress" {
    for_each = [for s in values(local.subnets) : s.cidr if s.tier == each.value.allow_from_tier]
    content {
      rule_no    = 100 + ingress.key
      protocol   = "tcp"
      action     = "allow"
      cidr_block = ingress.value
      from_port  = each.value.port
      to_port    = each.value.port
    }
  }

  # Salida: solo las respuestas (puertos efímeros) hacia la capa permitida.
  dynamic "egress" {
    for_each = [for s in values(local.subnets) : s.cidr if s.tier == each.value.allow_from_tier]
    content {
      rule_no    = 100 + egress.key
      protocol   = "tcp"
      action     = "allow"
      cidr_block = egress.value
      from_port  = 1024
      to_port    = 65535
    }
  }

  tags = merge(var.tags, { Name = "${var.name}-nacl-${each.key}" })
}
