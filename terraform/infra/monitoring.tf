# =============================================================================
# OPERACIÓN: CloudWatch (alarmas). Los logs están en ecs.tf y en el módulo de
# Lambda; las alarmas de las DLQ, en el módulo sqs-with-dlq.
# =============================================================================

# Tópico propio para alarmas (separado del de negocio).
resource "aws_sns_topic" "alarmas" {
  name              = "${local.name_prefix}-alarmas"
  kms_master_key_id = aws_kms_key.main.arn
}

# count: la suscripción por mail existe solo si se configuró una dirección.
resource "aws_sns_topic_subscription" "alarmas_email" {
  count = var.alarm_email == "" ? 0 : 1

  topic_arn = aws_sns_topic.alarmas.arn
  protocol  = "email"
  endpoint  = var.alarm_email
}

# Errores 5xx devueltos por Flask.
resource "aws_cloudwatch_metric_alarm" "alb_5xx" {
  alarm_name          = "${local.name_prefix}-api-5xx"
  alarm_description   = "La API devolvió más de 10 errores 5xx en 5 minutos"
  namespace           = "AWS/ApplicationELB"
  metric_name         = "HTTPCode_Target_5XX_Count"
  dimensions          = { LoadBalancer = aws_lb.api.arn_suffix }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 10
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alarmas.arn]
}

# Ninguna task sana detrás del ALB = la API está caída.
resource "aws_cloudwatch_metric_alarm" "alb_sin_tasks_sanas" {
  alarm_name        = "${local.name_prefix}-api-sin-tasks-sanas"
  alarm_description = "El target group no tiene tasks sanas"
  namespace         = "AWS/ApplicationELB"
  metric_name       = "HealthyHostCount"
  dimensions = {
    LoadBalancer = aws_lb.api.arn_suffix
    TargetGroup  = aws_lb_target_group.api.arn_suffix
  }
  statistic           = "Minimum"
  period              = 60
  evaluation_periods  = 3
  threshold           = 1
  comparison_operator = "LessThanThreshold"
  treat_missing_data  = "breaching"
  alarm_actions       = [aws_sns_topic.alarmas.arn]
}

# Errores en las Lambdas asincrónicas: nadie espera su respuesta, así que si
# fallan nadie se entera salvo por la alarma.
resource "aws_cloudwatch_metric_alarm" "lambda_errors" {
  for_each = toset(["validar-documento", "vencer-permisos", "archivar-auditoria", "notificar-mail"])

  alarm_name          = "${local.name_prefix}-${each.key}-errores"
  alarm_description   = "La Lambda ${each.key} tuvo errores"
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  dimensions          = { FunctionName = module.lambdas[each.key].function_name }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alarmas.arn]
}

# Almacenamiento libre de RDS por debajo de 2 GB.
resource "aws_cloudwatch_metric_alarm" "rds_storage" {
  alarm_name          = "${local.name_prefix}-rds-poco-espacio"
  alarm_description   = "RDS tiene menos de 2 GB libres"
  namespace           = "AWS/RDS"
  metric_name         = "FreeStorageSpace"
  dimensions          = { DBInstanceIdentifier = aws_db_instance.main.identifier }
  statistic           = "Minimum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 2 * 1024 * 1024 * 1024
  comparison_operator = "LessThanThreshold"
  alarm_actions       = [aws_sns_topic.alarmas.arn]
}

# vencer-permisos publica EdadPendiente una vez por ejecución: el período sigue
# a su frecuencia y hacen falta dos períodos seguidos para alarmar.
resource "aws_cloudwatch_metric_alarm" "outbox_atrasado" {
  alarm_name          = "${local.name_prefix}-avisos-pendientes"
  namespace           = "AccesoSeguro/Notificaciones"
  metric_name         = "EdadPendiente"
  dimensions          = { FunctionName = module.lambdas["vencer-permisos"].function_name }
  statistic           = "Maximum"
  period              = var.vencer_permisos_intervalo_minutos * 60
  evaluation_periods  = 2
  datapoints_to_alarm = 2
  threshold           = local.outbox_edad_maxima_segundos
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "breaching"
  alarm_description   = "Hay eventos sin publicar hace más de ${local.outbox_edad_maxima_segundos / 60} minutos o el recuperador no emite métricas."
  alarm_actions       = [aws_sns_topic.alarmas.arn]
}

# Lambda Errors no cuenta respuestas exitosas con batchItemFailures.
resource "aws_cloudwatch_log_metric_filter" "avisos_fallidos" {
  name           = "${local.name_prefix}-avisos-fallidos"
  log_group_name = "/aws/lambda/${module.lambdas["notificar-mail"].function_name}"
  pattern        = "{ $.msg = \"aviso_fallido\" }"
  metric_transformation {
    name      = "AvisosFallidos-${local.name_prefix}"
    namespace = "AccesoSeguro/Notificaciones"
    value     = "1"
  }
}

resource "aws_cloudwatch_metric_alarm" "avisos_fallidos" {
  alarm_name          = "${local.name_prefix}-avisos-fallidos"
  namespace           = "AccesoSeguro/Notificaciones"
  metric_name         = aws_cloudwatch_log_metric_filter.avisos_fallidos.metric_transformation[0].name
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alarmas.arn]
}

# Cualquier mensaje en una DLQ de entrega SNS (auditoría, notificaciones o mail).
resource "aws_cloudwatch_metric_alarm" "entrega_dlq" {
  for_each            = local.sns_delivery_dlqs
  alarm_name          = "${local.name_prefix}-entrega-${each.key}-fallida"
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  dimensions          = { QueueName = aws_sqs_queue.entrega_dlq[each.key].name }
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alarmas.arn]
}

moved {
  from = aws_cloudwatch_metric_alarm.entrega_eventos_dlq["auditoria"]
  to   = aws_cloudwatch_metric_alarm.entrega_dlq["auditoria"]
}
moved {
  from = aws_cloudwatch_metric_alarm.entrega_eventos_dlq["notificaciones"]
  to   = aws_cloudwatch_metric_alarm.entrega_dlq["notificaciones"]
}
moved {
  from = aws_cloudwatch_metric_alarm.entrega_mail_dlq
  to   = aws_cloudwatch_metric_alarm.entrega_dlq["mail"]
}
