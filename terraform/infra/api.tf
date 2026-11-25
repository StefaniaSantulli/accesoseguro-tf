# =============================================================================
# API: API Gateway (HTTP API) con authorizer JWT de Cognito -> VPC Link -> ALB
# interno -> ECS.
#
# API Gateway valida el ID token ANTES de que el pedido llegue a Flask: firma
# (con las llaves públicas del User Pool, que baja y renueva solo), emisor,
# audiencia (el app client) y vencimiento. Un token inválido recibe 401 y no
# consume ECS. Flask confía en los datos del token y solo autoriza (grupo y
# sede); ya no valida la firma ni consulta a Cognito en cada pedido.
# =============================================================================

resource "aws_apigatewayv2_authorizer" "cognito" {
  api_id           = aws_apigatewayv2_api.auth.id
  name             = "cognito-id-token"
  authorizer_type  = "JWT"
  identity_sources = ["$request.header.Authorization"]

  jwt_configuration {
    issuer   = "https://cognito-idp.${var.region}.amazonaws.com/${aws_cognito_user_pool.main.id}"
    audience = [aws_cognito_user_pool_client.web.id] # claim "aud" del ID token
  }
}

# El VPC Link crea interfaces de red en las subredes de app para llegar al ALB
# interno. Su SG solo puede salir hacia el ALB (locals.tf).
resource "aws_apigatewayv2_vpc_link" "api" {
  name               = "${local.name_prefix}-api"
  subnet_ids         = module.network.subnet_ids_by_tier["app"]
  security_group_ids = [aws_security_group.this["vpclink"].id]

  lifecycle {
    precondition {
      condition     = length(setsubtract(var.availability_zones, data.aws_availability_zones.vpc_link.names)) == 0
      error_message = "Elegir dos availability_zones compatibles en esta cuenta. Disponibles: ${join(", ", data.aws_availability_zones.vpc_link.names)}."
    }
  }
}

resource "aws_apigatewayv2_integration" "api" {
  api_id               = aws_apigatewayv2_api.auth.id
  integration_type     = "HTTP_PROXY"
  integration_method   = "ANY"
  integration_uri      = aws_lb_listener.http.arn
  connection_type      = "VPC_LINK"
  connection_id        = aws_apigatewayv2_vpc_link.api.id
  timeout_milliseconds = 29000

  # Prueba de que el pedido pasó por el authorizer: API Gateway escribe el
  # "sub" del token ya validado y PISA cualquier valor que mande el cliente.
  # Flask exige que coincida con el sub del token. (Los claims con ":", como
  # cognito:groups o custom:sede_id, no se pueden mapear a headers; Flask los
  # lee del mismo token, que API Gateway ya validó.)
  request_parameters = {
    "overwrite:path"                    = "$request.path"
    "overwrite:header.X-Authorizer-Sub" = "$context.authorizer.claims.sub"
  }
}

# Toda la API exige un token válido de Cognito.
resource "aws_apigatewayv2_route" "api" {
  api_id             = aws_apigatewayv2_api.auth.id
  route_key          = "ANY /api/{proxy+}"
  authorization_type = "JWT"
  authorizer_id      = aws_apigatewayv2_authorizer.cognito.id
  target             = "integrations/${aws_apigatewayv2_integration.api.id}"
}

# Preflight de CORS: el navegador manda OPTIONS SIN token antes de cada pedido
# con Authorization. La ruta ANY de arriba también atrapa los OPTIONS y el
# authorizer los rechazaría (el navegador muestra "Load failed"). Esta ruta,
# más específica y sin authorizer, los deja pasar; API Gateway agrega los
# headers CORS configurados en la API. Flask solo responde el OPTIONS (no
# ejecuta ninguna acción).
resource "aws_apigatewayv2_route" "api_preflight" {
  api_id    = aws_apigatewayv2_api.auth.id
  route_key = "OPTIONS /api/{proxy+}"
  target    = "integrations/${aws_apigatewayv2_integration.api.id}"
}

# Health check público (lo usa scripts/deploy.py para comprobar el despliegue).
resource "aws_apigatewayv2_route" "health" {
  api_id    = aws_apigatewayv2_api.auth.id
  route_key = "GET /health"
  target    = "integrations/${aws_apigatewayv2_integration.api.id}"
}
