# Cognito y permisos de AccesoSeguro

La aplicación conserva el workaround de las páginas 27–29 de «Clase 6a – IAM & Cognito – actualizada»: frontend S3 por HTTP, Cognito Hosted UI, callback HTTPS en API Gateway, Lambda que canjea el código y regreso al frontend con tokens. La API de negocio continúa por HTTP en el ALB, por decisión del proyecto.

## Qué es cada componente

| Componente | Función |
|---|---|
| User Pool de Cognito | Directorio de usuarios, contraseñas, correo verificado, grupos y atributos. |
| Hosted UI | Pantalla de registro e inicio de sesión administrada por Cognito. |
| App client | Identifica a esta aplicación dentro del User Pool y sus URLs de retorno permitidas. Su secreto queda en Secrets Manager. |
| API Gateway `/callback` | Dirección HTTPS fija que Cognito acepta para devolver el código. No recibe operaciones de negocio. |
| Lambda `auth-callback` | Canjea el código con Cognito usando el secreto y redirige a S3. |
| Access Token | JWT firmado que autoriza llamadas a Flask. Incluye usuario, cliente, vencimiento y `cognito:groups`. |
| ID Token | JWT con información de perfil; el frontend lo usa para mostrar el correo. No autoriza la API. |
| Grupo de Cognito | Tipo de usuario de la aplicación. No necesita un rol IAM asociado. |
| `custom:sede_id` | ID de la sede que puede gestionar un portero o responsable. Solo lo escribe el operador AWS. |
| `LabRole` | Rol IAM usado por ECS y Lambda para llamar servicios AWS; es distinto del tipo de usuario de la aplicación. |

## Flujo de ingreso

1. El usuario abre S3 y pulsa **Ingresar**. El navegador genera un `state` aleatorio y lo guarda en `sessionStorage`.
2. Cognito Hosted UI autentica al usuario y devuelve un código al callback HTTPS de API Gateway.
3. API Gateway invoca `auth-callback`. La Lambda canjea el código por tokens en Cognito y redirige a S3 con `#access_token=...&id_token=...&state=...`.
4. El frontend comprueba que `state` corresponde a la sesión iniciada, guarda los tokens en `sessionStorage` y limpia el fragmento de la URL.
5. Cada petición a Flask envía `Authorization: Bearer <access_token>`.
6. Flask verifica firma RSA, emisor, expiración, tipo `access` y `client_id`. No acepta un ID Token como sustituto.
7. Flask invoca `gestionar-usuarios` por el endpoint privado de Lambda. Esta función, fuera de la VPC, consulta en Cognito el correo verificado, la sede y los grupos vigentes. Así se obtienen atributos que no vienen por defecto en el Access Token, sin dar salida a Internet a ECS.
8. Flask compara los grupos vigentes con los del token y aplica los permisos y filtros por sede. Si cambió el rol, exige volver a iniciar sesión. La interfaz solo oculta pantallas; la autoridad es Flask.

Se consulta Cognito en cada petición autenticada: es sencillo y permite detectar cambios de rol o deshabilitaciones, a costa de invocaciones adicionales. No se añadieron componentes AWS.

## Permisos

Cada usuario tiene exactamente uno de estos grupos de aplicación. Los grupos adicionales ajenos a estos roles no conceden permisos.

| Grupo | Puede hacer | Alcance |
|---|---|---|
| `transportistas` | Crear solicitudes, subir documentación y consultar estado. | Solo sus solicitudes, identificadas por su correo verificado. Puede solicitar acceso a distintas sedes. |
| `porteros` | Consultar solicitudes, verificar DNI/patente, registrar ingreso y egreso. | Solo `custom:sede_id`. No aprueba ni consulta reportes o auditoría. |
| `responsables_sede` | Aprobar/rechazar, consultar solicitudes, exportar reportes y leer auditoría. | Solo `custom:sede_id`. No registra ingresos ni egresos. |

Un usuario sin grupo puede activar su cuenta como transportista o esperar la asignación del operador. No puede elegirse portero o responsable. Si falta una sede válida, se deniega el acceso del personal. Ejemplo: un portero de sede 1 recibe 404 al intentar registrar el ingreso de una solicitud de sede 2, aunque conozca su ID.

## Asignación sencilla por el equipo del proyecto

El operador es quien accede a la consola/CLI del Lab; no es un cuarto rol de la aplicación.

1. El usuario se registra y verifica su correo en Hosted UI.
2. En Cognito, el operador asigna el grupo y el atributo `custom:sede_id` a los usuarios internos. Debe retirar los grupos de aplicación anteriores.
3. El usuario vuelve a iniciar sesión para obtener tokens actualizados.

El script evita tener que recordar las operaciones de CLI. Se ejecuta desde la raíz del repositorio, con credenciales del Lab y Terraform ya aplicado. No se ejecuta desde el navegador:

```bash
make grupo EMAIL=portero@example.com GRUPO=porteros SEDE=1
make grupo EMAIL=responsable@example.com GRUPO=responsables_sede SEDE=2
make grupo EMAIL=transportista@example.com GRUPO=transportistas
```

También admite un pool explícito:

```bash
python3 scripts/asignar-rol.py --pool us-east-1_ID_REAL --usuario persona@example.com --grupo porteros --sede 1
```

El ID de sede debe existir en RDS. En la base inicial: 1 Planta Central, 2 Depósito Norte, 3 Depósito Sur. El script comprueba usuario y grupo, retira las membresías anteriores, actualiza la sede y agrega el nuevo grupo. Si falla a mitad del proceso, revisar el error y repetir; puede quedar sin rol, sin recibir permisos adicionales.

## Correos por sede

El transportista se suscribe a sus propios avisos al activar su cuenta. SNS exige confirmar el correo. Si esa suscripción falla, el alta de rol permanece y el operador puede configurar la dirección en Terraform.

Para responsables, configurar `notificaciones_suscriptores` con el ID de sede que se asignó en Cognito:

```hcl
notificaciones_suscriptores = {
  "responsable@example.com" = "responsable-sede-2"
}
```

Las solicitudes nuevas publican `responsable-sede-N`; los avisos de aprobación/rechazo van al correo del solicitante. Al cambiar de sede, rol o correo, el operador debe actualizar la suscripción y su filtro. Los filtros SNS pueden tardar hasta 15 minutos en propagarse. Los porteros no reciben por defecto las solicitudes pendientes de aprobación.

## Actualización de un despliegue existente

- Cambiar valores antiguos `personal` por `responsable-sede-N` en `terraform.tfvars`.
- Publicar una nueva versión de la imagen de API y aplicar juntos frontend, Lambdas y Terraform. Las sesiones anteriores con solo ID Token requieren un nuevo ingreso.
- El grupo `administradores` se conserva temporalmente en Terraform, pero Flask no le concede permisos. Reasignar sus usuarios con el script, según la función real de cada persona. No se decide automáticamente quién debe ser portero o responsable.
- Se agrega la tabla `auditoria_sede` mediante `create_all` para asociar nuevos logs a una sede. Los logs históricos sin asociación permanecen en `log_auditoria`; no se muestran al personal porque su sede no puede acreditarse. No se borra el historial ni se infiere la sede a partir del texto.
- `LabRole` debe permitir `cognito-idp:AdminGetUser`, `AdminListGroupsForUser`, `AdminAddUserToGroup`, `sns:Subscribe` y la invocación de la Lambda. El operador también necesita las operaciones de asignación usadas por el script. La validación local no confirma estos permisos en la cuenta real.

## Pruebas locales

```bash
python3 -m pip install -r backend/requirements.txt
python3 -m unittest discover -s tests -v
node tests/test_frontend_auth.cjs
terraform -chdir=terraform/infra init -backend=false -lockfile=readonly
terraform -chdir=terraform/infra validate
```

La demostración sin AWS requiere `LOCAL_DEMO=true` al iniciar `backend/app.py`; no se habilita por la mera ausencia de JWKS. Los encabezados de demostración solo se usan en ese modo, que se rechaza si existen `DB_HOST` o `APP_ENV`. El frontend local permite elegir rol y sede para probar los mismos filtros de autorización.

Este cambio no implementa PKCE ni renovación automática de JWKS. El `state` protege la correlación del login y no sustituye a PKCE. El cierre de sesión borra tokens de esta aplicación; no revoca por sí solo la sesión de Hosted UI. Mantener frontend y API por HTTP conserva la exposición del código y credenciales de sesión en tránsito: el puente HTTPS del callback no cifra esos tramos.

## Fuentes

- Material proporcionado: «Clase 6a – IAM & Cognito – actualizada», páginas 27–29; en particular, paso 6 de la página 29: llamadas con Access Token.
- [AWS — Access tokens](https://docs.aws.amazon.com/cognito/latest/developerguide/amazon-cognito-user-pools-using-the-access-token.html)
- [AWS — Verificación de JWT](https://docs.aws.amazon.com/cognito/latest/developerguide/amazon-cognito-user-pools-using-tokens-verifying-a-jwt.html)
- [AWS — Grupos de Cognito](https://docs.aws.amazon.com/cognito/latest/developerguide/cognito-user-pools-user-groups.html)
- [AWS — AdminGetUser](https://docs.aws.amazon.com/cognito-user-identity-pools/latest/APIReference/API_AdminGetUser.html)
- [AWS — Authorization Code y state](https://docs.aws.amazon.com/cognito/latest/developerguide/authorization-endpoint.html)
- [AWS — Filtros SNS](https://docs.aws.amazon.com/sns/latest/dg/sns-subscription-filter-policies.html)
