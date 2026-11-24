# Datos que existen fuera de este Terraform y solo se leen.

data "aws_caller_identity" "current" {}

# En el Learner Lab no se pueden crear roles: se usa el LabRole existente.
data "aws_iam_role" "lab_role" {
  name = var.lab_role_name
}

# La firma de los tokens ya no se valida en Flask: lo hace el authorizer JWT de
# API Gateway (api.tf), que baja y renueva solo las llaves públicas de Cognito.
