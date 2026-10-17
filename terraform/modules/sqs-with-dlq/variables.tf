variable "name" {
  description = "Nombre de la cola (la DLQ se llama <name>-dlq)."
  type        = string
}

variable "visibility_timeout_seconds" {
  description = "Tiempo que el mensaje queda invisible mientras se procesa. AWS recomienda 6 veces el timeout de la Lambda consumidora."
  type        = number
  default     = 180
}

variable "message_retention_seconds" {
  description = "Cuánto vive un mensaje sin procesar (default 4 días)."
  type        = number
  default     = 345600
}

variable "receive_wait_time_seconds" {
  description = "Long polling (0 a 20 segundos)."
  type        = number
  default     = 20
}

variable "max_receive_count" {
  description = "Intentos antes de mandar el mensaje a la DLQ."
  type        = number
  default     = 3
}

variable "dlq_retention_seconds" {
  description = "Retención de la DLQ (default 14 días, el máximo)."
  type        = number
  default     = 1209600
}

variable "kms_key_id" {
  description = "Clave KMS para cifrar los mensajes (ARN o alias)."
  type        = string
  default     = "alias/aws/sqs"
}

variable "alarm_actions" {
  description = "ARNs (por ejemplo un tópico SNS) a notificar cuando la DLQ tiene mensajes."
  type        = list(string)
  default     = []
}

variable "tags" {
  description = "Tags adicionales."
  type        = map(string)
  default     = {}
}
