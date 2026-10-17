variable "name" {
  description = "Prefijo para nombrar los recursos de red."
  type        = string
}

variable "vpc_cidr" {
  description = "Bloque CIDR de la VPC."
  type        = string

  validation {
    condition     = can(cidrhost(var.vpc_cidr, 0))
    error_message = "vpc_cidr tiene que ser un CIDR válido, por ejemplo 10.0.0.0/16."
  }
}

variable "azs" {
  description = "Zonas de disponibilidad donde se reparte cada capa (una subred por capa y por AZ)."
  type        = list(string)

  validation {
    condition     = length(var.azs) >= 2
    error_message = "Se necesitan al menos 2 AZ para alta disponibilidad."
  }
}

variable "subnet_tiers" {
  description = <<-EOT
    Capas de subredes. Por cada capa se crea una subred en cada AZ.
    - newbits: bits que se suman a la máscara de la VPC (/16 + 8 = /24).
    - netnum_offset: número de la primera subred de la capa (10.0.<offset>.0).
    - public: si la capa tiene ruta 0.0.0.0/0 al Internet Gateway.
  EOT
  type = map(object({
    newbits       = number
    netnum_offset = number
    public        = bool
  }))
}

variable "isolated_tiers" {
  description = <<-EOT
    Capas que llevan una NACL restrictiva (segunda barrera, sin estado).
    Solo entra tráfico TCP al puerto indicado desde las subredes de la capa
    allow_from_tier, y solo sale la respuesta (puertos efímeros) hacia ellas.
  EOT
  type = map(object({
    allow_from_tier = string
    port            = number
  }))
  default = {}
}

variable "tags" {
  description = "Tags adicionales para todos los recursos."
  type        = map(string)
  default     = {}
}
