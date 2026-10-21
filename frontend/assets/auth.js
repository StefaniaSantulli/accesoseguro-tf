// Workaround de clase: Cognito -> API Gateway /callback -> Lambda -> S3.
// El access token autoriza la API; el ID token solo muestra el perfil en pantalla.
const ROLES = { TRANSPORTISTA: "transportistas", PORTERO: "porteros", RESPONSABLE: "responsables_sede", SIN_ROL: "sin_rol", INVALIDO: "invalido" };
const GRUPOS_RESPONSABLE = [ROLES.RESPONSABLE];
const GRUPOS_PORTERIA = [ROLES.PORTERO];
const GRUPOS_VALIDOS = [ROLES.TRANSPORTISTA, ROLES.PORTERO, ROLES.RESPONSABLE];

function isCognitoMode() {
  return !!(window.APP_CONFIG && window.APP_CONFIG.loginUrl);
}
function storageGet(store, key) {
  try { return store.getItem(key); } catch (e) { return null; }
}
function storageSet(store, key, value) {
  try { store.setItem(key, value); return true; } catch (e) { return false; }
}
function storageDel(store, key) {
  try { store.removeItem(key); } catch (e) {}
}
function limpiarTokens() {
  storageDel(sessionStorage, "accesoseguro_access_token");
  storageDel(sessionStorage, "accesoseguro_id_token");
}
function leerClaims(token) {
  if (!token) return null;
  try {
    const payload = token.split(".")[1].replace(/-/g, "+").replace(/_/g, "/");
    const claims = JSON.parse(decodeURIComponent(escape(atob(payload))));
    return claims.exp && claims.exp * 1000 > Date.now() ? claims : null;
  } catch (e) { return null; }
}

(function capturarTokensDeLaUrl() {
  const params = new URLSearchParams(location.hash.slice(1));
  if (!params.has("access_token") && !params.has("id_token") && !params.has("error")) return;
  const esperado = storageGet(sessionStorage, "accesoseguro_oauth_state");
  history.replaceState(null, "", location.pathname + location.search);
  limpiarTokens();
  storageDel(sessionStorage, "accesoseguro_oauth_state");
  const access = leerClaims(params.get("access_token"));
  const id = leerClaims(params.get("id_token"));
  if (!esperado || params.get("state") !== esperado || params.has("error") ||
      !access || !id || access.token_use !== "access" || id.token_use !== "id" || access.sub !== id.sub) {
    storageSet(sessionStorage, "accesoseguro_login_error", "No se pudo completar el ingreso. Volvé a iniciar sesión.");
    return;
  }
  storageSet(sessionStorage, "accesoseguro_access_token", params.get("access_token"));
  storageSet(sessionStorage, "accesoseguro_id_token", params.get("id_token"));
})();

function getToken() {
  return isCognitoMode() ? storageGet(sessionStorage, "accesoseguro_access_token") : null;
}
function getClaims() {
  return leerClaims(storageGet(sessionStorage, "accesoseguro_id_token"));
}
function getGroups() {
  if (!isCognitoMode()) {
    const rol = storageGet(localStorage, "accesoseguro_rol");
    return GRUPOS_VALIDOS.includes(rol) ? [rol] : [];
  }
  const claims = leerClaims(getToken());
  return (claims && claims["cognito:groups"]) || [];
}
function hasAnyGroup(grupos) {
  return getGroups().some((g) => grupos.includes(g));
}
function getRole() {
  if (isCognitoMode() && !leerClaims(getToken())) return null;
  const grupos = getGroups();
  const roles = grupos.filter((g) => GRUPOS_VALIDOS.includes(g));
  if (roles.length > 1) return ROLES.INVALIDO;
  return roles[0] || (isCognitoMode() ? ROLES.SIN_ROL : null);
}
function setRole(rol) {
  if (GRUPOS_VALIDOS.includes(rol)) storageSet(localStorage, "accesoseguro_rol", rol);
}
function login() {
  if (!isCognitoMode()) return;
  // getRandomValues funciona también en el frontend HTTP del Lab.
  const bytes = crypto.getRandomValues(new Uint8Array(32));
  const state = Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
  if (!storageSet(sessionStorage, "accesoseguro_oauth_state", state)) {
    alert("Habilitá el almacenamiento de sesión del navegador para ingresar.");
    return;
  }
  const url = new URL(window.APP_CONFIG.loginUrl);
  url.searchParams.set("state", state);
  window.location.href = url.toString();
}
function refrescarSesion() {
  limpiarTokens();
  login();
}
function logout() {
  limpiarTokens();
  storageDel(sessionStorage, "accesoseguro_oauth_state");
  storageDel(localStorage, "accesoseguro_rol");
  window.location.href = "login.html";
}
function requireRole(rolesPermitidos) {
  const rol = getRole();
  if (!rol) { window.location.href = "login.html"; return null; }
  if ([ROLES.SIN_ROL, ROLES.INVALIDO].includes(rol)) {
    window.location.href = "elegir-rol.html"; return null;
  }
  if (!rolesPermitidos.includes(rol)) { window.location.href = "index.html"; return null; }
  return rol;
}

// Única lista de páginas: arma el menú superior (label) y las tarjetas del
// inicio (titulo y texto), filtradas según el rol del usuario.
const NAV = [
  { href: "nueva-solicitud.html", label: "Nueva solicitud", grupos: [ROLES.TRANSPORTISTA],
    titulo: "Nueva solicitud de acceso", texto: "Cargá tus datos y adjuntá documentación de respaldo." },
  { href: "mis-solicitudes.html", label: "Mis solicitudes", grupos: [ROLES.TRANSPORTISTA],
    titulo: "Mis solicitudes", texto: "Consultá el estado de tus solicitudes cargadas, buscando por tu DNI." },
  { href: "panel-aprobacion.html", label: "Aprobación", grupos: GRUPOS_RESPONSABLE,
    titulo: "Panel de aprobación", texto: "Revisá el documento adjunto de cada solicitud y aprobá o rechazá." },
  { href: "verificacion.html", label: "Portería", grupos: GRUPOS_PORTERIA,
    titulo: "Verificación en portería", texto: "Buscá por DNI o patente y registrá el ingreso o egreso." },
  { href: "reporte.html", label: "Reporte", grupos: GRUPOS_RESPONSABLE,
    titulo: "Reporte histórico", texto: "Exportá un CSV con el historial de tu sede, filtrado por fechas." },
  { href: "auditoria.html", label: "Auditoría", grupos: GRUPOS_RESPONSABLE,
    titulo: "Log de auditoría", texto: "Acciones registradas para mi sede." },
  { href: "solicitudes.html", label: "Solicitudes de mi sede", grupos: [...GRUPOS_RESPONSABLE, ...GRUPOS_PORTERIA],
    titulo: "Solicitudes de mi sede", texto: "Listado general con el estado actual de cada una." },
];
function linksVisibles() {
  return NAV.filter((l) => hasAnyGroup(l.grupos));
}
function renderNav() {
  const nav = document.getElementById("nav-links");
  if (!nav) return;
  nav.replaceChildren();
  for (const link of linksVisibles()) {
    const a = document.createElement("a");
    a.href = link.href; a.textContent = link.label; nav.appendChild(a);
  }
  const claims = getClaims();
  const quien = isCognitoMode() ? (claims && claims.email) : getRole();
  const salir = document.createElement("a");
  salir.href = "#"; salir.textContent = quien ? `Salir (${quien})` : "Salir";
  salir.addEventListener("click", (e) => { e.preventDefault(); logout(); });
  nav.appendChild(salir);
}
