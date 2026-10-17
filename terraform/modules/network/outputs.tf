output "vpc_id" {
  description = "ID de la VPC."
  value       = aws_vpc.this.id
}

output "vpc_cidr" {
  description = "CIDR de la VPC."
  value       = aws_vpc.this.cidr_block
}

output "subnet_ids_by_tier" {
  description = "Mapa capa => lista de IDs de subred (una por AZ)."
  value = {
    for tier in keys(var.subnet_tiers) :
    tier => [for key, s in local.subnets : aws_subnet.this[key].id if s.tier == tier]
  }
}

output "subnet_cidrs_by_tier" {
  description = "Mapa capa => lista de CIDRs."
  value = {
    for tier in keys(var.subnet_tiers) :
    tier => [for s in values(local.subnets) : s.cidr if s.tier == tier]
  }
}

output "route_table_ids_by_tier" {
  description = "Mapa capa => ID de su tabla de ruteo."
  value       = { for tier, rt in aws_route_table.this : tier => rt.id }
}

output "subnets" {
  description = "Detalle de cada subred creada (para documentar / depurar)."
  value = {
    for key, s in local.subnets : key => {
      id   = aws_subnet.this[key].id
      cidr = s.cidr
      az   = s.az
      tier = s.tier
    }
  }
}
