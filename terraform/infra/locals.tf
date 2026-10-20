# -----------------------------------------------------------------------------
# Valores calculados una sola vez y reutilizados en todo el proyecto (DRY).
# -----------------------------------------------------------------------------
locals {
  name_prefix = lower("${var.project_name}-${var.environment}")
  account_id  = data.aws_caller_identity.current.account_id

  # slice() se queda con las primeras N zonas disponibles.
  azs = slice(data.aws_availability_zones.available.names, 0, var.az_count)

  common_tags = {
    Project     = var.project_name
    Environment = var.environment
    ManagedBy   = "Terraform"
    Course      = "82.08 Cloud Computing"
  }

  # ---------------------------------------------------------------------------
  # Red: 3 capas x 2 AZ (ver modules/network)
  # ---------------------------------------------------------------------------
  subnet_tiers = {
    publica = { newbits = 8, netnum_offset = 1, public = true }   # ALB
    app     = { newbits = 8, netnum_offset = 11, public = false } # ECS, Lambdas, endpoints
    datos   = { newbits = 8, netnum_offset = 21, public = false } # RDS
  }

  # ---------------------------------------------------------------------------
  # Security Groups encadenados: las reglas apuntan a otros grupos, no a IPs.
  # ---------------------------------------------------------------------------
  security_groups = {
    alb       = "ALB publico: recibe HTTP desde internet"
    ecs       = "Tasks de ECS (Flask)"
    lambda    = "Lambdas dentro de la VPC"
    rds       = "RDS PostgreSQL"
    endpoints = "VPC Endpoints de tipo interface"
  }

  # Todas las entradas tienen la misma forma para poder usarlas en un for_each.
  sg_ingress_rules = {
    alb_http_internet     = { sg = "alb", port = 80, source_sg = null, cidr = "0.0.0.0/0", description = "HTTP desde internet" }
    ecs_from_alb          = { sg = "ecs", port = var.api_container_port, source_sg = "alb", cidr = null, description = "Flask solo desde el ALB" }
    rds_from_ecs          = { sg = "rds", port = 5432, source_sg = "ecs", cidr = null, description = "PostgreSQL desde ECS" }
    rds_from_lambda       = { sg = "rds", port = 5432, source_sg = "lambda", cidr = null, description = "PostgreSQL desde Lambdas" }
    endpoints_from_ecs    = { sg = "endpoints", port = 443, source_sg = "ecs", cidr = null, description = "HTTPS a endpoints desde ECS" }
    endpoints_from_lambda = { sg = "endpoints", port = 443, source_sg = "lambda", cidr = null, description = "HTTPS a endpoints desde Lambdas" }
  }

  # protocol "-1" = todo el tráfico. ECS y Lambda pueden salir a cualquier
  # destino DENTRO de la VPC: igual no hay ruta a internet.
  sg_egress_rules = {
    alb_to_ecs = { sg = "alb", protocol = "tcp", port = var.api_container_port, dest_sg = "ecs", cidr = null, description = "ALB hacia las tasks" }
    ecs_all    = { sg = "ecs", protocol = "-1", port = null, dest_sg = null, cidr = "0.0.0.0/0", description = "Salida de ECS (sin ruta a internet)" }
    lambda_all = { sg = "lambda", protocol = "-1", port = null, dest_sg = null, cidr = "0.0.0.0/0", description = "Salida de Lambdas (sin ruta a internet)" }
  }

  # ---------------------------------------------------------------------------
  # Buckets (módulo propio s3-bucket)
  # ---------------------------------------------------------------------------
  private_buckets = {
    documentos = { versioning = true, description = "PDFs de los transportistas" }
    auditoria  = { versioning = true, description = "Copia JSON de eventos y reportes CSV" }
  }

  bucket_names = merge(
    { for k, _ in local.private_buckets : k => format("%s-%s-%s", local.name_prefix, k, local.account_id) },
    {
      sitio      = format("%s-sitio-%s", local.name_prefix, local.account_id)
      cloudtrail = format("%s-cloudtrail-%s", local.name_prefix, local.account_id)
    }
  )

  bucket_lifecycle_rules = {
    documentos = [
      {
        id     = "documentos-por-antiguedad"
        prefix = "empresas/"
        transitions = [
          { days = var.documentos_lifecycle.to_standard_ia_days, storage_class = "STANDARD_IA" },
          { days = var.documentos_lifecycle.to_glacier_ir_days, storage_class = "GLACIER_IR" },
        ]
        expiration_days            = var.documentos_lifecycle.expiration_days
        noncurrent_expiration_days = 30
      }
    ]
    auditoria = [
      {
        id          = "eventos-a-glacier"
        prefix      = "eventos/"
        transitions = [{ days = 30, storage_class = "GLACIER" }]
      },
      {
        id              = "reportes-se-borran-al-dia"
        prefix          = "reportes/"
        expiration_days = 1
      }
    ]
  }

  # El navegador sube y baja PDFs directo a S3 con presigned URLs.
  bucket_cors_rules = {
    documentos = [
      {
        allowed_methods = ["GET", "PUT"]
        allowed_origins = [local.site_url]
        allowed_headers = ["*"]
        expose_headers  = ["ETag"]
        max_age_seconds = 3000
      }
    ]
    auditoria = [
      {
        allowed_methods = ["GET"]
        allowed_origins = [local.site_url]
        allowed_headers = ["*"]
        expose_headers  = []
        max_age_seconds = 3000
      }
    ]
  }

  site_url = "http://${module.sitio.website_endpoint}"

  # ---------------------------------------------------------------------------
  # Colas SQS (módulo propio sqs-with-dlq)
  # ---------------------------------------------------------------------------
  # visibility_timeout = 6 x timeout de la Lambda consumidora (recomendación AWS)
  sqs_queues = {
    documentos = {
      consumer       = "validar-documento"
      source_service = "s3.amazonaws.com"
      batch_size     = 5
    }
    auditoria = {
      consumer       = "archivar-auditoria"
      source_service = "sns.amazonaws.com"
      batch_size     = 10
    }
    notificaciones = {
      consumer       = "notificar-mail"
      source_service = "sns.amazonaws.com"
      batch_size     = 10
    }
  }

  # Desde qué recurso puede llegar cada cola (condición aws:SourceArn).
  sqs_source_arns = {
    documentos     = module.buckets["documentos"].arn
    auditoria      = aws_sns_topic.eventos_solicitud.arn
    notificaciones = aws_sns_topic.eventos_solicitud.arn
  }

  # Colas suscriptas al tópico (fan-out).
  sns_subscribed_queues = toset([
    for name, q in local.sqs_queues : name if q.source_service == "sns.amazonaws.com"
  ])

  # ---------------------------------------------------------------------------
  # Lambdas (módulo propio lambda-function, una llamada con for_each)
  # ---------------------------------------------------------------------------
  db_secret_env = {
    DB_SECRET_ARN = aws_secretsmanager_secret.db.arn
  }

  lambdas = {
    auth-callback = {
      description = "Cambia el code de Cognito por tokens y redirige al sitio"
      timeout     = 10
      memory_size = 128
      in_vpc      = false
      environment = {
        COGNITO_DOMAIN           = local.cognito_domain_url
        COGNITO_CLIENT_ID        = aws_cognito_user_pool_client.web.id
        COGNITO_CLIENT_SECRET_ID = aws_secretsmanager_secret.cognito_client.arn
        REDIRECT_URI             = local.cognito_callback_url
        FRONTEND_URL             = local.site_url
      }
    }
    generar-reporte = {
      description = "Consulta RDS, arma el CSV, lo guarda en S3 y devuelve una presigned URL"
      timeout     = 30
      memory_size = 512
      in_vpc      = true
      environment = merge(local.db_secret_env, {
        AUDIT_BUCKET      = local.bucket_names["auditoria"]
        REPORT_PREFIX     = "reportes/"
        PRESIGNED_URL_TTL = tostring(var.presigned_url_ttl_seconds)
      })
    }
    validar-documento = {
      description = "Revisa tipo y tamaño del PDF y actualiza su estado en RDS"
      timeout     = 30
      memory_size = 256
      in_vpc      = true
      environment = merge(local.db_secret_env, {
        DOCS_BUCKET = local.bucket_names["documentos"]
        MAX_SIZE_MB = tostring(var.max_pdf_size_mb)
      })
    }
    vencer-permisos = {
      description = "Marca permisos vencidos, graba auditoría y publica el evento"
      timeout     = 60
      memory_size = 256
      in_vpc      = true
      environment = merge(local.db_secret_env, {
        SNS_TOPIC_ARN = aws_sns_topic.eventos_solicitud.arn
      })
    }
    archivar-auditoria = {
      description = "Guarda la copia JSON de cada evento en el bucket de auditoría"
      timeout     = 30
      memory_size = 128
      in_vpc      = false
      environment = {
        AUDIT_BUCKET = local.bucket_names["auditoria"]
        EVENT_PREFIX = "eventos/"
      }
    }
    gestionar-usuarios = {
      description = "Agrega al usuario al grupo de Cognito que eligio en su primer ingreso"
      timeout     = 10
      memory_size = 128
      in_vpc      = false
      environment = {
        USER_POOL_ID             = aws_cognito_user_pool.main.id
        GRUPOS_PERMITIDOS        = "transportistas,administradores"
        NOTIFICACIONES_TOPIC_ARN = aws_sns_topic.notificaciones_mail.arn
      }
    }
    notificar-mail = {
      description = "Arma el aviso según el evento y lo publica en el tópico SNS de mails"
      timeout     = 30
      memory_size = 128
      in_vpc      = false
      environment = {
        NOTIFICACIONES_TOPIC_ARN = aws_sns_topic.notificaciones_mail.arn
        SITE_URL                 = local.site_url
      }
    }
  }

  # ---------------------------------------------------------------------------
  # Cognito
  # ---------------------------------------------------------------------------
  # Dos roles: transportistas (crean solicitudes) y administradores (personal
  # de planta: aprueban, rechazan, portería, reportes y auditoría).
  cognito_groups = {
    transportistas  = { precedence = 20, description = "Cargan solicitudes de acceso y suben su documentacion" }
    administradores = { precedence = 1, description = "Personal de planta: aprueban, controlan la porteria y ven reportes" }
  }

  cognito_domain_prefix = format("%s-%s", local.name_prefix, local.account_id)
  cognito_domain_url    = "https://${local.cognito_domain_prefix}.auth.${var.region}.amazoncognito.com"
  cognito_callback_url  = "${aws_apigatewayv2_api.auth.api_endpoint}${var.cognito_callback_path}"
  cognito_scopes        = ["openid", "email", "profile"]

  # URL de la Hosted UI. urlencode() escapa la redirect_uri.
  cognito_login_url = format(
    "%s/login?client_id=%s&response_type=code&scope=%s&redirect_uri=%s",
    local.cognito_domain_url,
    aws_cognito_user_pool_client.web.id,
    join("+", local.cognito_scopes),
    urlencode(local.cognito_callback_url)
  )

  # ---------------------------------------------------------------------------
  # Variables de entorno de la API (Flask en ECS)
  # ---------------------------------------------------------------------------
  api_environment = {
    AWS_REGION           = var.region
    APP_ENV              = var.environment
    PORT                 = tostring(var.api_container_port)
    DB_HOST              = aws_db_instance.main.address
    DB_PORT              = tostring(aws_db_instance.main.port)
    DB_NAME              = var.db_name
    DB_USER              = var.db_username
    COGNITO_USER_POOL_ID = aws_cognito_user_pool.main.id
    COGNITO_CLIENT_ID    = aws_cognito_user_pool_client.web.id
    COGNITO_ISSUER       = "https://cognito-idp.${var.region}.amazonaws.com/${aws_cognito_user_pool.main.id}"
    COGNITO_JWKS         = data.http.cognito_jwks.response_body
    SNS_TOPIC_ARN        = aws_sns_topic.eventos_solicitud.arn
    DOCS_BUCKET          = local.bucket_names["documentos"]
    REPORT_LAMBDA_NAME   = module.lambdas["generar-reporte"].function_name
    ROLES_LAMBDA_NAME    = module.lambdas["gestionar-usuarios"].function_name
    PRESIGNED_URL_TTL    = tostring(var.presigned_url_ttl_seconds)
    CORS_ALLOWED_ORIGIN  = local.site_url
    MAX_PDF_SIZE_MB      = tostring(var.max_pdf_size_mb)
  }

  # ---------------------------------------------------------------------------
  # Frontend: tipo MIME según la extensión del archivo
  # ---------------------------------------------------------------------------
  frontend_dir = "${path.module}/../../frontend"

  mime_types = {
    html = "text/html; charset=utf-8"
    css  = "text/css; charset=utf-8"
    js   = "application/javascript; charset=utf-8"
    json = "application/json"
    png  = "image/png"
    jpg  = "image/jpeg"
    svg  = "image/svg+xml"
    ico  = "image/x-icon"
  }

  # fileset() lista los archivos; config.js se genera aparte con templatefile.
  frontend_files = setsubtract(fileset(local.frontend_dir, "**"), ["config.js.tftpl"])
}
