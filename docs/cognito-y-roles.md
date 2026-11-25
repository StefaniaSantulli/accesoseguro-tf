# Cognito y permisos de AccesoSeguro

La aplicación conserva el workaround de las páginas 27–29 de «Clase 6a – IAM & Cognito – actualizada»: frontend S3 por HTTP, Cognito Hosted UI, callback HTTPS en API Gateway, Lambda que canjea el código y regreso al frontend con el ID token. La API de negocio entra por el mismo API Gateway (HTTPS), que valida el token de Cognito con un authorizer JWT y llega a Flask por un VPC Link hacia un ALB interno.

## Qué es cada componente

| Componente | Función |
|---|---|
| User Pool de Cognito | Directorio de usuarios, contraseñas, correo verificado, grupos y atributos. |
| Hosted UI | Pantalla de registro e inicio de sesión administrada por Cognito. |
| App client | Identifica a esta aplicación dentro del User Pool y sus URLs de retorno permitidas. Su secreto queda en Secrets Manager. |
| API Gateway | Única entrada pública (HTTPS). `GET /callback`: retorno del login. `/api/*`: la API, protegida por un authorizer JWT de Cognito. |
| Authorizer JWT | Valida el ID Token antes de llegar a Flask: firma (llaves públicas del User Pool, que renueva solo), emisor, audiencia y vencimiento. Token inválido: 401 sin consumir ECS. |
| VPC Link + ALB interno | Camino privado de API Gateway a ECS. El ALB no es alcanzable desde internet: su SG solo acepta el VPC Link. |
| Lambda `auth-callback` | Canjea el código con Cognito usando el secreto y redirige a S3. |
| ID Token | JWT firmado que autoriza las llamadas a la API. Trae `sub`, `email`, `email_verified`, `custom:sede_id`, `custom:empresa_id` y `cognito:groups`: todo lo que Flask necesita para autorizar. |
| Access Token | Cognito lo entrega a la Lambda durante el canje. El callback devuelve al navegador únicamente el ID token. |
| Grupo de Cognito | Tipo de usuario de la aplicación. No necesita un rol IAM asociado. |
| `custom:sede_id` | ID de la sede que puede gestionar un portero o responsable. Solo lo escribe el operador AWS. |
| `LabRole` | Rol IAM usado por ECS y Lambda para llamar servicios AWS; es distinto del tipo de usuario de la aplicación. |

## Flujo de ingreso

1. El usuario abre S3 y pulsa **Ingresar**. El navegador genera un `state` aleatorio y lo guarda en `sessionStorage`.
2. Cognito Hosted UI autentica al usuario y devuelve un código al callback HTTPS de API Gateway.
3. API Gateway invoca `auth-callback`. La Lambda canjea el código por tokens en Cognito y redirige a S3 con `#id_token=...&state=...`.
4. El frontend comprueba que `state` corresponde a la sesión iniciada, guarda el ID token en `sessionStorage` y limpia el fragmento de la URL.
5. Cada petición a la API envía `Authorization: Bearer <id_token>` a API Gateway.
6. El authorizer JWT de API Gateway verifica firma, emisor (el User Pool), audiencia (el app client) y vencimiento. Si falla, responde 401 y el pedido no llega a ECS.
7. API Gateway reenvía el pedido por el VPC Link al ALB interno y agrega el header `X-Authorizer-Sub` con el `sub` del token validado (pisa cualquier valor que mande el cliente).
8. Flask exige que `X-Authorizer-Sub` coincida con el token (prueba de que pasó por el authorizer), controla emisor, audiencia, vencimiento y tipo `id`, y toma del token el correo verificado, la sede, la empresa y el grupo. Aplica los permisos y filtros por sede. La interfaz solo oculta pantallas; la autoridad es Flask.

Ya no se consulta Cognito en cada petición. `gestionar-usuarios` se invoca solo para el alta como transportista y la suscripción a los mails. Compromiso aceptado: si el operador cambia el rol o deshabilita a un usuario, el cambio se aplica cuando vence su ID Token (60 minutos) o cuando vuelve a iniciar sesión.

## Permisos

Cada usuario tiene exactamente uno de estos grupos de aplicación. Los grupos adicionales ajenos a estos roles no conceden permisos.

| Grupo | Puede hacer | Alcance |
|---|---|---|
| `transportistas` | Crear solicitudes, subir documentación y consultar estado. | Solo sus solicitudes, identificadas por el `sub` de Cognito guardado al crearlas. Puede solicitar acceso a distintas sedes. |
| `porteros` | Consultar solicitudes, verificar DNI/patente, registrar ingreso y egreso. | Solo `custom:sede_id`. No aprueba ni consulta reportes o auditoría. |
| `responsables_sede` | Aprobar/rechazar, consultar solicitudes, exportar reportes y leer auditoría. | Solo `custom:sede_id`. No registra ingresos ni egresos. |

Un usuario sin grupo puede activar su cuenta como transportista o esperar la asignación del operador. No puede elegirse portero o responsable. Si falta una sede válida, se deniega el acceso del personal. Ejemplo: un portero de sede 1 recibe 404 al intentar registrar el ingreso de una solicitud de sede 2, aunque conozca su ID.

