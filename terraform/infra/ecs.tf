# =============================================================================
# CÓMPUTO SINCRÓNICO: ECR + ECS Fargate (Flask) + Auto Scaling.
# =============================================================================

# -----------------------------------------------------------------------------
# ECR: registro de imágenes. Módulo EXTERNO del Terraform Registry
# (terraform-aws-modules/ecr/aws). Tags inmutables: v1.4 siempre es la misma
# imagen. Se guardan solo las últimas 10 versiones (alcanza para un rollback).
# -----------------------------------------------------------------------------
module "ecr" {
  source  = "terraform-aws-modules/ecr/aws"
  version = "~> 2.0"

  repository_name                 = "${local.name_prefix}-api"
  repository_type                 = "private"
  repository_image_tag_mutability = "IMMUTABLE"
  repository_image_scan_on_push   = true
  repository_encryption_type      = "KMS"
  repository_kms_key              = aws_kms_key.main.arn
  repository_force_delete         = var.force_destroy_buckets

  # Sin repository policy: ECS baja la imagen con LabRole (permiso IAM).
  create_repository_policy = false
  attach_repository_policy = false

  create_lifecycle_policy = true
  repository_lifecycle_policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Conservar las ultimas 10 imagenes"
        selection = {
          tagStatus   = "any"
          countType   = "imageCountMoreThan"
          countNumber = 10
        }
        action = { type = "expire" }
      }
    ]
  })
}

# -----------------------------------------------------------------------------
# ECS
# -----------------------------------------------------------------------------
resource "aws_ecs_cluster" "main" {
  name = "${local.name_prefix}-cluster"

  setting {
    name  = "containerInsights"
    value = "enabled"
  }
}

resource "aws_ecs_cluster_capacity_providers" "main" {
  cluster_name       = aws_ecs_cluster.main.name
  capacity_providers = ["FARGATE"]

  default_capacity_provider_strategy {
    capacity_provider = "FARGATE"
    weight            = 1
  }
}

resource "aws_cloudwatch_log_group" "api" {
  name              = "/ecs/${local.name_prefix}-api"
  retention_in_days = var.log_retention_days
}

resource "aws_ecs_task_definition" "api" {
  family                   = "${local.name_prefix}-api"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.api_cpu
  memory                   = var.api_memory

  # LabRole hace de execution role (bajar la imagen, leer el secreto, escribir
  # logs) y de task role (lo que llama Flask: SNS, S3, Lambda).
  execution_role_arn = data.aws_iam_role.lab_role.arn
  task_role_arn      = data.aws_iam_role.lab_role.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }

  container_definitions = jsonencode([
    {
      name      = "api"
      image     = "${module.ecr.repository_url}:${var.api_image_tag}"
      essential = true

      portMappings = [
        { containerPort = var.api_container_port, protocol = "tcp" }
      ]

      # Mapa -> lista de {name, value}, el formato que pide ECS.
      environment = [
        for name in sort(keys(local.api_environment)) : {
          name  = name
          value = local.api_environment[name]
        }
      ]

      # La contraseña la inyecta ECS desde Secrets Manager al arrancar la task.
      secrets = [
        { name = "DB_PASSWORD", valueFrom = "${aws_secretsmanager_secret.db.arn}:password::" }
      ]

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          awslogs-group         = aws_cloudwatch_log_group.api.name
          awslogs-region        = var.region
          awslogs-stream-prefix = "api"
        }
      }
    }
  ])
}

resource "aws_ecs_service" "api" {
  name            = "${local.name_prefix}-api"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.api.arn
  desired_count   = var.deployment_ready ? var.api_min_tasks : 0
  launch_type     = "FARGATE"

  # Rolling update: nunca baja de las tasks sanas que hay (100%) y puede
  # levantar el doble mientras despliega (200%).
  deployment_minimum_healthy_percent = 100
  deployment_maximum_percent         = 200
  health_check_grace_period_seconds  = 60

  network_configuration {
    subnets          = module.network.subnet_ids_by_tier["app"]
    security_groups  = [aws_security_group.this["ecs"].id]
    assign_public_ip = false
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.api.arn
    container_name   = "api"
    container_port   = var.api_container_port
  }

  lifecycle {
    # El Auto Scaling cambia desired_count. Sin esto, cada apply lo volvería
    # al mínimo y pelearía con el escalado.
    ignore_changes = [desired_count]
  }

  # ECS rechaza el servicio si el target group todavía no está asociado a un
  # listener del ALB. Terraform no ve esa relación: se declara a mano.
  # Los endpoints tienen que existir para poder bajar la imagen sin internet.
  depends_on = [
    aws_lb_listener.http,
    aws_vpc_endpoint.interface,
    aws_vpc_endpoint.s3,
    aws_secretsmanager_secret_version.db,
  ]
}

# -----------------------------------------------------------------------------
# Auto Scaling: de 2 a 4 tasks, mantiene la CPU promedio cerca del 50%.
# -----------------------------------------------------------------------------
resource "aws_appautoscaling_target" "api" {
  service_namespace  = "ecs"
  resource_id        = "service/${aws_ecs_cluster.main.name}/${aws_ecs_service.api.name}"
  scalable_dimension = "ecs:service:DesiredCount"
  min_capacity       = var.deployment_ready ? var.api_min_tasks : 0
  max_capacity       = max(var.api_max_tasks, var.api_min_tasks)

  suspended_state {
    dynamic_scaling_in_suspended  = !var.deployment_ready
    dynamic_scaling_out_suspended = !var.deployment_ready
    scheduled_scaling_suspended   = !var.deployment_ready
  }
}

resource "aws_appautoscaling_policy" "api_cpu" {
  name               = "${local.name_prefix}-api-cpu-${var.api_cpu_target}"
  policy_type        = "TargetTrackingScaling"
  service_namespace  = aws_appautoscaling_target.api.service_namespace
  resource_id        = aws_appautoscaling_target.api.resource_id
  scalable_dimension = aws_appautoscaling_target.api.scalable_dimension

  target_tracking_scaling_policy_configuration {
    target_value       = var.api_cpu_target
    scale_out_cooldown = 60
    scale_in_cooldown  = 300

    predefined_metric_specification {
      predefined_metric_type = "ECSServiceAverageCPUUtilization"
    }
  }
}
