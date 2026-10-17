output "function_name" {
  description = "Nombre de la función."
  value       = aws_lambda_function.this.function_name
}

output "arn" {
  description = "ARN de la función."
  value       = aws_lambda_function.this.arn
}

output "invoke_arn" {
  description = "ARN de invocación (lo usa API Gateway)."
  value       = aws_lambda_function.this.invoke_arn
}

output "log_group_name" {
  description = "Log group de CloudWatch."
  value       = aws_cloudwatch_log_group.this.name
}
