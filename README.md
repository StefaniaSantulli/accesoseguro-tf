# AccesoSeguro — Infraestructura como Código (TP3)

82.08 Cloud Computing · Terraform sobre AWS Learner Lab

Este repositorio levanta con Terraform la arquitectura de **AccesoSeguro**: sitio estático en S3, login con la
Hosted UI de Cognito, API Flask en ECS Fargate detrás de un ALB, RDS PostgreSQL Multi-AZ,
7 Lambdas, SNS + SQS con DLQ y EventBridge; los avisos por mail salen por SNS. Sin EC2 y sin NAT Gateway.

La red se despliega exclusivamente en **us-east-1a y us-east-1b**, con una subred por capa en cada zona.

---

## 1. Por qué hay dos proyectos de Terraform

Terraform guarda en el **state file** (`terraform.tfstate`) qué recursos creó y con qué ID.
Si ese archivo queda en la máquina de una sola persona, el resto del equipo (y el pipeline)
no puede trabajar. Por eso el state va a un **backend remoto en S3**.

El problema: el bucket del state tiene que existir antes de que el Terraform principal pueda
usarlo. Como vimos en clase, se resuelve con **dos Terraform**:

| Proyecto | Qué crea | Dónde guarda su propio state |
|---|---|---|
| `bootstrap/` | El bucket S3 del state (versionado, cifrado, privado, solo HTTPS, `prevent_destroy`) y genera `infra/backend.hcl` | Temporal local; el despliegue lo migra al mismo S3, con lock |
| `infra/` | Toda la arquitectura de AccesoSeguro | En el bucket creado por `bootstrap/` |

El nombre del bucket es una variable (no está hardcodeado) y por defecto se arma como
`accesoseguro-tfstate-<account_id>` para que sea único a nivel global.

**Lock del state.** Con Terraform ≥ 1.10 el backend S3 bloquea el state con un archivo `.tflock`
en el mismo bucket (`use_lockfile = true`): si dos personas hacen `apply` a la vez, la segunda
espera. La tabla DynamoDB del método clásico queda como opción (`create_dynamodb_lock_table`).

---

## 2. Estructura del repositorio

```
.
├── .github/workflows/terraform.yml   # fmt, init, validate y plan en cada PR
├── .github/workflows/deploy.yml      # despliegue completo manual, incluidas imagen y RDS
├── .github/workflows/destroy.yml     # destroy manual de toda la infraestructura (con confirmación)
├── .github/workflows/tests.yml       # pruebas de API, PostgreSQL, frontend y Layer
├── terraform/
│   ├── bootstrap/                    # 1er Terraform: bucket del state (+ genera infra/backend.hcl)
│   ├── infra/                        # 2do Terraform: la arquitectura, un archivo por capa
│   │   ├── versions.tf  variables.tf  locals.tf  data.tf  outputs.tf
│   │   ├── network.tf  security.tf  frontend.tf  storage.tf  identity.tf
│   │   ├── alb.tf  ecs.tf  database.tf  lambdas.tf  messaging.tf  monitoring.tf
│   │   └── backend.hcl.example  terraform.tfvars.example
│   └── modules/                      # Módulos propios
│       ├── network/                  # VPC, subredes, IGW, rutas y NACL
│       ├── lambda-function/          # Lambda + zip + log group
│       ├── sqs-with-dlq/             # cola + DLQ + alarma
│       └── s3-bucket/                # bucket compatible con el Learner Lab
├── lambdas/<nombre>/handler.py       # código de las 7 Lambdas
├── backend/                          # API Flask + Dockerfile (imagen de ECS)
├── frontend/                         # sitio estático (HTML/CSS/JS) + config.js.tftpl
├── scripts/deploy.py                 # ordena infraestructura, imagen, base y verificaciones
├── Makefile                          # atajos: deploy / validate / test / grupo / destroy
├── tests/                            # regresiones y comprobaciones del despliegue
├── docs/                             # Cognito/roles y operación de notificaciones
└── README.md
```

Convenciones: todos los recursos se llaman `<proyecto>-<ambiente>-<componente>`
(ej. `accesoseguro-dev-cola-auditoria`); los nombres locales de Terraform van en
`snake_case`; los recursos únicos de un módulo se llaman `this`.

---

## 3. De la arquitectura al código

Este esquema resume el código actual. El diagrama gráfico definitivo de la entrega debe
incorporarse al repositorio antes de presentar el PR.

