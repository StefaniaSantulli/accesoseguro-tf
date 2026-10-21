output "sitio_url" {
  description = "URL del sitio estático (S3)."
  value       = local.site_url
}

output "api_url" {
  description = "URL de la API (ALB)."
  value       = "http://${aws_lb.api.dns_name}"
}

output "login_url" {
  description = "URL de la Hosted UI de Cognito para ingresar."
  value       = local.cognito_login_url
}

output "cognito_callback_url" {
  description = "Ruta HTTPS del API Gateway donde vuelve Cognito."
  value       = local.cognito_callback_url
}

output "cognito_user_pool_id" {
  description = "ID del User Pool."
  value       = aws_cognito_user_pool.main.id
}

output "cognito_client_id" {
  description = "ID del app client web."
  value       = aws_cognito_user_pool_client.web.id
}

output "ecr_repository_url" {
  description = "Repositorio donde se sube la imagen de la API."
  value       = module.ecr.repository_url
}

output "ecs_cluster_name" {
  description = "Cluster de ECS."
  value       = aws_ecs_cluster.main.name
}

output "ecs_service_name" {
  description = "Servicio de ECS."
  value       = aws_ecs_service.api.name
}

output "rds_endpoint" {
  description = "Host de RDS (solo accesible desde la VPC)."
  value       = aws_db_instance.main.address
}

output "db_secret_arn" {
  description = "Secreto con las credenciales de la base."
  value       = aws_secretsmanager_secret.db.arn
}

output "buckets" {
  description = "Nombre de cada bucket."
  value       = local.bucket_names
}

output "sns_topic_arn" {
  description = "Tópico de eventos de solicitud."
  value       = aws_sns_topic.eventos_solicitud.arn
}

output "sqs_queues" {
  description = "URL de cada cola y de su DLQ."
  value = {
    for name, q in module.sqs : name => {
      url = q.queue_url
      dlq = q.dlq_name
    }
  }
}

output "lambda_functions" {
  description = "Nombre de cada Lambda."
  value       = { for k, m in module.lambdas : k => m.function_name }
}

output "network" {
  description = "VPC y subredes creadas."
  value = {
    vpc_id  = module.network.vpc_id
    subnets = module.network.subnets
  }
}

output "notificaciones_topic_arn" {
  description = "Tópico SNS que manda las notificaciones por mail."
  value       = aws_sns_topic.notificaciones_mail.arn
}

output "notificaciones_dlq" {
  description = "Colas de fallos de entrega SNS; revisar antes de reintentar."
  value       = { for name, queue in aws_sqs_queue.entrega_dlq : name => queue.url }
}

output "deployment" {
  description = "Datos para ejecutar la inicialización en la misma red y task definition de la API."
  value = {
    task_definition = aws_ecs_task_definition.api.arn
    desired_count   = var.api_min_tasks
    network_configuration = {
      awsvpcConfiguration = {
        subnets        = module.network.subnet_ids_by_tier["app"]
        securityGroups = [aws_security_group.this["ecs"].id]
        assignPublicIp = "DISABLED"
      }
    }
    worker_mappings = [for key, mapping in aws_lambda_event_source_mapping.sqs : mapping.uuid if key != "auditoria"]
    expiration_rule = aws_cloudwatch_event_rule.vencer_permisos.name
  }
}
