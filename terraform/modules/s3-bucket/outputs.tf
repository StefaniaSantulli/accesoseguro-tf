output "id" {
  description = "Nombre del bucket (disponible recién cuando el bucket existe)."
  value       = terraform_data.bucket.output.name
}

output "arn" {
  description = "ARN del bucket."
  value       = local.arn
}

output "website_endpoint" {
  description = "Endpoint HTTP del sitio (null si no es website)."
  value       = one(aws_s3_bucket_website_configuration.this[*].website_endpoint)
}
