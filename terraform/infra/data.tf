# Datos que existen fuera de este Terraform y solo se leen.

data "aws_caller_identity" "current" {}

# En el Learner Lab no se pueden crear roles: se usa el LabRole existente.
data "aws_iam_role" "lab_role" {
  name = var.lab_role_name
}

# Llaves públicas (JWKS) del User Pool. Flask las necesita para validar la
# firma de los tokens, pero las tasks no tienen salida a internet: se leen
# acá y se le pasan al contenedor como variable de entorno.
data "http" "cognito_jwks" {
  url = format(
    "https://cognito-idp.%s.amazonaws.com/%s/.well-known/jwks.json",
    var.region,
    aws_cognito_user_pool.main.id
  )

  request_headers = {
    Accept = "application/json"
  }
}
