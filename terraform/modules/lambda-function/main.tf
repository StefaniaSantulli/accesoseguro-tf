# -----------------------------------------------------------------------------
# Módulo propio: una función Lambda en Python empaquetada desde una carpeta.
# Se llama una vez con for_each desde la raíz para crear las 7 funciones
# sin repetir código (DRY).
# -----------------------------------------------------------------------------

# Comprime la carpeta del código en un .zip durante el plan. Si el código
# cambia, cambia el hash y Terraform vuelve a desplegar solo esta función.
data "archive_file" "this" {
  type        = "zip"
  source_dir  = var.source_dir
  output_path = "${var.build_dir}/${var.function_name}.zip"
  excludes    = ["__pycache__", "*.pyc", "tests"]

  # Permisos fijos dentro del zip: Lambda necesita poder leer el código, sin
  # importar con qué permisos quedó el archivo en la compu de cada uno.
  output_file_mode = "0644"
}

# El log group se crea antes que la función para fijar la retención. Si no,
# Lambda lo crea sola con retención infinita (y se paga para siempre).
resource "aws_cloudwatch_log_group" "this" {
  name              = "/aws/lambda/${var.function_name}"
  retention_in_days = var.log_retention_days
  tags              = var.tags
}

resource "aws_lambda_function" "this" {
  function_name = var.function_name
  description   = var.description
  role          = var.role_arn
  handler       = var.handler
  runtime       = var.runtime
  architectures = [var.architecture]
  timeout       = var.timeout
  memory_size   = var.memory_size
  layers        = var.layers

  filename         = data.archive_file.this.output_path
  source_code_hash = data.archive_file.this.output_base64sha256

  # Bloque dinámico: solo se agrega vpc_config a las Lambdas que necesitan
  # llegar a RDS. Las demás corren fuera de la VPC.
  dynamic "vpc_config" {
    for_each = var.vpc_config == null ? [] : [var.vpc_config]
    content {
      subnet_ids         = vpc_config.value.subnet_ids
      security_group_ids = vpc_config.value.security_group_ids
    }
  }

  environment {
    variables = var.environment
  }

  tracing_config {
    mode = "PassThrough"
  }

  tags = var.tags

  depends_on = [aws_cloudwatch_log_group.this]
}
