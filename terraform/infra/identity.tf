# =============================================================================
# IDENTIDAD: Cognito User Pool + Hosted UI + puente HTTPS de login.
# Cognito autentica (quién es), API Gateway valida el token y Flask autoriza
# (qué puede hacer según el grupo y la sede que trae el token).
# =============================================================================

resource "aws_cognito_user_pool" "main" {
  name = "${local.name_prefix}-usuarios"

  # Login con mail; Cognito verifica el mail en el registro.
  username_attributes      = ["email"]
  auto_verified_attributes = ["email"]

  username_configuration {
    case_sensitive = false
  }

  password_policy {
    minimum_length                   = 8
    require_lowercase                = true
    require_uppercase                = true
    require_numbers                  = true
    require_symbols                  = false
    temporary_password_validity_days = 7
  }

  # Los transportistas se registran solos en la Hosted UI.
  admin_create_user_config {
    allow_admin_create_user_only = false
  }

  account_recovery_setting {
    recovery_mechanism {
      name     = "verified_email"
      priority = 1
    }
  }

  # Empresa del usuario. La asigna un administrador y viaja dentro del token.
  schema {
    name                     = "empresa_id"
    attribute_data_type      = "String"
    mutable                  = true
    developer_only_attribute = false

    string_attribute_constraints {
      min_length = 1
      max_length = 64
    }
  }

  # Sede del responsable o de la portería (la asigna un administrador).
  schema {
    name                     = "sede_id"
    attribute_data_type      = "String"
    mutable                  = true
    developer_only_attribute = false

    string_attribute_constraints {
      min_length = 1
      max_length = 64
    }
  }

  lifecycle {
    # Cognito no permite modificar atributos del schema una vez creados: si
    # Terraform detecta una diferencia intentaría recrear el pool y se
    # perderían todos los usuarios.
    ignore_changes = [schema]
  }
}

# Grupos: viajan en el token (claim cognito:groups) y Flask los usa para autorizar.
resource "aws_cognito_user_group" "this" {
  for_each = local.cognito_groups

  user_pool_id = aws_cognito_user_pool.main.id
  name         = each.key
  description  = each.value.description
  precedence   = each.value.precedence
}

# Dominio de la Hosted UI (prefijo único global).
resource "aws_cognito_user_pool_domain" "main" {
  domain       = local.cognito_domain_prefix
  user_pool_id = aws_cognito_user_pool.main.id
}

resource "aws_cognito_user_pool_client" "web" {
  name         = "${local.name_prefix}-web"
  user_pool_id = aws_cognito_user_pool.main.id

  # Authorization Code: el navegador recibe un code de un solo uso y la
  # Lambda auth-callback lo cambia por los tokens usando el client secret.
  generate_secret                      = true
  allowed_oauth_flows_user_pool_client = true
  allowed_oauth_flows                  = ["code"]
  allowed_oauth_scopes                 = local.cognito_scopes
  supported_identity_providers         = ["COGNITO"]

  # Cognito solo acepta HTTPS: vuelve al API Gateway, no al bucket (HTTP).
  callback_urls        = [local.cognito_callback_url]
  default_redirect_uri = local.cognito_callback_url

  explicit_auth_flows = [
    "ALLOW_USER_SRP_AUTH",
    "ALLOW_REFRESH_TOKEN_AUTH",
  ]

  prevent_user_existence_errors = "ENABLED"
  enable_token_revocation       = true

  access_token_validity  = 60
  id_token_validity      = 60
  refresh_token_validity = 30

  token_validity_units {
    access_token  = "minutes"
    id_token      = "minutes"
    refresh_token = "days"
  }

  # El usuario puede leer su empresa y su sede, pero NO escribirlas: así nadie
  # se declara de otra empresa. Solo un administrador las cambia.
  read_attributes  = ["email", "email_verified", "name", "custom:empresa_id", "custom:sede_id"]
  write_attributes = ["email", "name"]
}

# El client secret no va en texto plano en la Lambda: se guarda acá y la
# Lambda recibe solo el ARN.
resource "aws_secretsmanager_secret" "cognito_client" {
  name_prefix             = "${local.name_prefix}-cognito-client-"
  description             = "Client secret del app client web de Cognito"
  kms_key_id              = aws_kms_key.main.arn
  recovery_window_in_days = var.secrets_recovery_window_days
}

resource "aws_secretsmanager_secret_version" "cognito_client" {
  secret_id     = aws_secretsmanager_secret.cognito_client.id
  secret_string = aws_cognito_user_pool_client.web.client_secret
}

# -----------------------------------------------------------------------------
# API Gateway: la única puerta pública. Tiene dos usos:
#   - GET /callback: puente HTTPS del login (Lambda auth-callback), acá abajo.
#   - /api/*: la API, con authorizer JWT de Cognito y VPC Link al ALB (api.tf).
# -----------------------------------------------------------------------------
resource "aws_apigatewayv2_api" "auth" {
  name          = "${local.name_prefix}-auth-callback"
  description   = "Entrada HTTPS: callback del login y API con authorizer de Cognito"
  protocol_type = "HTTP"

  # El sitio (S3) es otro origen. API Gateway responde el preflight OPTIONS
  # antes del authorizer (el navegador no manda el token en el preflight).
  cors_configuration {
    allow_origins = [local.site_url]
    allow_methods = ["GET", "POST", "OPTIONS"]
    allow_headers = ["authorization", "content-type"]
    max_age       = 3600
  }
}

resource "aws_apigatewayv2_integration" "auth_callback" {
  api_id                 = aws_apigatewayv2_api.auth.id
  integration_type       = "AWS_PROXY"
  integration_uri        = module.lambdas["auth-callback"].invoke_arn
  integration_method     = "POST"
  payload_format_version = "2.0"
}

resource "aws_apigatewayv2_route" "auth_callback" {
  api_id    = aws_apigatewayv2_api.auth.id
  route_key = "GET ${var.cognito_callback_path}"
  target    = "integrations/${aws_apigatewayv2_integration.auth_callback.id}"
}

resource "aws_apigatewayv2_stage" "default" {
  api_id      = aws_apigatewayv2_api.auth.id
  name        = "$default"
  auto_deploy = true

  # Límite para toda la API (callback y /api/*): frena abusos antes de ECS.
  default_route_settings {
    throttling_rate_limit  = 50
    throttling_burst_limit = 100
  }
}

# Policy basada en recursos: API Gateway puede invocar a auth-callback.
resource "aws_lambda_permission" "apigw_auth_callback" {
  statement_id  = "AllowApiGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = module.lambdas["auth-callback"].function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.auth.execution_arn}/*/*${var.cognito_callback_path}"
}
