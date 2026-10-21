# Correos y recuperación de avisos

## Para entenderlo sin conocimientos técnicos

La aplicación lleva una libreta de avisos pendientes. Cuando alguien crea una solicitud o un responsable la resuelve, guarda la operación y su aviso juntos. Si se corta la conexión con AWS, la operación no se pierde y el aviso sigue en la libreta.

1. **Flask + RDS:** guardan solicitud, auditoría y aviso pendiente en una misma transacción.
2. **SNS de eventos:** recibe el aviso y reparte una copia a auditoría y otra a notificaciones.
3. **SQS:** guarda el trabajo hasta que pueda procesarse.
4. **Lambda `notificar-mail`:** consulta RDS para reconocer avisos ya procesados, comprueba que exista un destinatario confirmado y prepara el texto.
5. **SNS de correos:** entrega el aviso a las direcciones cuyos filtros coinciden.
6. **Recuperador `vencer-permisos`:** consulta RDS de verdad, vence permisos y vuelve a intentar publicar los eventos pendientes. Por defecto corre cada 15 minutos (`vencer_permisos_intervalo_minutos`).

La publicación se intenta inmediatamente después de guardar una operación. El recuperador es el respaldo si ese intento falla. El intervalo de recuperación no es la vigencia del permiso: `permiso_vigencia_segundos` configura esta última (por defecto, 86400 segundos = 24 horas desde la aprobación, si no hubo ingreso). En la demo local no hay vencimientos: los ejecuta esta Lambda en AWS.

## Quién recibe cada aviso

| Suceso | Destinatario |
|---|---|
| Solicitud nueva | Responsables suscriptos a la sede de la solicitud. |
| Aprobación, rechazo o vencimiento | Usuario que creó la solicitud, identificado por su `sub` de Cognito. |
| Fallos técnicos | Operador que confirme la suscripción de `alarm_email`. |

El filtro es `destinatario_id`: `usuario:<sub>` para avisos personales y `responsable-sede-N` para avisos de sede. El correo es solamente el destino de entrega de la suscripción; no identifica al dueño del aviso. El backend toma el `sub` del token verificado, nunca de datos enviados por el navegador.

El correo contiene el número de solicitud, sede, tipo de suceso, fecha y enlace. Los motivos de rechazo, documentos, DNI y demás detalles se consultan después de ingresar a la aplicación. Las colas estándar no garantizan orden: el texto invita a consultar el estado actual, y conserva la fecha del evento.

## Activar los correos

- Transportista: el alta intenta suscribirlo. Si falla, el rol se conserva; puede reintentar desde el inicio.
- Cualquier usuario con rol válido: botón **Activar o actualizar correos** en el inicio. Usa el correo verificado y los permisos actuales de Cognito.
- Operador: `python3 scripts/notificaciones.py --usuario persona@example.com`.
- `make grupo EMAIL=persona@example.com GRUPO=responsables_sede SEDE=2` asigna el rol y después sincroniza los correos. Si la segunda operación falla, el comando lo informa; no afirma que se haya revertido el rol.
- El usuario debe confirmar el mensaje de **Amazon SNS**. Verificar el correo en Cognito es un paso diferente.
- Los filtros pueden tardar hasta **15 minutos** en propagarse. Activar y confirmar antes de probar solicitudes.

`gestionar-usuarios` es el único administrador de las suscripciones de negocio. Terraform crea los tópicos y colas, pero no administra las direcciones individuales. La Lambda actualiza su filtro en el mismo ARN cuando están confirmadas, y retira una dirección anterior si la encuentra vinculada al mismo `sub`.

Si se cambia el grupo o la sede directamente en la consola, ejecutar también `scripts/notificaciones.py`. Cambiar o deshabilitar una cuenta en Cognito no modifica por sí solo una suscripción SNS. Para una baja, el operador debe eliminar su suscripción. El script de roles hace la sincronización automáticamente. El filtrado SNS no reemplaza los permisos del backend ni es instantáneo.

## Qué se registra y qué significa

- `solicitud_destinatario`: vincula cada solicitud con el `sub` de su creador, en la misma transacción que la crea.
- `evento_pendiente`: una fila por cambio, con UUID estable, contenido, intentos y próxima fecha de reintento. Se guarda en la misma transacción que la solicitud y la auditoría.
- `publicado` y `sns_message_id`: SNS de eventos aceptó el mensaje.
- `mail_publicado` y `mail_message_id`: SNS de correos aceptó el aviso. **No prueban entrega a la bandeja, lectura ni ausencia de spam.**

El recuperador aplica esperas progresivas (desde 60 segundos hasta una hora), procesa hasta 100 eventos por invocación y detiene el trabajo antes de agotar el tiempo disponible. Un evento cuya solicitud no tiene dueño registrado permanece pendiente y dispara la alarma de atraso; nunca se reemplaza su destinatario por el correo ni por un envío general.

El consumidor comprueba el evento contra la copia registrada en RDS y bloquea esa fila durante la publicación. Si el mismo evento llega de nuevo y ya tiene `mail_publicado`, lo omite. No publica si el tópico tiene una suscripción email confirmada sin filtro seguro; esa configuración requiere reparación del operador.

**Límite real:** SNS y PostgreSQL no comparten una transacción. Si SNS acepta el correo y el proceso se cae antes de guardar ese resultado, un reintento podría repetirlo. Se prioriza no descartar el aviso. No se promete entrega exactamente una vez. El UUID y los identificadores SNS permiten investigar esa situación.

## Fallos y recuperación