```mermaid
flowchart LR
  U[Usuario] -->|HTTP| WEB[S3 sitio]
  U -->|HTTPS login| COG[Cognito Hosted UI]
  COG -->|code HTTPS| GW[API Gateway callback]
  GW --> CB[Lambda auth-callback]
  CB -->|Canje de code| COG
  CB -->|Redirección HTTP con tokens en fragmento| U
  U -->|HTTP con access token| IGW[Internet Gateway] --> ALB
  subgraph VPC["VPC: us-east-1a y us-east-1b"]
    subgraph PUBLICA["Subredes públicas: una por AZ"]
      ALB[ALB]
    end
    subgraph APP["Subredes de aplicación: una por AZ"]
      ECS[ECS Fargate API]
      REP[Lambda generar-reporte]
      VAL[Lambda validar-documento]
      VEN[Lambda vencer-permisos]
      MAIL[Lambda notificar-mail]
      VPCE[Endpoints: S3, ECR, Logs, Secrets Manager, SNS y Lambda]
    end
    subgraph DATOS["Subredes de datos: una por AZ"]
      RDS[(RDS PostgreSQL Multi-AZ)]
    end
    ALB --> ECS
    ECS --> RDS
    ECS -->|Invocación autorizada| REP
    REP --> RDS
    VAL --> RDS
    VEN --> RDS
    MAIL --> RDS
  end
  ECR[ECR imagen] --> ECS
  SM[Secrets Manager] --> ECS
  SM --> REP & VAL & VEN & MAIL
  ECS -->|Perfil y alta| USERS[Lambda gestionar-usuarios]
  USERS --> COG
  USERS -->|Suscripciones y filtros| SNSMAIL[SNS notificaciones por email]
  U -->|PUT firmado HTTPS| DOCS[S3 documentos]
  DOCS --> QD[SQS documentos y DLQ] --> VAL
  ECS -->|Eventos guardados en outbox| SNS[SNS eventos-solicitud]
  EB[EventBridge cada 15 minutos configurable] --> VEN --> SNS
  SNS --> QA[SQS auditoría y DLQ] --> AUD[Lambda archivar-auditoria]
  AUD --> ARCH[S3 auditoría]
  REP -->|CSV en reportes/| ARCH
  ARCH -->|Descarga firmada HTTPS| U
  SNS --> QN[SQS notificaciones y DLQ] --> MAIL --> SNSMAIL
  SNSMAIL -->|Filtro por destinatario| EMAIL[Correo confirmado]
  CW[CloudWatch logs y alarmas]
```

| Capa | Servicio | Archivo | Recursos principales |
|---|---|---|---|
| Front | S3 sitio estático | `frontend.tf` | `module.sitio` (creación con CLI), website, policy pública de lectura, `aws_s3_object` |
| Identidad | Cognito + API Gateway /callback + Lambda | `identity.tf` | User Pool, 3 roles (transportistas, porteros y responsables_sede), dominio, app client, HTTP API |
| Entrada | Internet Gateway + ALB | `modules/network`, `alb.tf` | IGW, `aws_lb`, target group tipo `ip`, listener 80 |
| Cómputo | ECS Fargate, ECR, 7 Lambdas | `ecs.tf`, `lambdas.tf` | cluster, task definition, service, autoscaling, `module.lambdas` |
| Datos | RDS Multi-AZ, 2 buckets privados | `database.tf`, `storage.tf` | `aws_db_instance.main`, `module.buckets` |
| Eventos | SNS, SQS + DLQ, EventBridge, SNS por mail | `messaging.tf`, `lambdas.tf` | tópico, `module.sqs`, suscripciones, DLQs de entrega, recuperación cada 15 minutos (configurable) |
| Seguridad | VPC, SG, NACL, Secrets Manager, KMS, LabRole; CloudTrail propio opcional | `network.tf`, `security.tf` | SG encadenados, NACL datos, clave KMS, secretos; `enable_cloudtrail = false` por defecto |
| Operación | CloudWatch, Auto Scaling | `monitoring.tf`, `ecs.tf` | alarmas (5xx, tasks sanas, DLQ, errores Lambda, RDS) |

