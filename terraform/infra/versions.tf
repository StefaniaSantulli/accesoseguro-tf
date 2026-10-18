# -----------------------------------------------------------------------------
# El "segundo Terraform": toda la infraestructura de AccesoSeguro.
# Su state vive en el bucket que creó bootstrap/.
# -----------------------------------------------------------------------------
terraform {
  required_version = ">= 1.10.0"

  # Configuración parcial: bucket, key y región se pasan en el init con
  #   terraform init -backend-config=backend.hcl
  # Así el nombre del bucket no queda hardcodeado (los bloques backend no
  # aceptan variables).
  backend "s3" {}

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.4"
    }
    http = {
      source  = "hashicorp/http"
      version = "~> 3.4"
    }
  }
}

provider "aws" {
  region = var.region

  # Tags que se ponen solos en TODOS los recursos.
  default_tags {
    tags = local.common_tags
  }
}
