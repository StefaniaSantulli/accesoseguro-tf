# =============================================================================
# Generales
# =============================================================================
variable "region" {
  description = "Región de AWS (el Learner Lab trabaja en us-east-1)."
  type        = string
  default     = "us-east-1"
  validation {
    condition     = var.region == "us-east-1"
    error_message = "La arquitectura usa us-east-1a y us-east-1b; la región debe ser us-east-1."
  }
}

variable "project_name" {
  description = "Nombre del proyecto, prefijo de todos los recursos."
  type        = string
  default     = "accesoseguro"

  validation {
    condition     = can(regex("^[a-z0-9-]{3,20}$", var.project_name))
    error_message = "Solo minúsculas, números y guiones (3 a 20 caracteres)."
  }
}

variable "environment" {
  description = "Ambiente: dev o prod."
  type        = string
  default     = "dev"

  validation {
    condition     = contains(["dev", "prod"], var.environment)
    error_message = "environment tiene que ser dev o prod."
  }
}

variable "lab_role_name" {
  description = "Rol IAM que usan ECS y las Lambdas. En el Learner Lab no se pueden crear roles, se usa LabRole."
  type        = string
  default     = "LabRole"
}

# =============================================================================
# Red
# =============================================================================
variable "vpc_cidr" {
  description = "CIDR de la VPC."
  type        = string
  default     = "10.0.0.0/16"
}

variable "interface_endpoints" {
  description = "Servicios con VPC Endpoint de tipo interface (reemplazan al NAT Gateway)."
  type        = list(string)
  default     = ["ecr.api", "ecr.dkr", "logs", "secretsmanager", "sns", "lambda"]
}

# =============================================================================
# Cómputo: ECS Fargate
# =============================================================================
variable "api_image_tag" {
  description = "Versión de la imagen de la API en ECR (nunca latest). Los tags son inmutables: cada cambio de la API lleva un tag nuevo."
  type        = string
  default     = "v1.1"

  validation {
    condition     = var.api_image_tag != "latest"
    error_message = "No se usa latest: cada despliegue va con una versión explícita (ej: v1.4)."
  }
}

variable "api_container_port" {
  description = "Puerto donde escucha Flask dentro del contenedor."
  type        = number
  default     = 5000
}

variable "api_cpu" {
  description = "CPU de cada task (256 = 0,25 vCPU)."
  type        = number
  default     = 256
}

variable "api_memory" {
  description = "Memoria de cada task en MB."
  type        = number
  default     = 512
}

variable "api_min_tasks" {
  description = "Mínimo de tasks (una por AZ)."
  type        = number
  default     = 2
}

variable "api_max_tasks" {
  description = "Máximo de tasks del Auto Scaling."
  type        = number
  default     = 4
}

variable "api_cpu_target" {
  description = "Porcentaje de CPU promedio que mantiene el Auto Scaling."
  type        = number
  default     = 50
}

variable "api_health_check_path" {
  description = "Ruta que consulta el ALB para saber si una task está sana."
  type        = string
  default     = "/health"
}

# =============================================================================
# Lambdas
# =============================================================================
variable "lambda_runtime" {
  description = "Runtime de Python de las Lambdas."
  type        = string
  default     = "python3.12"
}

variable "vencer_permisos_intervalo_minutos" {
  description = "Cada cuántos minutos EventBridge ejecuta vencer-permisos (vencimientos y reintento de avisos pendientes). La alarma de avisos atrasados se ajusta a este valor."
  type        = number
  default     = 15

  validation {
    condition     = var.vencer_permisos_intervalo_minutos >= 1 && floor(var.vencer_permisos_intervalo_minutos) == var.vencer_permisos_intervalo_minutos
    error_message = "El intervalo debe ser un número entero de minutos, mayor o igual a 1."
  }
}

