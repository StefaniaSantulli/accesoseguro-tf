# AccesoSeguro — Infraestructura como Código (TP3)

82.08 Cloud Computing · Terraform sobre AWS Learner Lab

Este repositorio levanta con Terraform la arquitectura de **AccesoSeguro** (versión del
17/10/2026, con la devolución de la cátedra del 16/10): sitio estático en S3, login con la
Hosted UI de Cognito, API Flask en ECS Fargate detrás de un ALB, RDS PostgreSQL Multi-AZ,
6 Lambdas, SNS + SQS con DLQ y EventBridge; los avisos por mail salen por SNS. Sin EC2 y sin NAT Gateway.

---

## 1. Por qué hay dos proyectos de Terraform

Terraform guarda en el **state file** (`terraform.tfstate`) qué recursos creó y con qué ID.
Si ese archivo queda en la máquina de una sola persona, el resto del equipo (y el pipeline)
no puede trabajar. Por eso el state va a un **backend remoto en S3**.

El problema: el bucket del state tiene que existir antes de que el Terraform principal pueda
usarlo. Como vimos en clase, se resuelve con **dos Terraform**:

| Proyecto | Qué crea | Dónde guarda su propio state |
|---|---|---|
| `bootstrap/` | El bucket S3 del state (versionado, cifrado, privado, solo HTTPS, `prevent_destroy`) y genera `infra/backend.hcl` | Local (`bootstrap/terraform.tfstate`) |
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
├── .github/workflows/terraform.yml   # Pipeline: fmt, init, validate y plan en cada PR
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
├── lambdas/<nombre>/handler.py       # código de las 6 Lambdas
├── backend/                          # API Flask + Dockerfile (imagen de ECS)
├── frontend/                         # sitio estático (HTML/CSS/JS) + config.js.tftpl
├── scripts/push-image.sh             # build + push a ECR + redeploy de ECS
├── Makefile                          # atajos: make bootstrap / init / plan / apply / image
├── architecture.png                  # diagrama actualizado
└── README.md
```

Convenciones: todos los recursos se llaman `<proyecto>-<ambiente>-<componente>`
(ej. `accesoseguro-dev-cola-auditoria`); los nombres locales de Terraform van en
`snake_case`; los recursos únicos de un módulo se llaman `this`.

---

## 3. De la arquitectura al código

```mermaid
flowchart LR
  U[Usuario] -->|HTML/JS| S3S[S3 sitio]
  U -->|login| COG[Cognito Hosted UI]
  COG -->|code| APIGW[API Gateway /callback]
  APIGW --> LCB[Lambda auth-callback]
  U -->|Bearer token| IGW[Internet Gateway] --> ALB
  subgraph VPC 10.0.0.0/16
    ALB[ALB - subredes públicas] --> ECS[ECS Fargate Flask - subredes app]
    ECS --> RDS[(RDS PostgreSQL Multi-AZ - subredes datos)]
    ECS -->|invoke| LREP[Lambda generar-reporte]
    LVAL[Lambda validar-documento] --> RDS
    LVEN[Lambda vencer-permisos] --> RDS
    LREP --> RDS
    VPCE[VPC Endpoints: S3, ECR, logs, secrets, SNS, Lambda]
  end
  ECS -->|publica| SNS[SNS eventos-solicitud]
  SNS --> QA[SQS auditoría] --> LAUD[Lambda archivar-auditoria] --> S3A[S3 auditoría]
  SNS --> QN[SQS notificaciones] --> LMAIL[Lambda notificar-mail] --> SNSMAIL[SNS notificaciones-mail: mail]
  S3D[S3 documentos] -->|PDF nuevo| QD[SQS documentos] --> LVAL
  EB[EventBridge 15 min] --> LVEN --> SNS
