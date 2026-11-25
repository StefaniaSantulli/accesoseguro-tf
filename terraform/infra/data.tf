# Datos que existen fuera de este Terraform y solo se leen.

data "aws_caller_identity" "current" {}

# En el Learner Lab no se pueden crear roles: se usa el LabRole existente.
data "aws_iam_role" "lab_role" {
  name = var.lab_role_name
}

# IDs físicos admitidos por VPC Link V2 en us-east-1. También admiten RDS Proxy.
data "aws_availability_zones" "vpc_link" {
  state = "available"
  filter {
    name   = "zone-id"
    values = ["use1-az1", "use1-az2", "use1-az4", "use1-az5", "use1-az6"]
  }
}