### Red
| Subred | CIDR | Contiene | Ruta a internet |
|---|---|---|---|
| publica-a / publica-b | 10.0.1.0/24 y 10.0.2.0/24 | ALB | Sí (IGW) |
| app-a / app-b | 10.0.11.0/24 y 10.0.12.0/24 | Tasks de ECS, Lambdas en la VPC, endpoints | No |
| datos-a / datos-b | 10.0.21.0/24 y 10.0.22.0/24 | RDS primaria y standby | No |

Security Groups encadenados: `alb` (80 desde internet) → `ecs` (5000 solo desde `alb`) →
`rds` (5432 solo desde `ecs` y `lambda`); `endpoints` (443 desde `ecs` y `lambda`).

---

## 4. Módulos

| Módulo | Tipo | Para qué | Cómo se usa |
|---|---|---|---|
| `modules/network` | **Propio** | VPC en 3 capas x 2 AZ, IGW, tablas de ruteo y NACL | Una vez, en `network.tf` |
| `modules/lambda-function` | **Propio** | Empaquetar y crear una Lambda con su log group | Una llamada con `for_each` crea las 7 |
| `modules/sqs-with-dlq` | **Propio** | Cola + DLQ + alarma | Una llamada con `for_each` crea las 3 colas |
| `modules/s3-bucket` | **Propio** | Bucket con cifrado, versionado, políticas, lifecycle, CORS y website, compatible con el Learner Lab | `for_each` (documentos y auditoría), una vez para el sitio y con `count` para CloudTrail |
| `terraform-aws-modules/ecr/aws` `~> 2.0` | **Externo** (Terraform Registry) | Repositorio ECR con tags inmutables, escaneo, cifrado KMS y lifecycle policy | Una vez, en `ecs.tf` |

Cada módulo propio sigue la estructura recomendada en clase: `main.tf`, `variables.tf`,
`outputs.tf`, `versions.tf` y `README.md`.

---

## 5. Funciones de Terraform utilizadas

| Función | Dónde | Para qué |
|---|---|---|
| `cidrsubnet()` | `modules/network/main.tf` | Calcular el CIDR de cada subred a partir de la VPC |
| `cidrhost()` + `can()` | `modules/network/variables.tf` | Validar que `vpc_cidr` sea un CIDR válido |
| `merge()` | network, `locals.tf` | Unir los mapas de subredes por capa; unir nombres de buckets |
| `substr()` | network, `alb.tf` | Letra de la AZ (`us-east-1a` → `a`); recortar nombres a 32 caracteres |
| `format()` | `locals.tf`, `security.tf`, `bootstrap/main.tf` | Armar nombres de bucket, dominio de Cognito y URLs |
| `coalesce()` | `bootstrap/main.tf` | Usar el nombre de bucket pasado o, si está vacío, uno generado |
| `templatefile()` | `bootstrap/main.tf`, `frontend.tf` | Generar `backend.hcl` y el `config.js` del sitio con valores reales |
| `jsonencode()` | KMS, bucket policies, SQS, ECS, ECR | Escribir políticas y definiciones JSON en HCL |
| `fileset()` + `setsubtract()` | `locals.tf` | Listar los archivos del sitio para subirlos (sin la plantilla) |
| `filemd5()` | `frontend.tf` | Volver a subir un archivo solo si cambió |
| `lookup()`, `split()`, `element()`, `length()`, `lower()` | `frontend.tf` | Elegir el Content-Type según la extensión |
| `urlencode()`, `join()` | `locals.tf` | Armar la URL de login de la Hosted UI |
| `keys()`, `sort()` | `ecs.tf` | Pasar el mapa de variables de entorno al formato de ECS |
| `toset()`, `concat()` | `locals.tf`, `network.tf`, `monitoring.tf`, `modules/s3-bucket` | Sets para `for_each` (colas suscriptas, endpoints, alarmas); unir statements de la bucket policy |
| `tostring()` | `locals.tf` | Las variables de entorno tienen que ser strings |
| `title()`, `replace()` | `messaging.tf`, `network.tf` | Armar el `Sid` de la policy y el nombre de cada endpoint |
| `max()` | `ecs.tf`, `locals.tf` | El máximo del Auto Scaling nunca queda por debajo del mínimo; umbral mínimo de la alarma de avisos atrasados |
| `formatdate()`, `timestamp()` | `database.tf` | Nombre del snapshot final en prod |
| `one()` | `bootstrap/outputs.tf` | Output de un recurso creado con `count` (o `null`) |
| `contains()`, `regex()` | `variables.tf` | Validaciones de entrada |

---

## 6. Meta-argumentos utilizados

