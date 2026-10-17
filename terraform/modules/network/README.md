# Módulo `network` (propio)

Crea una VPC en N capas x N zonas de disponibilidad, sin NAT Gateway.

| Recurso | Cantidad en AccesoSeguro | Meta-argumento |
|---|---|---|
| `aws_vpc.this` | 1 | – |
| `aws_subnet.this` | 6 (3 capas x 2 AZ) | `for_each` |
| `aws_internet_gateway.this` | 1 (solo si hay capa pública) | `count` |
| `aws_route_table.this` | 3 (una por capa) | `for_each` |
| `aws_route.internet` | 1 (0.0.0.0/0 en la capa pública) | `for_each` |
| `aws_route_table_association.this` | 6 | `for_each` |
| `aws_network_acl.isolated` | 1 (capa datos) | `for_each` + bloques `dynamic` |

Los CIDR se calculan con `cidrsubnet(vpc_cidr, newbits, netnum_offset + índice_az)`.

## Entradas
| Variable | Tipo | Descripción |
|---|---|---|
| `name` | string | Prefijo de nombres |
| `vpc_cidr` | string | CIDR de la VPC (validado con `cidrhost`) |
| `azs` | list(string) | Al menos 2 AZ |
| `subnet_tiers` | map(object) | `newbits`, `netnum_offset`, `public` por capa |
| `isolated_tiers` | map(object) | Capa con NACL: `allow_from_tier` y `port` |
| `tags` | map(string) | Tags extra |

## Salidas
`vpc_id`, `vpc_cidr`, `subnet_ids_by_tier`, `subnet_cidrs_by_tier`, `route_table_ids_by_tier`, `subnets`.

## Ejemplo
```hcl
module "network" {
  source       = "../modules/network"
  name         = "accesoseguro-dev"
  vpc_cidr     = "10.0.0.0/16"
  azs          = ["us-east-1a", "us-east-1b"]
  subnet_tiers = {
    publica = { newbits = 8, netnum_offset = 1, public = true }
    app     = { newbits = 8, netnum_offset = 11, public = false }
    datos   = { newbits = 8, netnum_offset = 21, public = false }
  }
  isolated_tiers = { datos = { allow_from_tier = "app", port = 5432 } }
}
```
