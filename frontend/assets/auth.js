// Autenticación y roles.
//
// En AWS (config.js existe): login real con la Hosted UI de Cognito. Al
// volver, la Lambda auth-callback deja los tokens después del "#" en la URL;
// se guardan en sessionStorage y se limpia la URL. El rol sale de los grupos
// del usuario (cognito:groups), y el backend vuelve a validar el token y el
// grupo en cada pedido: esto del front solo decide qué pantallas mostrar.
//
// En local (sin config.js): se mantiene el selector de rol de la v3.

const ROLES = { CONDUCTOR: "conductor", ADMIN: "admin" };
const GRUPOS_RESPONSABLE = ["responsables_sede", "administradores"];
const GRUPOS_PORTERIA = ["porteria", "administradores"];

function isCognitoMode() {
  return !!(window.APP_CONFIG && window.APP_CONFIG.loginUrl);
}

function storageGet(store, key) {
  try { return store.getItem(key); } catch (e) { return null; }
}
function storageSet(store, key, value) {
  try { store.setItem(key, value); } catch (e) { /* sin storage, seguimos */ }
}
function storageDel(store, key) {
  try { store.removeItem(key); } catch (e) {}
}

// --- Tokens de Cognito ------------------------------------------------------
(function capturarTokensDeLaUrl() {
  if (!location.hash.includes("id_token=")) return;
  const params = new URLSearchParams(location.hash.slice(1));
  storageSet(sessionStorage, "accesoseguro_id_token", params.get("id_token"));
  history.replaceState(null, "", location.pathname + location.search);
})();

function getToken() {
  return isCognitoMode() ? storageGet(sessionStorage, "accesoseguro_id_token") : null;
}

function getClaims() {
  const token = getToken();
  if (!token) return null;
  try {
    const payload = token.split(".")[1].replace(/-/g, "+").replace(/_/g, "/");
    const claims = JSON.parse(decodeURIComponent(escape(atob(payload))));
    if (claims.exp && claims.exp * 1000 < Date.now()) return null; // vencido
    return claims;
  } catch (e) {
    return null;
  }
}

function getGroups() {
  const claims = getClaims();
  return (claims && claims["cognito:groups"]) || [];
}

function hasAnyGroup(grupos) {
  return getGroups().some((g) => grupos.includes(g));
}

// --- Rol --------------------------------------------------------------------
function getRole() {
  if (isCognitoMode()) {
    if (!getClaims()) return null;
    return hasAnyGroup([...GRUPOS_RESPONSABLE, ...GRUPOS_PORTERIA]) ? ROLES.ADMIN : ROLES.CONDUCTOR;
  }
  return storageGet(localStorage, "accesoseguro_rol");
}

function setRole(rol) {
  storageSet(localStorage, "accesoseguro_rol", rol);
}

function login() {
  if (isCognitoMode()) window.location.href = window.APP_CONFIG.loginUrl;
}

function logout() {
  storageDel(sessionStorage, "accesoseguro_id_token");
  storageDel(localStorage, "accesoseguro_rol");
  window.location.href = "login.html";
}

// Redirige a login si no hay sesión, o a index si el rol no puede ver la página.
function requireRole(rolesPermitidos) {
  const rol = getRole();
  if (!rol) {
    window.location.href = "login.html";
    return null;
  }
  if (!rolesPermitidos.includes(rol)) {
    window.location.href = "index.html";
    return null;
  }
  return rol;
}

// --- Navegación ---------------------------------------------------------------
const NAV_CONDUCTOR = [
  { href: "nueva-solicitud.html", label: "Nueva solicitud" },
  { href: "mis-solicitudes.html", label: "Mis solicitudes" },
];

// grupos: quién ve cada link en modo Cognito (en local se ven todos).
const NAV_ADMIN = [
  { href: "panel-aprobacion.html", label: "Aprobación", grupos: GRUPOS_RESPONSABLE },
  { href: "solicitudes.html", label: "Todas", grupos: [...GRUPOS_RESPONSABLE, ...GRUPOS_PORTERIA] },
  { href: "verificacion.html", label: "Verificación", grupos: GRUPOS_PORTERIA },
  { href: "reporte.html", label: "Reporte", grupos: GRUPOS_RESPONSABLE },
  { href: "auditoria.html", label: "Auditoría", grupos: GRUPOS_RESPONSABLE },
];

function linksVisibles() {
  const rol = getRole();
  if (rol !== ROLES.ADMIN) return NAV_CONDUCTOR;
  return isCognitoMode() ? NAV_ADMIN.filter((l) => hasAnyGroup(l.grupos)) : NAV_ADMIN;
}

function renderNav() {
  const nav = document.getElementById("nav-links");
  if (!nav) return;
  const claims = getClaims();
  const quien = isCognitoMode() ? (claims && claims.email) : getRole();
  const linksHtml = linksVisibles().map((l) => `<a href="${l.href}">${l.label}</a>`).join("");
  const salirHtml = `<a href="#" id="link-salir">Salir${quien ? ` (${quien})` : ""}</a>`;
  nav.innerHTML = linksHtml + salirHtml;
  document.getElementById("link-salir").addEventListener("click", (e) => {
    e.preventDefault();
    logout();
  });
}