```

| Capa (PDF) | Servicio | Archivo | Recursos principales |
|---|---|---|---|
| Front | S3 sitio estático | `frontend.tf` | `aws_s3_bucket.sitio`, website, policy pública de lectura, `aws_s3_object` |
| Identidad | Cognito + API Gateway /callback + Lambda | `identity.tf` | User Pool, 3 grupos, dominio, app client, HTTP API |
| Entrada | Internet Gateway + ALB | `modules/network`, `alb.tf` | IGW, `aws_lb`, target group tipo `ip`, listener 80 |
| Cómputo | ECS Fargate, ECR, 6 Lambdas | `ecs.tf`, `lambdas.tf` | cluster, task definition, service, autoscaling, `module.lambdas` |
| Datos | RDS Multi-AZ, 2 buckets privados | `database.tf`, `storage.tf` | `aws_db_instance.main`, `module.buckets` |
| Eventos | SNS, SQS + DLQ, EventBridge, SNS por mail | `messaging.tf`, `lambdas.tf` | tópico, `module.sqs`, suscripciones, regla cada 15 min |
| Seguridad | VPC, SG, NACL, Secrets Manager, KMS, LabRole, CloudTrail | `network.tf`, `security.tf` | SG encadenados, NACL datos, clave KMS, secretos |
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
| `modules/lambda-function` | **Propio** | Empaquetar y crear una Lambda con su log group | Una llamada con `for_each` crea las 6 |
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
| `slice()` | `locals.tf` | Quedarse con las primeras N zonas de disponibilidad |
| `format()` | `locals.tf`, `security.tf`, `bootstrap/main.tf` | Armar nombres de bucket, dominio de Cognito y URLs |
| `coalesce()` | `bootstrap/main.tf` | Usar el nombre de bucket pasado o, si está vacío, uno generado |
| `templatefile()` | `bootstrap/main.tf`, `frontend.tf` | Generar `backend.hcl` y el `config.js` del sitio con valores reales |
| `jsonencode()` | KMS, bucket policies, SQS, ECS, ECR | Escribir políticas y definiciones JSON en HCL |
| `fileset()` + `setsubtract()` | `locals.tf` | Listar los archivos del sitio para subirlos (sin la plantilla) |
| `filemd5()` | `frontend.tf` | Volver a subir un archivo solo si cambió |
| `lookup()`, `split()`, `element()`, `length()`, `lower()` | `frontend.tf` | Elegir el Content-Type según la extensión |
| `urlencode()`, `join()` | `locals.tf` | Armar la URL de login de la Hosted UI |
| `keys()`, `sort()` | `ecs.tf` | Pasar el mapa de variables de entorno al formato de ECS |
| `toset()`, `compact()`, `concat()` | `messaging.tf`, `network.tf`, `modules/s3-bucket` | Lista de mails a verificar sin vacíos; set para `for_each` |
| `tostring()` | `locals.tf` | Las variables de entorno tienen que ser strings |
| `title()`, `replace()` | `messaging.tf`, `network.tf` | Armar el `Sid` de la policy y el nombre de cada endpoint |
| `max()` | `ecs.tf` | El máximo del Auto Scaling nunca queda por debajo del mínimo |
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
- AWS CLI v2 (Terraform la usa para crear los buckets) y Docker
- Credenciales del Learner Lab: **AWS Details → AWS CLI** y pegarlas en `~/.aws/credentials`
  (vencen cada sesión de 4 horas)

```bash
aws sts get-caller-identity   # tiene que mostrar el account id del lab
```

### Paso 1 — State remoto (bootstrap)
```bash
cd terraform/bootstrap
cp terraform.tfvars.example terraform.tfvars   # opcional
terraform init
terraform plan
terraform apply
terraform output
```
Esto crea el bucket `accesoseguro-tfstate-<account_id>` y escribe `infra/backend.hcl`.
**Guardar `bootstrap/terraform.tfstate`** (no se sube al repo). Si se pierde, el bucket se
puede importar con `terraform import aws_s3_bucket.tfstate <nombre>`.

### Paso 2 — Infraestructura
```bash
cd ../infra
cp terraform.tfvars.example terraform.tfvars   # completar notificaciones_suscriptores
terraform init -backend-config=backend.hcl
terraform validate
terraform plan -out=tfplan
terraform apply tfplan                          # ~15-20 min (RDS Multi-AZ es lo más lento)
```
Para verificar que el state quedó en S3:
```bash
aws s3 ls s3://$(cd ../bootstrap && terraform output -raw state_bucket_name)/accesoseguro/dev/
```

### Paso 3 — Imagen de la API
El servicio de ECS queda creado, pero las tasks no arrancan hasta que exista en ECR la
imagen con el tag de `api_image_tag`:
```bash
make image      # construye backend/ y la sube con el tag de terraform.tfvars
make plan
make apply      # nueva revisión de la task definition + rolling update
```
Los tags son **inmutables**: para desplegar un cambio de la API, subí `api_image_tag`
(v1.1 → v1.2), `make image`, `make plan`, `make apply`.

### Paso 4 — Probar la aplicación
```bash
make outputs    # sitio_url, api_url, login_url...
```
1. Abrir `sitio_url` → **Ingresar** → registrarse en la Hosted UI de Cognito (llega un código al mail).
2. Un usuario sin grupo es **transportista**: carga solicitudes (con PDF) y ve las suyas.
3. Para el personal de planta, asignar el grupo:
   ```bash
   make grupo EMAIL=responsable@mail.com GRUPO=responsables_sede   # aprobación, reportes, auditoría
   make grupo EMAIL=porteria@mail.com    GRUPO=porteria            # verificación, ingreso y egreso
   make grupo EMAIL=admin@mail.com       GRUPO=administradores     # todo
   ```
   El usuario tiene que **salir y volver a ingresar** para que el grupo aparezca en su token.
4. Flujo completo: el transportista crea una solicitud con PDF → el PDF sube directo a S3 →
   S3 avisa a `cola-documentos` → Lambda `validar-documento`. La API publica el evento en SNS →
   `cola-auditoria` (Lambda guarda el JSON en el bucket de auditoría) y `cola-notificaciones`
   (Lambda `notificar-mail`). El responsable aprueba, portería verifica por DNI o patente y
   registra ingreso/egreso, y todo queda en auditoría y en el reporte CSV.

### Paso 5 — Destruir (al terminar la sesión del lab)
```bash
cd terraform/infra && terraform destroy
```
El bucket del state **no** se destruye (tiene `prevent_destroy`). Si de verdad hay que borrarlo,
sacar el bloque `lifecycle` de `bootstrap/main.tf`, vaciar el bucket y hacer `terraform destroy`.

---

## 8. Pipeline de GitHub Actions

`.github/workflows/terraform.yml` corre en cada **pull request** a `main`:

| Job | Pasos | Necesita credenciales |
|---|---|---|
| `validate` (matriz `bootstrap` / `infra`) | `terraform fmt -check` → `terraform init -backend=false` → `terraform validate` | No |
| `plan` (`infra`) | arma `backend.hcl` → `terraform init` → `terraform plan` → publica el plan en el resumen y comenta en el PR | Sí |

Secrets a cargar en **Settings → Secrets and variables → Actions** del repositorio donde
corre el PR:

| Secret | Valor |
|---|---|
| `AWS_ACCESS_KEY_ID` | del Learner Lab |
| `AWS_SECRET_ACCESS_KEY` | del Learner Lab |
| `AWS_SESSION_TOKEN` | del Learner Lab (vence: actualizarlo antes de abrir el PR) |
| `TF_STATE_BUCKET` | salida `state_bucket_name` de `bootstrap/` |

Si faltan los secrets, el job `plan` muestra un aviso y termina sin error; `validate` corre
igual. **Antes de cada push** correr `make fmt` para que no falle
el `fmt -check`.

---

## 9. Decisiones por las restricciones del Learner Lab

| Restricción | Cómo se resolvió |
|---|---|
| Una *service control policy* prohíbe `s3:GetBucketObjectLockConfiguration`, que `aws_s3_bucket` lee siempre (falla con `AccessDenied`) | El bucket se crea con la AWS CLI desde `terraform_data` + `local-exec` (módulo `s3-bucket`); cifrado, versionado, políticas, lifecycle y website siguen en Terraform con los recursos `aws_s3_bucket_*`. Lo mismo para el bucket del state en `bootstrap/` |
| No se pueden crear roles IAM | ECS y Lambdas usan `LabRole` (`data.aws_iam_role.lab_role`). Sí se escriben políticas sobre los recursos (buckets, colas, KMS) |
| No hay CloudFront ni HTTPS en el sitio | Cognito vuelve a API Gateway `/callback` (HTTPS) y la Lambda redirige al sitio con los tokens en el `#` |
| Las tasks no tienen salida a internet | VPC Endpoints. Las llaves públicas de Cognito (JWKS) las lee Terraform (`data.http`) y se pasan al contenedor como variable de entorno |
| SES: el LabRole no tiene `ses:VerifyEmailIdentity`, y en sandbox SES solo manda a direcciones verificadas | Los avisos salen por **SNS por mail**: tópico `notificaciones-mail` + una suscripción por persona (`notificaciones_suscriptores`). Cada una confirma con el link que manda AWS. La Lambda publica con el atributo `destinatario` y la *filter policy* de cada suscripción hace que cada uno reciba solo lo suyo (el personal recibe además las solicitudes nuevas). En producción se volvería a SES con el dominio verificado |
| CloudTrail | El historial de 90 días existe siempre; el trail propio queda detrás de `enable_cloudtrail` |
| Se destruye y recrea seguido | `force_destroy` en buckets, `recovery_window_in_days = 0` en secretos, `skip_final_snapshot` fuera de prod |

