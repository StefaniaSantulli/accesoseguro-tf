output "queue_arn" {
  description = "ARN de la cola principal."
  value       = aws_sqs_queue.this.arn
}

output "queue_url" {
  description = "URL de la cola principal."
  value       = aws_sqs_queue.this.id
}

output "queue_name" {
  description = "Nombre de la cola principal."
  value       = aws_sqs_queue.this.name
}

output "dlq_arn" {
  description = "ARN de la DLQ."
  value       = aws_sqs_queue.dlq.arn
}

output "dlq_name" {
  description = "Nombre de la DLQ."
  value       = aws_sqs_queue.dlq.name
}
