# =============================================================================
# ENTRADA: Application Load Balancer, la única puerta de la API.
# =============================================================================

resource "aws_lb" "api" {
  name               = substr("${local.name_prefix}-alb", 0, 32) # máx. 32 caracteres
  load_balancer_type = "application"
  internal           = false
  security_groups    = [aws_security_group.this["alb"].id]
  subnets            = module.network.subnet_ids_by_tier["publica"]

  idle_timeout               = 60
  drop_invalid_header_fields = true
  enable_deletion_protection = false
}

# Target group de tipo "ip": con Fargate cada task tiene su propia ENI.
resource "aws_lb_target_group" "api" {
  name        = substr("${local.name_prefix}-api-tg", 0, 32)
  port        = var.api_container_port
  protocol    = "HTTP"
  target_type = "ip"
  vpc_id      = module.network.vpc_id

  deregistration_delay = 30

  health_check {
    path                = var.api_health_check_path
    matcher             = "200"
    interval            = 15
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }
}

# Listener HTTP:80. En producción: listener 443 con certificado de ACM y
# dominio en Route 53, y el 80 redirigiendo al 443.
resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.api.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.api.arn
  }
}
