# -----------------------------------------------------------------------------
# Módulo propio: cola SQS Standard + su dead-letter queue + alarma.
# Patrón que se repite 3 veces en AccesoSeguro (documentos, auditoría y
# notificaciones), por eso va en un módulo.
# -----------------------------------------------------------------------------

# Cola donde caen los mensajes que fallaron max_receive_count veces.
resource "aws_sqs_queue" "dlq" {
  name                      = "${var.name}-dlq"
  message_retention_seconds = var.dlq_retention_seconds
  kms_master_key_id         = var.kms_key_id
  tags                      = var.tags
}

resource "aws_sqs_queue" "this" {
  name                       = var.name
  visibility_timeout_seconds = var.visibility_timeout_seconds
  message_retention_seconds  = var.message_retention_seconds
  receive_wait_time_seconds  = var.receive_wait_time_seconds # long polling
  kms_master_key_id          = var.kms_key_id

  # Después de N fallos el mensaje pasa a la DLQ en vez de reintentarse para
  # siempre.
  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.dlq.arn
    maxReceiveCount     = var.max_receive_count
  })

  tags = var.tags
}

# Solo esta cola puede usar la DLQ como destino.
resource "aws_sqs_queue_redrive_allow_policy" "dlq" {
  queue_url = aws_sqs_queue.dlq.id

  redrive_allow_policy = jsonencode({
    redrivePermission = "byQueue"
    sourceQueueArns   = [aws_sqs_queue.this.arn]
  })
}

# Si aparece cualquier mensaje en la DLQ, algo falló de forma persistente.
resource "aws_cloudwatch_metric_alarm" "dlq_not_empty" {
  alarm_name          = "${var.name}-dlq-con-mensajes"
  alarm_description   = "Hay mensajes en ${aws_sqs_queue.dlq.name}: el consumidor falló ${var.max_receive_count} veces."
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  dimensions          = { QueueName = aws_sqs_queue.dlq.name }
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  comparison_operator = "GreaterThanThreshold"
  threshold           = 0
  treat_missing_data  = "notBreaching"
  alarm_actions       = var.alarm_actions
  ok_actions          = var.alarm_actions
  tags                = var.tags
}
