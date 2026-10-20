# AccesoSeguro v3 — frontend estático separado del backend

## Guía rápida para el equipo (correrlo en tu computadora)

Necesitás tener Python 3 instalado (`python3 --version` para chequear).

```
git clone https://github.com/StefaniaSantulli/cloud-tp1.git
cd cloud-tp1
```

**Terminal 1 — backend** (queda corriendo, no la cierres):
```
cd backend
pip3 install -r requirements.txt
python3 app.py
```

**Terminal 2 — frontend** (una Terminal nueva, también queda corriendo):
```
cd frontend
python3 -m http.server 8000
```

Con las dos corriendo, abrí en el navegador:
```
http://localhost:8000/login.html
```

No hace falta AWS ni credenciales para probarlo así: el backend usa una base
SQLite local automáticamente. Si adjuntás un documento en "Nueva solicitud"
va a tirar un error en la consola del backend (no hay credenciales de AWS
configuradas para S3), pero no rompe nada — para probar el flujo, mejor no
adjuntar archivo mientras se prueba en local.

---


Este paquete es el primer paso de la corrección que pidió el profesor: separar
front y back, y que el frontend sea puramente estático. No incluye todavía
las otras correcciones (claves de S3 por empresa, presigned URLs de subida,
lifecycle policy, justificación Multi-AZ vs Read Replica) — esas quedan para
el siguiente paso, ya con esta base separada.

## Qué cambió respecto a la versión anterior

- **`backend/app.py`**: ya no usa `render_template` ni Jinja2. Cada ruta
  devuelve JSON (o, en el caso del reporte, un CSV para descargar). Se agregó
  `Flask-Cors` porque ahora el frontend va a vivir en otro origen (otro
  dominio/puerto) que el backend, y el navegador exige CORS para permitir que
  el JavaScript del frontend llame a la API.
- **`backend/requirements.txt`**: se agregó `Flask-Cors` y `psycopg[binary]`
  (esto último ya se había detectado como faltante en el repo compartido).
- **`frontend/`**: HTML/CSS/JS plano, sin build step, listo para subir a un
  bucket S3 con website hosting (o detrás de CloudFront). Reemplaza a la
  carpeta `templates/` anterior.
- **Separación por rol** (conductor / admin): se agregó una pantalla
  `login.html` donde se elige el rol, y cada pantalla interna valida con
  `requireRole([...])` que el rol elegido puede verla. **Importante:** esto
  es un selector del lado del cliente, no un login real — no hay contraseña
  ni el backend valida el rol. Decide qué pantallas mostrar, pero no es una
  medida de seguridad. Si más adelante quieren seguridad real, hay que
  agregar una tabla `Usuario` con contraseñas hasheadas y que el backend
  exija un token en los endpoints de admin.
- Se agregó una pantalla nueva para el rol conductor, **`mis-solicitudes.html`**,
  que no existía antes: permite consultar el estado de las propias
  solicitudes buscando por DNI (antes solo existía la búsqueda de portería,
  pensada para el guardia, que solo muestra solicitudes aprobadas).

## Cómo queda repartido el trabajo entre front y back

| Antes | Ahora |
|---|---|
| Flask renderiza HTML con Jinja2 (`render_template`) | Flask devuelve JSON puro en endpoints `/api/...` |
| El HTML vive en `templates/`, servido por el EC2 privado detrás del ALB | El HTML/CSS/JS vive en un bucket S3 (o S3+CloudFront), servido directo al navegador |
| Una sola "app" mezcla presentación y lógica | Dos piezas independientes que se comunican por HTTP/JSON (con CORS) |

## Endpoints de la API (`backend/app.py`)

| Método | Ruta | Para qué |
|---|---|---|
| GET | `/` | Health check (lo usa el Target Group del ALB) |
| GET | `/api/sedes` | Combo de sedes |
| POST | `/api/solicitudes` | Crear una solicitud (conductor) |
| GET | `/api/mis-solicitudes?dni=...` | Consultar mis solicitudes (conductor) |
| GET | `/api/solicitudes/pendientes` | Panel de aprobación (admin) |
| POST | `/api/solicitudes/<id>/resolver` | Aprobar/rechazar (admin) |
| GET | `/api/solicitudes` | Todas las solicitudes (admin) |
| GET | `/api/verificacion?busqueda=...` | Verificación en portería (admin) |
| POST | `/api/solicitudes/<id>/ingreso` | Registrar ingreso (admin) |
| POST | `/api/solicitudes/<id>/egreso` | Registrar egreso (admin) |
| GET | `/api/auditoria` | Log de auditoría (admin) |
| GET | `/api/reporte/exportar?sede_id=&desde=&hasta=` | Descargar CSV (admin) |
| GET | `/api/expirar-vencidas` | Tarea de expiración (demo manual) |

