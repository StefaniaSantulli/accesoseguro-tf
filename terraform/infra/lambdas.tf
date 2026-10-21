# =============================================================================
# CÓMPUTO ASINCRÓNICO: las 7 Lambdas con UNA sola llamada al módulo propio.
# =============================================================================

module "lambdas" {
  source   = "../modules/lambda-function"
  for_each = local.lambdas

  function_name = "${local.name_prefix}-${each.key}"
  description   = each.value.description
  source_dir    = "${path.module}/../../lambdas/${each.key}"
  build_dir     = "${path.module}/.build"
  runtime       = var.lambda_runtime
  timeout       = each.value.timeout
  memory_size   = each.value.memory_size
  role_arn      = data.aws_iam_role.lab_role.arn

  # Las que usan RDS reciben el secreto de la base y la Layer (driver + código
  # compartido), y corren dentro de la VPC (subredes de app). Las demás, fuera.
  environment = merge(each.value.usa_rds ? local.db_secret_env : {}, each.value.environment)
  layers      = each.value.usa_rds ? [aws_lambda_layer_version.notificaciones.arn] : []
  vpc_config = each.value.usa_rds ? {
    subnet_ids         = module.network.subnet_ids_by_tier["app"]
    security_group_ids = [aws_security_group.this["lambda"].id]
  } : null

  log_retention_days = var.log_retention_days
}

# -----------------------------------------------------------------------------
# Disparadores SQS -> Lambda (validar-documento, archivar-auditoria,
# notificar-mail). Lambda hace el polling de la cola.
# -----------------------------------------------------------------------------
resource "aws_lambda_event_source_mapping" "sqs" {
  for_each = local.sqs_queues

  event_source_arn = module.sqs[each.key].queue_arn
  function_name    = module.lambdas[each.value.consumer].arn
  batch_size       = each.value.batch_size
  enabled          = each.key == "auditoria" ? true : var.deployment_ready

  # Si falla un mensaje del lote, solo ese vuelve a la cola.
  function_response_types = ["ReportBatchItemFailures"]
  dynamic "scaling_config" {
    for_each = each.key == "notificaciones" ? [1] : []
    content {
      maximum_concurrency = 2
    }
  }
}

# -----------------------------------------------------------------------------
# EventBridge: vencimientos y recuperación periódica de avisos pendientes.
# -----------------------------------------------------------------------------
resource "aws_cloudwatch_event_rule" "vencer_permisos" {
  name                = "${local.name_prefix}-vencer-permisos"
  description         = "Dispara vencer-permisos y republica eventos pendientes"
  schedule_expression = local.vencer_permisos_schedule
  state               = var.deployment_ready ? "ENABLED" : "DISABLED"
}

resource "aws_lambda_layer_version" "notificaciones" {
  layer_name               = "${local.name_prefix}-notificaciones"
  filename                 = "${path.module}/.build/notificaciones-layer.zip"
  source_code_hash         = fileexists("${path.module}/.build/notificaciones-layer.zip") ? filebase64sha256("${path.module}/.build/notificaciones-layer.zip") : null
  compatible_runtimes      = ["python3.12"]
  compatible_architectures = ["x86_64"]

  lifecycle {
    precondition {
      condition = try(trimspace(file("${path.module}/.build/notificaciones-layer.sha256")) == sha256(join("", [
        filesha256("${path.module}/../../backend/notifications.py"),
        filesha256("${path.module}/../../backend/notification_jobs.py"),
        filesha256("${path.module}/../../backend/application_jobs.py"),
        filesha256("${path.module}/../../lambdas/layers/notificaciones/requirements.txt"),
      ])), false) && var.lambda_runtime == "python3.12"
      error_message = "Ejecutar make layer antes de planificar; la Layer requiere Python 3.12/x86_64 y fuentes actualizadas."
    }
  }
}

resource "aws_cloudwatch_event_target" "vencer_permisos" {
  rule      = aws_cloudwatch_event_rule.vencer_permisos.name
  target_id = "vencer-permisos"
  arn       = module.lambdas["vencer-permisos"].arn

  retry_policy {
    maximum_retry_attempts       = 2
    maximum_event_age_in_seconds = 900
  }
}

resource "aws_lambda_permission" "eventbridge_vencer_permisos" {
  statement_id  = "AllowEventBridgeInvoke"
  action        = "lambda:InvokeFunction"
  function_name = module.lambdas["vencer-permisos"].function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.vencer_permisos.arn
}
