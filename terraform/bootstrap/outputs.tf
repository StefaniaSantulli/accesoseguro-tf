output "state_bucket_name" {
  description = "Bucket donde vive el state del Terraform principal."
  value       = aws_s3_bucket.tfstate.bucket
}

output "state_bucket_arn" {
  description = "ARN del bucket del state."
  value       = aws_s3_bucket.tfstate.arn
}

output "infra_state_key" {
  description = "Key del state de infra/ dentro del bucket."
  value       = local.infra_state_key
}

output "lock_table_name" {
  description = "Tabla DynamoDB de locks (null si no se creó)."
  value       = one(aws_dynamodb_table.tf_locks[*].name)
}

output "next_step" {
  description = "Qué hacer después."
  value       = "cd ../infra && terraform init -backend-config=backend.hcl"
}
