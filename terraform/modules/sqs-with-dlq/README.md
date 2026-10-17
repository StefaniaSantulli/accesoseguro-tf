# Módulo `sqs-with-dlq` (propio)

Cola SQS Standard + dead-letter queue + alarma de CloudWatch cuando la DLQ tiene mensajes.

- `redrive_policy`: después de `max_receive_count` fallos (default 3) el mensaje pasa a la DLQ.
- `aws_sqs_queue_redrive_allow_policy`: solo esta cola puede usar la DLQ.
- Long polling (`receive_wait_time_seconds = 20`) y cifrado con KMS.
- La policy de acceso de la cola (quién puede escribir) la define quien llama al módulo,
  porque depende del productor (S3 o SNS).

## Entradas principales
`name`, `visibility_timeout_seconds` (usar 6 x timeout de la Lambda), `max_receive_count`,
`message_retention_seconds`, `dlq_retention_seconds`, `kms_key_id`, `alarm_actions`, `tags`.

## Salidas
`queue_arn`, `queue_url`, `queue_name`, `dlq_arn`, `dlq_name`.
