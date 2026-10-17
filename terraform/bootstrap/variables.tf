variable "region" {
  description = "Región de AWS donde se crea el bucket del state (la misma que la infra)."
  type        = string
  default     = "us-east-1"
}

variable "project_name" {
  description = "Nombre del proyecto. Se usa como prefijo de los recursos."
  type        = string
  default     = "accesoseguro"

  validation {
    condition     = can(regex("^[a-z0-9-]{3,20}$", var.project_name))
    error_message = "Solo minúsculas, números y guiones (3 a 20 caracteres): los nombres de bucket no aceptan otra cosa."
  }
}

variable "environment" {
  description = "Ambiente (dev, prod...). Forma parte de la key del state."
  type        = string
  default     = "dev"
}

variable "state_bucket_name" {
  description = "Nombre del bucket del state. Si se deja vacío se arma como <proyecto>-tfstate-<account_id> para que sea único a nivel global."
  type        = string
  default     = ""
}

variable "noncurrent_version_retention_days" {
  description = "Días que se conservan las versiones viejas del state (para poder volver atrás)."
  type        = number
  default     = 90
}

variable "create_dynamodb_lock_table" {
  description = "Crear además una tabla DynamoDB de locks (método clásico). Con Terraform >= 1.10 alcanza con use_lockfile en S3."
  type        = bool
  default     = false
}

variable "generate_backend_file" {
  description = "Si es true, escribe ../infra/backend.hcl con la configuración del backend lista para usar."
  type        = bool
  default     = true
}
