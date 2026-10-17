# -----------------------------------------------------------------------------
# BOOTSTRAP: el "primer Terraform".
# Crea SOLO el bucket donde va a vivir el state file del Terraform principal
# (infra/). Como el bucket todavía no existe, este proyecto guarda su propio
# state en local (bootstrap/terraform.tfstate). Es el problema del huevo y la
# gallina: alguien tiene que crear el bucket antes de poder usarlo de backend.
# -----------------------------------------------------------------------------
terraform {
  # 1.10+ trae el lock nativo de S3 (use_lockfile), sin necesidad de DynamoDB.
  required_version = ">= 1.10.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
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