## Asignación sencilla por el equipo del proyecto

El operador es quien accede a la consola/CLI del Lab; no es un cuarto rol de la aplicación.

1. El usuario se registra y verifica su correo en Hosted UI.
2. En Cognito, el operador asigna el grupo y el atributo `custom:sede_id` a los usuarios internos. Debe retirar los grupos de aplicación anteriores.
3. El usuario vuelve a iniciar sesión para obtener tokens actualizados.

El script evita tener que recordar las operaciones de CLI. Se ejecuta desde la raíz del repositorio, con credenciales del Lab y Terraform conectado al state del despliegue. Para preparar otra copia local, ver [el README](../README.md#probar-la-aplicación). No se ejecuta desde el navegador:

```bash
make grupo EMAIL=portero@example.com GRUPO=porteros SEDE=1
make grupo EMAIL=responsable@example.com GRUPO=responsables_sede SEDE=2
make grupo EMAIL=transportista@example.com GRUPO=transportistas
```

También admite un pool explícito:

```bash
python3 scripts/asignar-rol.py --pool us-east-1_ID_REAL --notificaciones-lambda accesoseguro-dev-gestionar-usuarios --usuario persona@example.com --grupo porteros --sede 1
```

El ID de sede debe existir en RDS. En la base inicial: 1 Planta Central, 2 Depósito Norte, 3 Depósito Sur. El script comprueba usuario y grupo, retira las membresías anteriores, actualiza la sede y agrega el nuevo grupo. Si falla a mitad del proceso, revisar el error y repetir; puede quedar sin rol, sin recibir permisos adicionales.

## Correos por sede

El alta de transportista intenta suscribir su correo verificado. Todos los usuarios pueden reintentar desde **Activar o actualizar correos** en el inicio. El script de roles sincroniza el filtro tras asignar rol/sede. El usuario debe confirmar el correo de Amazon SNS; Cognito no confirma esa suscripción.

Los filtros usan `usuario:<sub>` y, para responsables, `responsable-sede-N`. Las suscripciones las administra `gestionar-usuarios` (Terraform crea el tópico, no las suscripciones). Ver [circuito y recuperación de correos](notificaciones.md).

## Auditoría por sede

Cada registro de `log_auditoria` que corresponde a una sede se asocia en `auditoria_sede`, y cada responsable consulta solo los de la suya. Las acciones sin sede (por ejemplo, la activación de cuenta de un transportista) quedan en `log_auditoria` pero no se muestran al personal de planta.

## Permisos de AWS requeridos

- `LabRole` debe permitir `cognito-idp:AdminGetUser`, `AdminListGroupsForUser`, `AdminAddUserToGroup`, las operaciones SNS detalladas en [notificaciones.md](notificaciones.md) y la invocación de la Lambda. El operador también necesita las operaciones de asignación usadas por el script. La validación local no confirma estos permisos en la cuenta real.

## Pruebas locales

```bash
python3 -m pip install -r backend/requirements.txt
python3 -m unittest discover -s tests -v
node tests/test_frontend_auth.cjs
terraform -chdir=terraform/infra init -backend=false -lockfile=readonly
terraform -chdir=terraform/infra validate
```

La demostración sin AWS requiere `LOCAL_DEMO=true` al iniciar `backend/app.py`. Los encabezados de demostración solo se usan en ese modo, que se rechaza si existen `DB_HOST` o `APP_ENV`. El frontend local permite elegir rol y sede para probar los mismos filtros de autorización.

El login usa `state` para correlacionar el retorno de Cognito. Al cerrar sesión se borra el ID token del navegador. API Gateway gestiona las claves públicas del emisor. El sitio de S3 se sirve por HTTP por la restricción del laboratorio. Tanto el callback como las llamadas del navegador a la API usan HTTPS. [Authorizer JWT de API Gateway](https://docs.aws.amazon.com/apigateway/latest/developerguide/http-api-jwt-authorizer.html).

## Fuentes

- Material proporcionado: «Clase 6a – IAM & Cognito – actualizada», páginas 27–29; en particular, paso 6 de la página 29: llamadas con Access Token.
- [AWS — Access tokens](https://docs.aws.amazon.com/cognito/latest/developerguide/amazon-cognito-user-pools-using-the-access-token.html)
- [AWS — Verificación de JWT](https://docs.aws.amazon.com/cognito/latest/developerguide/amazon-cognito-user-pools-using-tokens-verifying-a-jwt.html)
- [AWS — Grupos de Cognito](https://docs.aws.amazon.com/cognito/latest/developerguide/cognito-user-pools-user-groups.html)
- [AWS — AdminGetUser](https://docs.aws.amazon.com/cognito-user-identity-pools/latest/APIReference/API_AdminGetUser.html)
- [AWS — Authorization Code y state](https://docs.aws.amazon.com/cognito/latest/developerguide/authorization-endpoint.html)
- [AWS — Filtros SNS](https://docs.aws.amazon.com/sns/latest/dg/sns-subscription-filter-policies.html)
