variable "name" {
  description = "Nombre del bucket (único a nivel global)."
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$", var.name))
    error_message = "Nombre de bucket inválido: 3 a 63 caracteres, minúsculas, números, puntos y guiones."
  }
}

variable "region" {
  description = "Región donde se crea el bucket."
  type        = string
}

variable "force_destroy" {
  description = "Si es true, un destroy vacía y borra el bucket. Si es false, el bucket se conserva."
  type        = bool
  default     = false
}

variable "versioning" {
  description = "Activar versionado."
  type        = bool
  default     = true
}

variable "kms_key_arn" {
  description = "Clave KMS para SSE-KMS. null = SSE-S3 (AES256), obligatorio para sitios públicos."
  type        = string
  default     = null
}

variable "website" {
  description = "Sitio estático público (website hosting + lectura pública de objetos)."
  type        = bool
  default     = false
}

variable "lifecycle_rules" {
  description = "Reglas de lifecycle por prefijo."
  type = list(object({
    id     = string
    prefix = string
    transitions = optional(list(object({
      days          = number
      storage_class = string
    })), [])
    expiration_days            = optional(number)
    noncurrent_expiration_days = optional(number)
  }))
  default = []
}

variable "cors_rules" {
  description = "Reglas CORS (para que el navegador use presigned URLs)."
  type = list(object({
    allowed_methods = list(string)
    allowed_origins = list(string)
    allowed_headers = optional(list(string), ["*"])
    expose_headers  = optional(list(string), [])
    max_age_seconds = optional(number, 3000)
  }))
  default = []
}

variable "extra_policy_statements" {
  description = "Statements adicionales para la bucket policy (por ejemplo, permisos de CloudTrail)."
  type        = any
  default     = []
}