## 10. La aplicación

- **`backend/`** — API Flask (JSON) que corre en ECS. Se conecta a RDS con las variables que
  inyecta la task definition (la contraseña sale de Secrets Manager), valida el token de Cognito
  (firma, emisor, cliente, vencimiento) con las llaves que Terraform le pasa en `COGNITO_JWKS`, y
  autoriza por grupo: `responsables_sede` (aprobar, reportes, auditoría), `porteria`
  (verificación, ingreso/egreso), `administradores` (todo); sin grupo = transportista.
  Genera presigned URLs para subir y ver los PDFs (clave `empresas/{empresa}/solicitudes/{id}/...`)
  y publica los eventos en SNS. Sin `DB_HOST` ni `COGNITO_JWKS` corre en local con SQLite y sin login.
- **`frontend/`** — HTML/CSS/JS sin build. En AWS el login es con la Hosted UI de Cognito y el rol
  sale del grupo del token; en local (sin `config.js`) queda el selector de rol de la v3.
- **Lambdas** — `validar-documento`, `vencer-permisos` y `generar-reporte` tienen la parte de RDS
  simulada (mock data, permitido por la consigna); el reporte CSV hoy lo genera la API.

### Probar en local (sin AWS)
```bash
cd backend && pip3 install -r requirements.txt && python3 app.py     # terminal 1
cd frontend && python3 -m http.server 8000                           # terminal 2
# abrir http://localhost:8000/login.html
```