| Meta-argumento | Dónde | Por qué |
|---|---|---|
| `for_each` | Subredes, rutas, SG y sus reglas, endpoints, grupos de Cognito, `module.lambdas`, `module.sqs`, `module.buckets`, event source mappings, suscripciones SNS, alarmas | Crear N recursos parecidos a partir de un mapa, con claves estables (`publica-a`, `cola-auditoria`) |
| `count` | IGW (si hay capa pública), CloudTrail y su bucket (`enable_cloudtrail`, también sobre un `module`), website / lifecycle / CORS opcionales en `s3-bucket`, suscripción de alarmas por mail, tabla DynamoDB de locks, `local_file` del backend | Recursos opcionales: 0 o 1 según una variable |
| `depends_on` | Policy del bucket después del public access block; notificación S3 después de la policy de la cola; suscripción SNS después de la policy; servicio ECS después del listener y de los endpoints; CloudTrail después de la policy del bucket; log group antes de la Lambda | Dependencias que AWS exige pero que Terraform no puede deducir de las referencias |
| `lifecycle { prevent_destroy }` | Bucket del state (`bootstrap/`) | Que un `destroy` nunca borre el state |
| `lifecycle { create_before_destroy }` | Security Groups, parameter group de RDS | Reemplazar sin cortar: AWS no deja borrar un SG en uso |
| `lifecycle { ignore_changes }` | `desired_count` del servicio ECS; `schema` del User Pool; snapshot final de RDS | No pelear con el Auto Scaling; Cognito no permite modificar el schema (recrearía el pool y se perderían los usuarios) |

También se usan **bloques `dynamic`** (`vpc_config` de las Lambdas, reglas de la NACL) y
**validaciones** en las variables.

---

## 7. Paso a paso

Atajo: todos los pasos están en el `Makefile` (`make help`).

### Requisitos

- Terraform ≥ 1.10 (`terraform -version`)
- Python 3.12, AWS CLI v2 (creación/borrado de buckets) y Docker funcionando
- Node.js 22 para las pruebas del frontend; GitHub Actions instala estas herramientas
- Credenciales del Learner Lab: **AWS Details → AWS CLI** y pegarlas en `~/.aws/credentials`
  (son temporales; renovar cuando venzan)

```bash
aws sts get-caller-identity   # tiene que mostrar el account id del lab
```

### Desplegar desde cero o actualizar

1. Guardar los cambios en un commit y subirlos. El workflow manual debe estar en `main` para habilitarlo en GitHub; luego permite seleccionar la rama a desplegar. [Requisito de GitHub](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/manually-run-a-workflow).
2. Configurar los secrets de AWS en GitHub (ver sección 8).
3. Ejecutar **Actions → desplegar-aplicacion → Run workflow**, seleccionando esa rama.
4. Esperar el resultado y abrir la URL publicada en el resumen.

El workflow construye la Layer y la imagen, crea o recupera el backend de Terraform,
prepara la infraestructura, sube la imagen a ECR, ejecuta una tarea puntual de ECS para
crear las tablas y sedes, habilita la aplicación y verifica salud, sitio y reporte real.
No requiere crear tablas ni subir la imagen manualmente. Si ya hay una instalación,
usa el mismo state y realiza una actualización con una ventana de mantenimiento.

Equivalente desde un checkout limpio, con Python 3.12, Terraform, AWS CLI y Docker:

```bash
make deploy
```

Los comandos individuales `make plan` y `make apply` no sustituyen el proceso completo.
El script despliega `accesoseguro/dev` en `us-east-1`; otras opciones conservan los valores
de `variables.tf`, salvo las definidas en un `terraform/infra/terraform.tfvars` local.
Ese archivo está ignorado por Git: los cambios locales no se trasladan a Actions.

### Variables y outputs