| Dónde falla | Qué ocurre |
|---|---|
| Guardar solicitud/auditoría/aviso | Se revierte toda la transacción; no se publica nada. |
| Publicar desde RDS a SNS | El evento queda pendiente con próxima fecha de reintento. |
| Entregar desde SNS a SQS | SNS usa su política de reintentos y una DLQ de entrega para cada suscripción. |
| Procesar SQS o publicar el correo | Solo falla ese mensaje; después de los intentos configurados pasa a la DLQ de procesamiento. |
| Falta suscripción confirmada | Se trata como fallo de procesamiento; no se marca como enviado. Confirmar/sincronizar y recuperar desde la DLQ si ya llegó allí. |
| SNS no puede entregar al servidor de correo | Se conserva en la DLQ de entrega de correo tras la política de reintentos de SNS, cuando corresponde. El correo en spam/lectura no es detectable con este circuito. |

Las DLQ tienen retención finita (14 días por defecto); no son un archivo permanente. El operador debe revisar las alarmas a tiempo. Reparado el problema, la DLQ de procesamiento puede redirigirse a su cola original desde SQS. Revisar cuerpo y destinatario antes de recuperar DLQ de entrega: el mensaje SNS de correo es texto, no el JSON del evento interno, y no debe enviarse directamente al consumidor de eventos.

Alarmas: evento pendiente durante más de dos ejecuciones del recuperador (mínimo 15 minutos), falta de métricas del recuperador, fallos parciales de correo, mensajes en las DLQ de entrega y de procesamiento, y errores de las Lambdas asincrónicas. Configurar `alarm_email` y confirmar esa suscripción; de lo contrario las alarmas existen pero no llegan a una persona por correo.

## Despliegue

Requiere Terraform 1.10 del proyecto, AWS CLI y credenciales habilitadas del Lab.

1. Ejecutar `make layer` (Python con pip y curl). Descarga dependencias **Linux x86_64/Python 3.12**, el certificado público oficial de RDS, y empaqueta el código común. La Layer permite conexiones TLS con verificación del certificado de RDS. Terraform detecta fuentes desactualizadas y pide reconstruirla.
2. Ejecutar el workflow **desplegar-aplicacion** (o `make deploy`). El pipeline prepara las tablas con una tarea puntual de ECS antes de habilitar la API y los workers. Ver [despliegue TP3](../README.md#7-paso-a-paso).
3. Cada usuario activa y confirma su suscripción; esperar la propagación de filtros.
4. Confirmar `alarm_email`. Probar solicitud nueva, aprobación/rechazo, vencimiento y una recuperación controlada de la DLQ con cuentas y sedes de prueba. Para probar el vencimiento sin esperar 24 horas, bajar `permiso_vigencia_segundos` en `terraform.tfvars`.

**Infraestructura involucrada:** `notificar-mail` corre en las subredes privadas para consultar RDS y controlar duplicados, con los security groups y endpoints SNS/Secrets Manager del proyecto; no hay NAT, otra base ni DynamoDB. Hay una Layer compartida y tres DLQ de entrega (dos para el reparto de eventos y una para correos).

Permisos por verificar en el Lab: SNS Publish/Subscribe/ListSubscriptionsByTopic/GetSubscriptionAttributes/SetSubscriptionAttributes/Unsubscribe sobre los recursos del proyecto; Cognito AdminGetUser/AdminListGroupsForUser/AdminAddUserToGroup; Lambda InvokeFunction para el operador/API; lectura del secreto y KMS; conectividad Lambda-VPC y acceso SQS del consumidor. Se sigue usando LabRole por la restricción del laboratorio. En producción, separar roles de ejecución con mínimo privilegio y usuarios SQL con permisos limitados por función.

## Validación

`python3 -m unittest discover -s tests -v` verifica permisos, tokens, outbox, fallos de SNS, destinatarios, transacciones, vencimientos y fallos parciales. Usa clientes AWS simulados y SQLite para las pruebas rápidas.

`tests/test_notifications_postgres.py` comprueba bloqueos y concurrencia con PostgreSQL real cuando se define `TEST_POSTGRES_URL` hacia una base **de pruebas**. El workflow `pruebas` levanta PostgreSQL aislado y también importa la Layer en Linux. Esas pruebas se omiten localmente si no hay PostgreSQL; configurar un workflow no equivale a haberlo ejecutado.

`node tests/test_frontend_auth.cjs` y `terraform -chdir=terraform/infra validate` completan las comprobaciones locales. La confirmación y entrega real de correo, permisos IAM/KMS y conectividad privada requieren una prueba en AWS.

## Fuentes

- [AWS: transactional outbox](https://docs.aws.amazon.com/prescriptive-guidance/latest/cloud-design-patterns/transactional-outbox.html).
- [AWS: SQS con Lambda, duplicados y fallos parciales](https://docs.aws.amazon.com/lambda/latest/dg/with-sqs.html).
- [AWS: confirmación de correos SNS](https://docs.aws.amazon.com/sns/latest/dg/sns-email-notifications.html).
- [AWS: filtros y propagación](https://docs.aws.amazon.com/sns/latest/dg/sns-subscription-filter-policies.html).
- [AWS: DLQ de SNS](https://docs.aws.amazon.com/sns/latest/dg/sns-dead-letter-queues.html).
- [AWS: dependencias Linux en Layers](https://docs.aws.amazon.com/lambda/latest/dg/python-layers.html).
- [AWS: TLS y certificados de RDS](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/UsingWithRDS.SSL.html).