variable "permiso_vigencia_segundos" {
  description = "Vigencia desde aprobación para ingresar; independiente de la frecuencia del recuperador."
  type        = number
  default     = 86400
  validation {
    condition     = var.permiso_vigencia_segundos > 0 && floor(var.permiso_vigencia_segundos) == var.permiso_vigencia_segundos
    error_message = "La vigencia debe ser un número entero positivo de segundos."
  }
}

variable "max_pdf_size_mb" {
  description = "Tamaño máximo aceptado para un PDF."
  type        = number
  default     = 10
}

variable "presigned_url_ttl_seconds" {
  description = "Vigencia de las presigned URLs (5 minutos)."
  type        = number
  default     = 300
}

# =============================================================================
# Datos: RDS
# =============================================================================
variable "db_name" {
  description = "Nombre de la base."
  type        = string
  default     = "accesoseguro"
}

variable "db_username" {
  description = "Usuario administrador de la base."
  type        = string
  default     = "accesoseguro_admin"
}

variable "db_engine_version" {
  description = "Versión mayor de PostgreSQL."
  type        = string
  default     = "16"
}

variable "db_instance_class" {
  description = "Clase de instancia de RDS."
  type        = string
  default     = "db.t3.micro"
}

variable "db_allocated_storage" {
  description = "Almacenamiento inicial en GB."
  type        = number
  default     = 20
}

variable "db_max_allocated_storage" {
  description = "Tope del autoscaling de almacenamiento en GB."
  type        = number
  default     = 50
}

variable "db_multi_az" {
  description = "Standby sincrónica en la otra AZ."
  type        = bool
  default     = true
}

variable "db_backup_retention_days" {
  description = "Días de backups automáticos."
  type        = number
  default     = 7
}

variable "db_deletion_protection" {
  description = "Protección contra borrado. En el lab va en false para poder hacer destroy."
  type        = bool
  default     = false
}

# =============================================================================
# Almacenamiento
# =============================================================================
variable "force_destroy_buckets" {
  description = "Permite borrar buckets con objetos en un destroy (true solo en el lab)."
  type        = bool
  default     = true
}

variable "documentos_lifecycle" {
  description = "Días de transición del bucket de documentos."
  type = object({
    to_standard_ia_days = number
    to_glacier_ir_days  = number
    expiration_days     = number
  })
  default = {
    to_standard_ia_days = 30
    to_glacier_ir_days  = 180
    expiration_days     = 730
  }

  validation {
    condition = (
      var.documentos_lifecycle.to_standard_ia_days >= 30 &&
      var.documentos_lifecycle.to_glacier_ir_days > var.documentos_lifecycle.to_standard_ia_days &&
      var.documentos_lifecycle.expiration_days > var.documentos_lifecycle.to_glacier_ir_days
    )
    error_message = "S3 exige al menos 30 días en Standard antes de pasar a Standard-IA, y las etapas tienen que ir en orden."
  }
}

# =============================================================================
# Eventos y mails
# =============================================================================
variable "alarm_email" {
  description = "Mail que recibe las alarmas de CloudWatch. Vacío = sin suscripción."
  type        = string
  default     = ""
}

# =============================================================================
# Seguridad y operación
# =============================================================================
variable "log_retention_days" {
  description = "Retención de logs en CloudWatch."
  type        = number
  default     = 14
}

variable "secrets_recovery_window_days" {
  description = "Ventana de recuperación de los secretos al borrarlos (0 = borrado inmediato, útil en el lab)."
  type        = number
  default     = 0
}

variable "enable_cloudtrail" {
  description = "Crea un trail propio que guarda los eventos en S3. El historial de eventos de 90 días existe siempre, sin trail."
  type        = bool
  default     = false
}

variable "cognito_callback_path" {
  description = "Ruta del API Gateway donde vuelve Cognito después del login."
  type        = string
  default     = "/callback"
}

variable "deployment_ready" {
  description = "El pipeline usa false hasta publicar la imagen e inicializar RDS; luego habilita API y workers con true."
  type        = bool
  default     = true
}