| Entrada / salida | Uso |
|---|---|
| `api_min_tasks`, `api_max_tasks`, `api_cpu`, `api_memory` | Capacidad de ECS; por defecto 2–4 tareas, 256 CPU y 512 MB por tarea |
| `db_instance_class`, `db_multi_az`, `db_allocated_storage` | RDS: `db.t3.micro`, Multi-AZ y 20 GB por defecto |
| `permiso_vigencia_segundos`, `vencer_permisos_intervalo_minutos` | Vigencia de 24 horas; revisión y reintentos cada 15 minutos |
| `max_pdf_size_mb`, `presigned_url_ttl_seconds` | PDF máximo de 10 MB; enlaces firmados de 300 segundos |
| `alarm_email`, `enable_cloudtrail` | Correo de alarmas y trail propio opcionales |
| `api_image_tag`, `deployment_ready` | El script los administra: SHA del commit y habilitación tras preparar la base |
| `sitio_url`, `api_url`, `login_url` | Direcciones para probar la aplicación |
| `cognito_user_pool_id`, `lambda_functions` | Identificadores para los scripts de usuarios y notificaciones |
| `deployment`, `ecr_repository_url`, `ecs_*` | Datos que utiliza el despliegue para publicar la imagen e inicializar RDS |

Ver todas las entradas en [variables.tf](terraform/infra/variables.tf), sus ejemplos en
[terraform.tfvars.example](terraform/infra/terraform.tfvars.example) y las salidas en
[outputs.tf](terraform/infra/outputs.tf). No guardar credenciales en esos archivos.

### Orden del despliegue y recuperación

1. Pruebas en Actions; construir Layer e imagen Linux antes de detener una instalación existente.
2. Crear o recuperar el bucket del state; inicializar y validar Terraform.
3. Pausar API, Auto Scaling y consumidores que requieren RDS. Auditoría continúa activa.
4. `plan` y `apply` con `deployment_ready=false`; publicar la imagen en ECR.
5. Ejecutar `python manage_db.py upgrade` como tarea puntual de ECS, con la misma imagen,
   red y permisos que la API. Crea tablas, relaciones y tres sedes iniciales; no agrega
   un servicio permanente. [Tareas puntuales de ECS](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/standalone-tasks.html).
6. `plan` y `apply` con `deployment_ready=true`; activar ECS y comprobar revisión,
   cantidad de tareas, `/health`, sitio y una invocación real de reportes.

La imagen usa el SHA completo como tag inmutable; repetir el commit reutiliza la imagen.
La API no crea tablas al arrancar. Si falla la imagen o la inicialización, el script no
continúa a la activación. `/health` responde 503 si RDS o su esquema no están disponibles.

