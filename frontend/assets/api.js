// Configuración de la API backend.
// En AWS, config.js (generado por Terraform) trae la URL del ALB en
// window.APP_CONFIG.apiUrl. En local (sin config.js) se usa localhost:5000.
const API_BASE_URL =
  (window.APP_CONFIG && window.APP_CONFIG.apiUrl) || "http://localhost:5000";

// Agrega el token de Cognito a cada pedido. Si la API responde 401 (token
// vencido o inválido) se vuelve al login.
function authHeaders(extra) {
  const token = getToken();
  const headers = token ? { Authorization: `Bearer ${token}` } : {};
  if (!isCognitoMode()) {
    headers["X-Demo-Role"] = getRole() || "";
    headers["X-Demo-Sede"] = storageGet(localStorage, "accesoseguro_demo_sede") || "1";
  }
  return Object.assign({}, extra || {}, headers);
}

async function apiFetch(path, options) {
  const res = await fetch(`${API_BASE_URL}${path}`, options);
  if (res.status === 401 && isCognitoMode()) {
    logout();
    throw new Error("La sesión venció, volvé a ingresar");
  }
  if (!res.ok) {
    const detalle = await res.json().catch(() => ({}));
    throw new Error(detalle.error || `Error ${res.status} al consultar ${path}`);
  }
  return res;
}

async function apiGet(path) {
  const res = await apiFetch(path, { headers: authHeaders() });
  return res.json();
}

async function apiPostJSON(path, data) {
  const res = await apiFetch(path, {
    method: "POST",
    headers: authHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify(data),
  });
  return res.json();
}

async function apiPostForm(path, formData) {
  const res = await apiFetch(path, { method: "POST", headers: authHeaders(), body: formData });
  return res.json();
}

// Descarga un archivo (ej. el CSV del reporte) enviando el token.
async function apiDownload(path, filename) {
  const res = await apiFetch(path, { headers: authHeaders() });
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  // Safari cancela la descarga si el archivo se libera enseguida: se espera.
  setTimeout(() => URL.revokeObjectURL(url), 30000);
}

// Sube un PDF directo a S3 con una presigned URL (no pasa por la API).
async function subirDocumento(solicitudId, archivo) {
  const { url, max_mb } = await apiPostJSON(`/api/solicitudes/${solicitudId}/documento`, {});
  if (archivo.size > max_mb * 1024 * 1024) throw new Error(`El PDF supera los ${max_mb} MB`);
  const res = await fetch(url, {
    method: "PUT",
    headers: { "Content-Type": "application/pdf" },
    body: archivo,
  });
  if (!res.ok) throw new Error(`S3 respondió ${res.status} al subir el documento`);
}
