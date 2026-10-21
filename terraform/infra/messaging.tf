# =============================================================================
# EVENTOS: SNS (fan-out) -> SQS (con DLQ) -> Lambdas, y SNS por mail para
# las notificaciones a las personas.
# =============================================================================

# El outbox publica cada evento con ID estable; SNS lo reparte a las colas.
resource "aws_sns_topic" "eventos_solicitud" {
  name              = "${local.name_prefix}-eventos-solicitud"
  kms_master_key_id = aws_kms_key.main.arn
}

# -----------------------------------------------------------------------------
# Colas: módulo propio sqs-with-dlq, una instancia por cola.
# -----------------------------------------------------------------------------
module "sqs" {
  source   = "../modules/sqs-with-dlq"
  for_each = local.sqs_queues

  name = "${local.name_prefix}-cola-${each.key}"

  # 6 x el timeout de la Lambda que la consume.
  visibility_timeout_seconds = 6 * local.lambdas[each.value.consumer].timeout
  max_receive_count          = 3
  kms_key_id                 = aws_kms_key.main.arn
  alarm_actions              = [aws_sns_topic.alarmas.arn]
}

# Policy de cada cola: solo puede escribir el servicio que le corresponde
# (S3 para documentos, SNS para auditoría y notificaciones) y solo desde
# nuestro bucket / tópico.
resource "aws_sqs_queue_policy" "this" {
  for_each = local.sqs_queues

  queue_url = module.sqs[each.key].queue_url

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "Allow${title(replace(each.value.source_service, ".amazonaws.com", ""))}SendMessage"
        Effect    = "Allow"
        Principal = { Service = each.value.source_service }
        Action    = "sqs:SendMessage"
        Resource  = module.sqs[each.key].queue_arn
        Condition = {
          ArnEquals    = { "aws:SourceArn" = local.sqs_source_arns[each.key] }
          StringEquals = { "aws:SourceAccount" = local.account_id }
        }
      }
    ]
  })
}

# Fan-out: el tópico copia cada evento a auditoría y a notificaciones.
resource "aws_sns_topic_subscription" "colas" {
  for_each = local.sns_subscribed_queues

  topic_arn            = aws_sns_topic.eventos_solicitud.arn
  protocol             = "sqs"
  endpoint             = module.sqs[each.key].queue_arn
  raw_message_delivery = true
  redrive_policy       = jsonencode({ deadLetterTargetArn = aws_sqs_queue.entrega_eventos_dlq[each.key].arn })

  # Sin la policy de la cola, SNS no puede entregar y los mensajes se pierden.
  depends_on = [aws_sqs_queue_policy.this, aws_sqs_queue_policy.entrega_eventos_dlq]
}

# -----------------------------------------------------------------------------
# Notificaciones por mail con SNS (reemplaza a SES: en el Learner Lab el
# LabRole no puede verificar identidades de SES, así que SES no puede mandar).
#
# gestionar-usuarios administra email + filtro destinatario_id desde Cognito.
# Cada dirección debe confirmar. El correo nunca se usa como identificador.
# -----------------------------------------------------------------------------
resource "aws_sns_topic" "notificaciones_mail" {
  name              = "${local.name_prefix}-notificaciones-mail"
  kms_master_key_id = aws_kms_key.main.arn
}

# Terraform deja de administrar las suscripciones existentes, sin borrarlas.
# La Lambda migra sus filtros al ID verificado. Evita dos administradores
# que se pisen entre sí (Terraform y cambios de rol en tiempo de ejecución).
removed {
  from = aws_sns_topic_subscription.notificaciones_mail
  lifecycle {
    destroy = false
  }
}

# Si SNS no puede entregar a SQS, la DLQ de procesamiento no alcanza:
# hace falta una DLQ de entrega en la suscripción SNS.
resource "aws_sqs_queue" "entrega_eventos_dlq" {
  for_each                  = local.sns_subscribed_queues
  name                      = "${local.name_prefix}-entrega-${each.key}-dlq"
  message_retention_seconds = 1209600
  kms_master_key_id         = aws_kms_key.main.arn
}

resource "aws_sqs_queue_policy" "entrega_eventos_dlq" {
  for_each  = local.sns_subscribed_queues
  queue_url = aws_sqs_queue.entrega_eventos_dlq[each.key].url
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "sns.amazonaws.com" }
      Action    = "sqs:SendMessage"
      Resource  = aws_sqs_queue.entrega_eventos_dlq[each.key].arn
      Condition = { ArnEquals = { "aws:SourceArn" = aws_sns_topic.eventos_solicitud.arn } }
    }]
  })
}

resource "aws_sqs_queue" "entrega_mail_dlq" {
  name                      = "${local.name_prefix}-entrega-mail-dlq"
  message_retention_seconds = 1209600
  kms_master_key_id         = aws_kms_key.main.arn
}

resource "aws_sqs_queue_policy" "entrega_mail_dlq" {
  queue_url = aws_sqs_queue.entrega_mail_dlq.url
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "sns.amazonaws.com" }
      Action    = "sqs:SendMessage"
      Resource  = aws_sqs_queue.entrega_mail_dlq.arn
      Condition = { ArnEquals = { "aws:SourceArn" = aws_sns_topic.notificaciones_mail.arn } }
    }]
  })
}
