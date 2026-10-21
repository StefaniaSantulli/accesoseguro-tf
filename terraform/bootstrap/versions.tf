# -----------------------------------------------------------------------------
# BOOTSTRAP: el "primer Terraform".
# Crea SOLO el bucket donde va a vivir el state file del Terraform principal
# (infra/). Como el bucket todavía no existe, este proyecto guarda su propio
# state inicialmente en local (bootstrap/terraform.tfstate). scripts/deploy.py
# lo migra al mismo S3 con lock mediante un backend_override.tf generado.
# -----------------------------------------------------------------------------
terraform {
  # 1.10+ trae el lock nativo de S3 (use_lockfile), sin necesidad de DynamoDB.
  required_version = ">= 1.10.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    local = {
      source  = "hashicorp/local"
      version = "~> 2.5"
    }
  }
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project     = var.project_name
      Environment = var.environment
      ManagedBy   = "Terraform"
      Stack       = "bootstrap"
    }
  }
}