Los states se guardan en `accesoseguro/dev/bootstrap.tfstate` e
`accesoseguro/dev/infra.tfstate`. El primero se migra desde local a S3 al crear el bucket.
`terraform/bootstrap/backend_override.tf` activa el backend y se genera sin versionarlo.
Ambos usan cifrado y bloqueo nativo de S3. [Backend S3 de Terraform](https://developer.hashicorp.com/terraform/language/backend/s3).

Si falla, revisar el paso y los logs de la tarea/Lambda en CloudWatch, corregir y repetir
el workflow usando **el mismo bucket de state**. No cambiar el bucket para sortear un error.
Las tablas se preparan de forma aditiva, con transacción y bloqueo de PostgreSQL, sin
duplicar sedes ni borrar solicitudes. Futuras modificaciones requieren una migración en
`backend/database_schema.py`. Los bloques `moved` conservan las referencias a colas,
políticas y alarmas renombradas en v4; no eliminarlos por limpieza.
[Refactorización de recursos en Terraform](https://developer.hashicorp.com/terraform/language/modules/develop/refactoring).

### Probar la aplicación

Si desplegaste desde Actions, antes de usar `make outputs` o `make grupo` en otra copia local:

```bash
cp terraform/infra/backend.hcl.example terraform/infra/backend.hcl
# Editar bucket con el nombre del state de ese despliegue; conservar key, región y lock.
make init
```

```bash
make outputs    # sitio_url, api_url, login_url...
```
1. Abrir `sitio_url` → **Ingresar** → registrarse en la Hosted UI de Cognito (llega un código al mail).
2. Un usuario sin grupo puede activar su cuenta como **transportista**. No puede elegirse personal de planta.
3. El operador del Lab asigna los roles internos y una sede existente:
   ```bash
   make grupo EMAIL=portero@example.com GRUPO=porteros SEDE=1
   make grupo EMAIL=responsable@example.com GRUPO=responsables_sede SEDE=2
   ```
   El script reemplaza el rol que tuviera el usuario por uno único. Luego el usuario vuelve a
   iniciar sesión. Ver [Cognito y roles](docs/cognito-y-roles.md) para el workaround de clase,
   los permisos y las suscripciones de correo por sede.
4. Flujo completo: el transportista crea una solicitud con PDF → el PDF sube directo a S3 →
   S3 avisa a `cola-documentos` → Lambda `validar-documento`. La API publica el evento en SNS →
   `cola-auditoria` (Lambda guarda el JSON en el bucket de auditoría) y `cola-notificaciones`
   (Lambda `notificar-mail`). El responsable aprueba, portería verifica por DNI o patente y
   registra ingreso/egreso. Estos movimientos quedan en RDS y en el reporte CSV;
   S3 auditoría recibe los eventos de creación, aprobación, rechazo y vencimiento.

### Destruir al terminar
Conviene bajar la infraestructura cuando no se usa (después de la corrección o entre sesiones),
porque RDS Multi-AZ, el ALB, los VPC Endpoints y las tasks de Fargate consumen crédito del Lab.

1. **Actions → destruir-infraestructura → Run workflow**.
2. En el campo de confirmación escribir `destruir` (cualquier otro texto cancela sin borrar nada).

El workflow usa el mismo state remoto que el despliegue, empaqueta la Layer (la exige una
precondición de `lambdas.tf`) y ejecuta `terraform destroy`. Si no encuentra state, termina sin
cambios. Borra todo, incluidos los datos de RDS, los buckets y los usuarios de Cognito.
Si falla por una dependencia que AWS tarda en liberar (por ejemplo, interfaces de red de
las Lambdas en la VPC), volver a ejecutarlo.

Equivalente local, con las credenciales del Lab: `make destroy` (empaqueta la Layer antes).

El bucket del state **no** se destruye: un nuevo despliegue lo reutiliza. El bootstrap
tiene `prevent_destroy` y no tiene un provisioner que borre ese bucket.

---

## 8. Pipeline de GitHub Actions

| Workflow | Cuándo | Qué ejecuta |
|---|---|---|
| `terraform.yml` | PR a `main` o manual | `fmt`, `init`, `validate`; `plan` con credenciales y backend existente |
| `tests.yml` | PR, manual y antes de desplegar | Python, PostgreSQL 16, frontend y Layer Linux |
| `deploy.yml` | Manual: **desplegar-aplicacion** | Pruebas y despliegue completo: `init`, `validate`, `plan`, `apply`, imagen, esquema y comprobaciones |
| `destroy.yml` | Manual: **destruir-infraestructura**, con confirmación `destruir` | `init` y `destroy` de toda la infraestructura; conserva el bucket del state |

Secrets en **Settings → Secrets and variables → Actions** del repositorio que ejecuta:

| Secret | Valor |
|---|---|
| `AWS_ACCESS_KEY_ID` | Del Learner Lab |
| `AWS_SECRET_ACCESS_KEY` | Del Learner Lab |
| `AWS_SESSION_TOKEN` | Del Learner Lab; renovar cuando venza |
| `TF_STATE_BUCKET` | Bucket del despliegue existente; opcional si se usa `accesoseguro-tfstate-<account_id>` |

Para un primer despliegue, ejecutar **desplegar-aplicacion**; el bucket se crea automáticamente.
El `plan` del PR necesita ese backend. En un PR de fork no se reciben los secrets:
se ejecutan pruebas y validación, y se informa que el plan no se ejecutó. El repositorio
receptor debe configurar sus credenciales para que la cátedra pueda desplegar.
Los workflows de Terraform, despliegue y destroy comparten un grupo de concurrencia: nunca corren a la vez sobre el mismo state.

Antes de subir cambios instalar `pip install -r backend/requirements.txt` en un entorno
Python y ejecutar `make fmt`, `make validate` y `make test`. `make validate` inicializa ambos
proyectos sin backend; no requiere AWS. Las pruebas PostgreSQL requieren `TEST_POSTGRES_URL`
contra una base de pruebas: Actions la proporciona; sin ella se omiten esas pruebas.
No subir credenciales,
state ni planes binarios al repositorio.

---

## 9. Decisiones por las restricciones del Learner Lab

Estas decisiones corresponden al Lab y a lo acordado para esta entrega; los permisos
efectivos de la cuenta deben verificarse al ejecutar el workflow.

| Restricción | Cómo se resolvió |
|---|---|
| Lectura de Object Lock restringida en el Lab utilizado | El bucket se crea con AWS CLI desde `terraform_data` + `local-exec` (módulo `s3-bucket`); cifrado, versionado, políticas, lifecycle y website siguen en Terraform. Lo mismo para el bucket del state |
| No se pueden crear roles IAM | ECS y Lambdas usan `LabRole` (`data.aws_iam_role.lab_role`). Sí se escriben políticas sobre los recursos (buckets, colas, KMS) |
| Se acordó mantener sitio y API por HTTP | Cognito vuelve a API Gateway `/callback` (HTTPS) y la Lambda redirige al sitio con los tokens en el `#`. Se conserva el workaround de clase; el tramo HTTP no cifra el tráfico y no corresponde a una configuración de producción |
| Las tasks no tienen salida a internet | VPC Endpoints. Las llaves públicas de Cognito (JWKS) las lee Terraform (`data.http`) y se pasan al contenedor como variable de entorno |
| SES: el LabRole no tiene `ses:VerifyEmailIdentity`, y en sandbox SES solo manda a direcciones verificadas | Los avisos salen por **SNS por mail**: tópico `notificaciones-mail` + una suscripción por persona administrada por `gestionar-usuarios`. Cada una confirma con el link que manda AWS. La Lambda publica con el atributo `destinatario_id` y la *filter policy* de cada suscripción hace que cada uno reciba solo lo suyo (cada responsable recibe además las solicitudes nuevas de su sede). En producción se volvería a SES con el dominio verificado |
| CloudTrail | El trail propio queda detrás de `enable_cloudtrail`; el historial regional de eventos de administración de los últimos 90 días no lo requiere. [AWS](https://docs.aws.amazon.com/awscloudtrail/latest/userguide/view-cloudtrail-events.html) |
| Se destruye y recrea seguido | `force_destroy` en buckets, `recovery_window_in_days = 0` en secretos, `skip_final_snapshot` fuera de prod |

## 10. La aplicación

- **`backend/`** — Flask en ECS con PostgreSQL y credenciales de Secrets Manager.
  Valida el Access Token y consulta el perfil/grupos vigentes mediante `gestionar-usuarios`.
  Autoriza tres roles: transportistas (solicitudes propias), porteros (ingresos/egresos
  de su sede) y responsables_sede (aprobaciones, reportes y auditoría de su sede).
  Los roles internos solo los asigna el operador AWS. El modo local requiere `LOCAL_DEMO=true`.
- **`frontend/`** — HTML/CSS/JS sin build. Conserva el callback HTTPS de clase,
  guarda Access Token e ID Token en sessionStorage y usa solo el primero para la API.
  Ver [guía de Cognito y roles](docs/cognito-y-roles.md).
- **Lambdas** — `validar-documento` persiste la validación de cada PDF en RDS;
  `generar-reporte` consulta datos reales por sede y publica el CSV privado en S3.
  La API autoriza e invoca el reporte. Ver el detalle debajo.

### Probar en local (sin AWS)
```bash
cd backend && pip3 install -r requirements.txt && LOCAL_DEMO=true python3 app.py     # terminal 1
cd frontend && python3 -m http.server 8000                           # terminal 2
# abrir http://localhost:8000/login.html
```

## Notificaciones y vencimientos

Ver [notificaciones.md](docs/notificaciones.md) para el circuito real, la confirmación SNS, el outbox, reintentos y recuperación de fallos. Ejecutar `make layer` antes de `make plan`. Las suscripciones se activan desde el inicio o `scripts/notificaciones.py`; el script de roles actualiza los filtros automáticamente.

`vencer-permisos` corre cada `vencer_permisos_intervalo_minutos` (15 por defecto): vence en RDS los permisos aprobados y no usados después de `permiso_vigencia_segundos` y reintenta los avisos pendientes. `notificar-mail` usa RDS para registrar publicaciones y omitir duplicados. Las cuatro Lambdas que acceden a RDS (`usa_rds = true` en `locals.tf`) comparten la Layer con el driver y el código común.

### Documentos: funcionamiento real

1. La API comprueba que la solicitud pertenece al transportista, sigue pendiente y el tamaño informado es válido antes de reemplazar el documento anterior. Registra el documento como **pendiente** y entrega una URL temporal para cargarlo.
2. El navegador carga el PDF directamente en S3. S3 avisa por la cola existente.
3. `validar-documento` lee **la versión exacta** del objeto, comprueba tipo declarado, tamaño mayor que cero y hasta el máximo configurado, y cabecera `%PDF-`.
4. Guarda **válido** o **rechazado**, el motivo y la versión en `documento_validacion`, relacionada por clave foránea con `solicitud`.
5. El responsable ve el resultado. Si hay un documento adjunto, no puede aprobar hasta que sea válido. El enlace abre la versión validada, incluso si luego se vuelve a escribir la misma clave en S3.

El adjunto es opcional. Esta validación es técnica y básica: no analiza malware, autenticidad ni contenido legal del PDF. El transportista puede reemplazar un archivo pendiente/rechazado desde **Mis solicitudes**.

Los eventos repetidos o atrasados no sobrescriben un resultado más reciente. Se compara el `sequencer` para la misma clave, como indica [S3](https://docs.aws.amazon.com/AmazonS3/latest/userguide/notification-content-structure.html). Si falla S3 o RDS, el mensaje se reintenta mediante la cola existente; solo se reportan los mensajes fallidos, según el mecanismo de [respuestas parciales de Lambda/SQS](https://docs.aws.amazon.com/lambda/latest/dg/services-sqs-errorhandling.html).

### Reportes: funcionamiento real

1. El responsable pide un reporte. La API verifica su identidad y toma la sede del perfil autorizado, no de un valor arbitrario del navegador.
2. Invoca `generar-reporte`, que consulta solicitudes e ingresos/egresos reales de esa sede en PostgreSQL. El filtro corresponde a la fecha de creación de la solicitud y el día final se incluye completo; las fechas almacenadas son UTC.
3. La Lambda genera el CSV y lo guarda en `reportes/` del bucket privado ya existente. Usa su cifrado y regla de eliminación al día.
4. Devuelve una URL firmada temporal. El navegador descarga con esa URL, sin enviar el token de Cognito a S3.

Sin registros, el CSV contiene únicamente encabezados. Se rechazan rangos inválidos y reportes de más de 10.000 filas, pidiendo acotar las fechas. Se escapan inicios de fórmulas en los campos de texto; es una mitigación de [CSV injection](https://community.owasp.org/attacks/CSV_Injection), no una garantía sobre posteriores transformaciones de la planilla.

Las cuatro Lambdas que acceden a RDS comparten una Layer. Contiene el driver PostgreSQL, SQLAlchemy, el código compartido y los certificados de RDS, con dependencias para Python 3.12 y Linux x86_64, siguiendo los requisitos de [AWS Lambda Layers](https://docs.aws.amazon.com/lambda/latest/dg/python-layers.html).

## 11. Verificación de la consigna TP3

Referencia: **Tercera Entrega: Despliegue de Recursos de Infraestructura (IaC)**, consigna de la cátedra.

| Requisito | Evidencia en el repositorio |
|---|---|
| Al menos un módulo propio y uno externo | Cuatro propios en `terraform/modules`; ECR del Terraform Registry (sección 4) |
| Variables y outputs | Ambos proyectos incluyen `variables.tf` y `outputs.tf`; el script consume las salidas (sección 7) |
| Al menos cuatro funciones | `cidrsubnet`, `merge`, `templatefile`, `jsonencode`, entre otras (sección 5) |
| Al menos tres metaargumentos | `for_each`, `count`, `depends_on` y `lifecycle` (sección 6) |
| Organización, nomenclatura y DRY | Archivos por capa; módulos, mapas y código Python compartido |
| README detallado | Arquitectura, módulos, funciones, metaargumentos, variables, ejecución y recuperación |
| GitHub Actions: init, validate, plan, apply | Validación/plan en PR y despliegue manual completo (sección 8) |
| Diagrama alineado | Esquema Mermaid incluido; falta incorporar el diagrama gráfico definitivo |
| PR al repositorio de la cátedra y hash en campus | Pasos de entrega del equipo; no los realiza Terraform |

Antes de entregar:

- Incorporar el diagrama definitivo: SNS email en lugar de SES; siete Lambdas, cuatro con
  acceso a RDS; dos AZ; endpoints privados; reportes CSV; EventBridge cada 15 minutos.
- Abrir el PR al repositorio indicado y revisar sus ejecuciones. Un plan omitido por falta
  de secrets **no acredita** un plan correcto: configurar credenciales vigentes en el
  repositorio que lo ejecuta y preparar primero el backend con el despliegue manual.
- Ejecutar y revisar **desplegar-aplicacion** con el commit final para acreditar `apply`
  y las comprobaciones de aplicación. Las pruebas locales no acreditan permisos o cuotas del Lab.
- Entregar en campus el hash completo obtenido con `git rev-parse HEAD` del commit presentado.
