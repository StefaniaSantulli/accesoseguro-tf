variable "function_name" {
  description = "Nombre de la función."
  type        = string
}

variable "description" {
  description = "Descripción corta de qué hace."
  type        = string
  default     = ""
}

variable "source_dir" {
  description = "Carpeta con el código fuente (se empaqueta en un zip)."
  type        = string
}

variable "build_dir" {
  description = "Carpeta donde se dejan los zips generados."
  type        = string
}

variable "handler" {
  description = "Punto de entrada, formato archivo.funcion."
  type        = string
  default     = "handler.lambda_handler"
}

variable "layers" {
  description = "ARNs de Layers compatibles con runtime y arquitectura."
  type        = list(string)
  default     = []
}

variable "runtime" {
  description = "Runtime de Lambda."
  type        = string
  default     = "python3.12"
}

variable "architecture" {
  description = "x86_64 o arm64."
  type        = string
  default     = "x86_64"

  validation {
    condition     = contains(["x86_64", "arm64"], var.architecture)
    error_message = "architecture tiene que ser x86_64 o arm64."
  }
}

variable "timeout" {
  description = "Tiempo máximo de ejecución en segundos."
  type        = number
  default     = 30

  validation {
    condition     = var.timeout >= 1 && var.timeout <= 900
    error_message = "Lambda acepta entre 1 y 900 segundos (15 minutos)."
  }
}

variable "memory_size" {
  description = "Memoria en MB (la CPU escala con la memoria)."
  type        = number
  default     = 128
}

variable "role_arn" {
  description = "Rol de ejecución. En el Learner Lab es siempre LabRole."
  type        = string
}

variable "environment" {
  description = "Variables de entorno."
  type        = map(string)
  default     = {}
}

variable "vpc_config" {
  description = "Subredes y security groups si la función corre dentro de la VPC. null = fuera de la VPC."
  type = object({
    subnet_ids         = list(string)
    security_group_ids = list(string)
  })
  default = null
}

variable "log_retention_days" {
  description = "Días de retención de los logs en CloudWatch."
  type        = number
  default     = 14
}

variable "tags" {
  description = "Tags adicionales."
  type        = map(string)
  default     = {}
}
