# =============================================================================
# CÓMPUTO ASINCRÓNICO: las 6 Lambdas con UNA sola llamada al módulo propio.
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
  environment   = each.value.environment

  # Solo las que necesitan RDS van dentro de la VPC (subredes de app).
  vpc_config = each.value.in_vpc ? {
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
  enabled          = true

  # Si falla un mensaje del lote, solo ese vuelve a la cola.
  function_response_types = ["ReportBatchItemFailures"]
}

# -----------------------------------------------------------------------------
# EventBridge: vencer-permisos cada 15 minutos (cron administrado).
# -----------------------------------------------------------------------------
resource "aws_cloudwatch_event_rule" "vencer_permisos" {
  name                = "${local.name_prefix}-vencer-permisos"
  description         = "Dispara vencer-permisos y republica eventos pendientes"
  schedule_expression = var.vencer_permisos_schedule
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
