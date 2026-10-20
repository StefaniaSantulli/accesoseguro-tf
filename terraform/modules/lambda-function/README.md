# Módulo `lambda-function` (propio)

Empaqueta una carpeta de código Python en un zip (`archive_file`) y crea la función
con su log group (retención fija). Se usa con `for_each` desde la raíz para las 7
Lambdas de AccesoSeguro.

- `vpc_config = null` → la función corre fuera de la VPC.
- `vpc_config = { subnet_ids, security_group_ids }` → se agrega el bloque `dynamic "vpc_config"`.
- `source_code_hash` cambia solo si cambia el código: Terraform redespliega solo esa función.
- `depends_on = [aws_cloudwatch_log_group.this]`: el log group se crea antes para que Lambda no
  cree uno propio con retención infinita.

## Entradas principales
`function_name`, `source_dir`, `build_dir`, `role_arn`, `handler`, `runtime`, `timeout`
(validado 1-900), `memory_size`, `environment`, `vpc_config`, `log_retention_days`, `tags`.

## Salidas
`function_name`, `arn`, `invoke_arn`, `log_group_name`.
