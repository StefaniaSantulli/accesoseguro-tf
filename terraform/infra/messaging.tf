# =============================================================================
# EVENTOS: SNS (fan-out) -> SQS (con DLQ) -> Lambdas, y SNS por mail para
# las notificaciones a las personas.
# =============================================================================

# Flask publica cada evento una sola vez y SNS lo reparte a las colas.
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

  # Sin la policy de la cola, SNS no puede entregar y los mensajes se pierden.
  depends_on = [aws_sqs_queue_policy.this]
}

# -----------------------------------------------------------------------------
# Notificaciones por mail con SNS (reemplaza a SES: en el Learner Lab el
# LabRole no puede verificar identidades de SES, así que SES no puede mandar).
#
# Cada dirección se suscribe al tópico y tiene que CONFIRMAR la suscripción con
# el link que le manda AWS. La Lambda notificar-mail publica cada aviso con el
# atributo "destinatario", y la filter policy de cada suscripción hace que cada
# persona reciba solo lo suyo:
#   - transportista: solo los avisos con su propio mail
#   - personal:      los suyos + los dirigidos a "personal" (solicitud nueva)
# -----------------------------------------------------------------------------
resource "aws_sns_topic" "notificaciones_mail" {
  name              = "${local.name_prefix}-notificaciones-mail"
  kms_master_key_id = aws_kms_key.main.arn
}

resource "aws_sns_topic_subscription" "notificaciones_mail" {
  for_each = var.notificaciones_suscriptores

  topic_arn           = aws_sns_topic.notificaciones_mail.arn
  protocol            = "email"
  endpoint            = each.key
  filter_policy_scope = "MessageAttributes"
  filter_policy = jsonencode({
    destinatario = concat([lower(each.key)], each.value == "personal" ? ["personal"] : [])
  })
}