## Pasos para desplegar

### 1. Backend (EC2 / ASG / ALB — reusa lo que ya tenés)

1. Reemplazá `app.py` y `requirements.txt` en tu repo/AMI por los de
   `backend/` acá.
2. Al hornear la nueva AMI (o al actualizar el User Data), instalá las
   dependencias nuevas: `pip install -r requirements.txt` (esto instala
   `flask-cors`).
3. Opcional pero recomendado: seteá la variable de entorno
   `FRONTEND_ORIGIN` en el User Data con el dominio real de tu frontend
   (por ejemplo `https://tu-distribucion.cloudfront.net`) para no dejar
   CORS abierto a cualquier origen (`*`). Mientras estás probando, `*`
   funciona.
4. **No hace falta tocar el Target Group**: sigue apuntando a `/` en el
   puerto 5000, que ahora devuelve `{"status": "ok", ...}` en vez del home
   renderizado, así que el health check sigue funcionando igual.

### 2. Frontend (bucket S3 nuevo)

1. Creá un bucket S3 nuevo (por ejemplo `accesoseguro-frontend-2026-stefi`).
   Importante: **es un bucket distinto** al que ya usás para los documentos
   (`accesoseguro-documentos-2026...`) — no hay que mezclarlos.
2. Antes de subir nada, editá `frontend/assets/api.js` y confirmá que
   `API_BASE_URL` apunta al DNS real de tu ALB (ya está puesto el que
   usaste: `accesoseguro-alb-542552161.us-east-1.elb.amazonaws.com`, con
   `http://` porque el ALB todavía no tiene HTTPS).
3. Habilitá **Static website hosting** en el bucket (Properties → Static
   website hosting → Enable), con `index.html` como documento de índice.
4. Subí el contenido de `frontend/` (todo, incluida la carpeta `assets/`)
   a la raíz del bucket.
5. Para que sea accesible públicamente necesitás una bucket policy de
   lectura pública (`s3:GetObject` para `Principal: "*"`) o, mejor,
   ponerlo detrás de CloudFront con Origin Access Control (así no hace
   falta bucket público y de paso sumás HTTPS, que es otra corrección
   pendiente del profesor).

### 3. Probar

1. Abrí la URL del sitio estático (la de "Bucket website endpoint", o la
   de CloudFront si la agregaste).
2. Elegí un rol en `login.html`.
3. Probá el flujo conductor: cargar una solicitud nueva, y después buscarla
   en "Mis solicitudes" por DNI.
4. Cambiá a rol admin (botón "Salir" en la barra de navegación) y probá
   aprobar esa solicitud, verificarla en portería, y ver que aparezca en
   auditoría y en el reporte.

Si algo falla con un error de CORS en la consola del navegador, es casi
siempre porque `FRONTEND_ORIGIN` en el backend no coincide con el origen
real desde el que se está sirviendo el frontend (protocolo + dominio, sin
barra al final).

## Qué queda pendiente del resto del feedback del profesor

Esto es solo el primer paso. Todavía falta, en el orden sugerido:

1. Namespacing de las claves de S3 por empresa/usuario (hoy son
   `solicitudes/{id}_{nombre_archivo}`, planas).
2. Presigned URLs también para la **subida** del documento (hoy la subida
   sigue pasando por el backend con `s3.upload_fileobj`; solo la
   *descarga/visualización* ya usaba presigned URLs). Esto haría que el
   navegador suba el archivo directo a S3, sin pasar por el ALB/EC2.
3. Lifecycle policy en el bucket de documentos.
4. Justificación escrita de Multi-AZ vs Read Replica (y evaluar si conviene
   sumar una Read Replica para el endpoint de verificación, que es de
   lectura intensiva).
