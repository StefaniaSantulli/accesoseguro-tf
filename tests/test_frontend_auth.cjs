// Ejecutar: node tests/test_frontend_auth.cjs (sin dependencias externas).
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const { webcrypto } = require('node:crypto');
const root = path.resolve(__dirname, '..');
const auth = fs.readFileSync(path.join(root, 'frontend/assets/auth.js'), 'utf8');
const api = fs.readFileSync(path.join(root, 'frontend/assets/api.js'), 'utf8');
const storage = () => {
  const values = new Map();
  return { getItem: (k) => values.get(k) || null, setItem: (k, v) => values.set(k, String(v)), removeItem: (k) => values.delete(k) };
};
const jwt = (use, groups = ['porteros'], extra = {}) => 'header.' + Buffer.from(JSON.stringify({
  sub: 'user-1', token_use: use, exp: Math.floor(Date.now() / 1000) + 3600, 'cognito:groups': groups, ...extra,
})).toString('base64url') + '.signature';
function browser(hash = '', state = 'a'.repeat(64), config = true) {
  const context = {
    URL, URLSearchParams, Uint8Array, crypto: webcrypto, Date, console,
    atob: (s) => Buffer.from(s, 'base64').toString('binary'),
    sessionStorage: storage(), localStorage: storage(),
    location: { hash, pathname: '/', search: '', href: '' },
    history: { replaceState() {} }, alert: () => {}, document: {},
    window: { APP_CONFIG: config ? { loginUrl: 'https://login.example.invalid/login?client_id=test', apiUrl: 'http://alb.example.invalid' } : null },
  };
  context.window.location = context.location;
  if (state) context.sessionStorage.setItem('accesoseguro_oauth_state', state);
  vm.createContext(context);
  vm.runInContext(auth, context);
  vm.runInContext(api, context);
  return context;
}
function run(context, code) { return vm.runInContext(code, context); }
const access = jwt('access');
const id = jwt('id', ['porteros'], { email: 'person@example.invalid' });
const hash = '#access_token=' + access + '&id_token=' + id + '&state=' + 'a'.repeat(64);
let context = browser(hash);
assert.equal(run(context, 'getToken()'), access);
assert.equal(run(context, 'authHeaders().Authorization'), 'Bearer ' + access);
assert.equal(run(context, 'getRole()'), 'porteros');
assert.equal(run(context, 'linksVisibles().some(l => l.href === "panel-aprobacion.html")'), false);
assert.equal(run(context, 'linksVisibles().some(l => l.href === "verificacion.html")'), true);
assert.equal(context.sessionStorage.getItem('accesoseguro_oauth_state'), null);
run(context, 'requireRole([ROLES.RESPONSABLE])');
assert.equal(context.window.location.href, 'index.html');
context = browser(hash, 'b'.repeat(64));
assert.equal(run(context, 'getToken()'), null);
assert.ok(context.sessionStorage.getItem('accesoseguro_login_error'));
context = browser(hash, null);
assert.equal(run(context, 'getToken()'), null);
context = browser('#id_token=' + id);
assert.equal(run(context, 'getToken()'), null);
context = browser();
context.sessionStorage.setItem('accesoseguro_access_token', jwt('access', ['administradores']));
assert.equal(run(context, 'getRole()'), 'invalido');
context.sessionStorage.setItem('accesoseguro_access_token', jwt('access', ['porteros', 'responsables_sede']));
assert.equal(run(context, 'getRole()'), 'invalido');
context.sessionStorage.setItem('accesoseguro_access_token', jwt('access', ['porteros'], { exp: 1 }));
assert.equal(run(context, 'getRole()'), null);
run(context, 'login()');
assert.equal(new URL(context.window.location.href).searchParams.get('state'), context.sessionStorage.getItem('accesoseguro_oauth_state'));
assert.match(context.sessionStorage.getItem('accesoseguro_oauth_state'), /^[a-f0-9]{64}$/);
context = browser('', null, false);
run(context, 'setRole(ROLES.RESPONSABLE)');
assert.equal(run(context, 'getRole()'), 'responsables_sede');
assert.equal(run(context, 'authHeaders()["X-Demo-Role"]'), 'responsables_sede');
assert.equal(run(context, 'linksVisibles().some(l => l.href === "verificacion.html")'), false);
for (const [page, role] of [['panel-aprobacion', 'RESPONSABLE'], ['reporte', 'RESPONSABLE'], ['auditoria', 'RESPONSABLE'], ['verificacion', 'PORTERO'], ['mis-solicitudes', 'TRANSPORTISTA']]) {
  assert.ok(fs.readFileSync(path.join(root, `frontend/${page}.html`), 'utf8').includes(`requireRole([ROLES.${role}])`));
}
console.log('Frontend OK: access token, state, roles, navegación y modo demo.');
module.exports = { passed: true, checks: 'tokens, state, roles, navegación y modo demo' };
