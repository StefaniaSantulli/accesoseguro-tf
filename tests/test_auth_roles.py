"""Pruebas locales con JWT RSA reales, SQLite y clientes AWS simulados."""
import importlib.util
import io
import json
import os
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import boto3
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

ROOT = Path(__file__).resolve().parents[1]
PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PUBLIC_JWK = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(PRIVATE_KEY.public_key()))
PUBLIC_JWK['kid'] = 'test-key'
ISSUER = 'https://cognito-idp.us-east-1.amazonaws.com/test-pool'
ENV = {
    'DATABASE_URL': 'sqlite://', 'COGNITO_JWKS': json.dumps({'keys': [PUBLIC_JWK]}),
    'COGNITO_ISSUER': ISSUER, 'COGNITO_CLIENT_ID': 'test-client',
    'ROLES_LAMBDA_NAME': 'test-users', 'USER_POOL_ID': 'test-pool',
    'NOTIFICACIONES_TOPIC_ARN': 'test-mail', 'SNS_TOPIC_ARN': 'test-events',
}


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(os.environ, ENV, clear=True), patch.object(boto3, 'client', return_value=Mock()):
        spec.loader.exec_module(module)
    return module


api = load('api_under_test', 'backend/app.py')
users = load('users_under_test', 'lambdas/gestionar-usuarios/handler.py')
callback = load('callback_under_test', 'lambdas/auth-callback/handler.py')
mail = load('mail_under_test', 'lambdas/notificar-mail/handler.py')


def token(group='transportistas', **overrides):
    claims = {
        'sub': 'user-1', 'username': 'user-1', 'iss': ISSUER,
        'iat': int(time.time()), 'exp': int(time.time()) + 3600,
        'token_use': 'access', 'client_id': 'test-client',
        'cognito:groups': [group] if group else [],
    }
    claims.update(overrides)
    return jwt.encode(claims, PRIVATE_KEY, algorithm='RS256', headers={'kid': 'test-key'})


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.client = api.app.test_client()
        api.app.config['TESTING'] = True
        self.profile = {'ok': True, 'usuario': {'email': 'one@example.invalid', 'empresa_id': None, 'sede_id': '1'}}
        self.current_groups = None
        api.lambda_client = Mock()
        api.s3 = Mock()
        api.sns = Mock()
        api.s3.generate_presigned_url.return_value = 'https://s3.example.invalid/signed'
        def invoke(**kwargs):
            payload = json.loads(kwargs['Payload'])
            if payload['accion'] == 'perfil':
                return {'Payload': io.BytesIO(json.dumps({**self.profile, 'grupos': self.current_groups}).encode())}
            return {'Payload': io.BytesIO(b'{"ok": true, "grupo": "transportistas"}')}
        api.lambda_client.invoke.side_effect = invoke
        with api.app.app_context():
            api.db.drop_all()
            api.db.create_all()
            api.db.session.add_all([api.Sede(id=1, nombre='Uno'), api.Sede(id=2, nombre='Dos')])
            api.db.session.add_all([
                api.Solicitud(id=1, sede_id=1, nombre_solicitante='Propia', dni='111', email_solicitante='one@example.invalid', estado='pendiente'),
                api.Solicitud(id=2, sede_id=2, nombre_solicitante='Otra sede', dni='222', email_solicitante='two@example.invalid', estado='aprobada'),
                api.Solicitud(id=3, sede_id=1, nombre_solicitante='Aprobada', dni='333', email_solicitante='two@example.invalid', estado='aprobada'),
            ])
            api.db.session.commit()

    def headers(self, group='transportistas', **claims):
        if self.current_groups is None:
            self.current_groups = [group] if group else []
        return {'Authorization': 'Bearer ' + token(group, **claims)}

    def test_access_token_accepted_and_id_token_rejected(self):
        self.assertEqual(self.client.get('/api/me', headers=self.headers()).status_code, 200)
        self.assertEqual(self.client.get('/api/me', headers=self.headers(token_use='id', aud='test-client')).status_code, 401)

    def test_invalid_token_claims_rejected(self):
        for claims in ({'client_id': 'other'}, {'iss': 'https://other.invalid'}, {'exp': 1}):
            with self.subTest(claims=claims):
                self.assertEqual(self.client.get('/api/me', headers=self.headers(**claims)).status_code, 401)
        bad = token()[:-12] + 'AAAAAAAAAAAA'
        self.assertEqual(self.client.get('/api/me', headers={'Authorization': 'Bearer ' + bad}).status_code, 401)
        self.assertEqual(self.client.get('/api/me').status_code, 401)

    def test_missing_required_claim_rejected(self):
        claims = jwt.decode(token(), options={'verify_signature': False})
        del claims['exp']
        missing = jwt.encode(claims, PRIVATE_KEY, algorithm='RS256', headers={'kid': 'test-key'})
        self.assertEqual(self.client.get('/api/me', headers={'Authorization': 'Bearer ' + missing}).status_code, 401)

    def test_no_self_elevation_even_with_valid_token(self):
        headers = self.headers(None)
        for group in ['administradores', 'porteros', 'responsables_sede']:
            self.assertEqual(self.client.post('/api/mi-rol', json={'grupo': group}, headers=headers).status_code, 400)
        self.assertEqual(self.client.post('/api/mi-rol', json={'grupo': 'transportistas'}, headers=headers).status_code, 200)

    def test_no_role_cannot_create_requests(self):
        self.assertEqual(self.client.post('/api/solicitudes', json={}, headers=self.headers(None)).status_code, 403)

    def test_transportista_only_sees_and_uploads_own_requests(self):
        headers = self.headers()
        data = self.client.get('/api/mis-solicitudes', headers=headers).get_json()
        self.assertEqual([s['id'] for s in data], [1])
        self.assertEqual(self.client.post('/api/solicitudes/2/documento', json={}, headers=headers).status_code, 403)
        for path in ['/api/solicitudes/pendientes', '/api/verificacion?busqueda=333', '/api/reporte/exportar', '/api/auditoria']:
            self.assertEqual(self.client.get(path, headers=headers).status_code, 403)

    def test_portero_cannot_approve_or_export(self):
        headers = self.headers('porteros')
        self.assertEqual(self.client.post('/api/solicitudes/1/resolver', json={'accion': 'aprobada'}, headers=headers).status_code, 403)
        self.assertEqual(self.client.get('/api/reporte/exportar', headers=headers).status_code, 403)
        self.assertEqual(self.client.get('/api/auditoria', headers=headers).status_code, 403)
        self.assertEqual(self.client.post('/api/solicitudes', json={}, headers=headers).status_code, 403)

    def test_portero_only_operates_own_site(self):
        headers = self.headers('porteros')
        self.assertFalse(self.client.get('/api/verificacion?busqueda=222', headers=headers).get_json()['autorizado'])
        self.assertTrue(self.client.get('/api/verificacion?busqueda=333', headers=headers).get_json()['autorizado'])
        self.assertEqual(self.client.post('/api/solicitudes/2/ingreso', headers=headers).status_code, 404)
        self.assertEqual(self.client.post('/api/solicitudes/1/ingreso', headers=headers).status_code, 409)
        self.assertEqual(self.client.post('/api/solicitudes/3/ingreso', headers=headers).status_code, 200)
        self.assertEqual(self.client.post('/api/solicitudes/3/egreso', headers=headers).status_code, 200)

    def test_responsable_can_approve_but_not_act_as_portero(self):
        headers = self.headers('responsables_sede')
        self.assertEqual(self.client.post('/api/solicitudes/1/resolver', json={'accion': 'aprobada'}, headers=headers).status_code, 200)
        self.assertEqual(self.client.post('/api/solicitudes/2/resolver', json={'accion': 'rechazada'}, headers=headers).status_code, 404)
        self.assertEqual(self.client.post('/api/solicitudes/3/ingreso', headers=headers).status_code, 403)
        logs = self.client.get('/api/auditoria', headers=headers).get_json()
        self.assertEqual(len(logs), 1)

    def test_responsable_scope_applies_to_lists_reports_and_history(self):
        headers = self.headers('responsables_sede')
        self.assertEqual([s['id'] for s in self.client.get('/api/sedes', headers=headers).get_json()], [1])
        self.assertEqual({s['id'] for s in self.client.get('/api/solicitudes', headers=headers).get_json()}, {1, 3})
        report = self.client.get('/api/reporte/exportar', headers=headers)
        self.assertEqual(report.status_code, 200)
        self.assertNotIn('Otra sede', report.get_data(as_text=True))
        self.assertEqual(self.client.get('/api/reporte/exportar?sede_id=2', headers=headers).status_code, 403)
        with api.app.app_context():
            log = api.LogAuditoria(accion='Privado', detalle='otra sede')
            old = api.LogAuditoria(accion='Legado', detalle='sin sede comprobable')
            api.db.session.add_all([log, old])
            api.db.session.flush()
            api.db.session.add(api.AuditoriaSede(log_id=log.id, sede_id=2))
            api.db.session.commit()
        self.assertEqual(self.client.get('/api/auditoria', headers=headers).get_json(), [])

    def test_invalid_or_missing_site_denied(self):
        for sede in (None, 'abc', '999'):
            self.profile['usuario']['sede_id'] = sede
            self.assertEqual(self.client.get('/api/solicitudes', headers=self.headers('porteros')).status_code, 403)

    def test_revoked_group_and_legacy_group_denied(self):
        self.current_groups = []
        self.assertEqual(self.client.get('/api/solicitudes', headers=self.headers('porteros')).status_code, 403)
        self.current_groups = ['administradores']
        self.assertEqual(self.client.get('/api/me', headers=self.headers('administradores')).status_code, 403)

    def test_multiple_application_roles_denied(self):
        self.current_groups = ['porteros', 'responsables_sede']
        headers = self.headers('porteros', **{'cognito:groups': self.current_groups})
        self.assertEqual(self.client.get('/api/me', headers=headers).status_code, 403)

    def test_expiration_only_changes_own_site(self):
        from datetime import datetime, timedelta
        with api.app.app_context():
            for id in (2, 3):
                api.db.session.get(api.Solicitud, id).fecha_aprobacion = datetime.now() - timedelta(days=1)
            api.db.session.commit()
        result = self.client.get('/api/expirar-vencidas', headers=self.headers('responsables_sede'))
        self.assertEqual(result.get_json()['ids'], [3])
        with api.app.app_context():
            self.assertEqual(api.db.session.get(api.Solicitud, 2).estado, 'aprobada')

    def test_demo_requires_explicit_configuration_and_checks_roles(self):
        with patch.object(api, 'AUTH_ENABLED', False), patch.object(api, 'LOCAL_DEMO', True):
            headers = {'X-Demo-Role': 'porteros', 'X-Demo-Sede': '2'}
            self.assertEqual([s['id'] for s in self.client.get('/api/solicitudes', headers=headers).get_json()], [2])
            self.assertEqual(self.client.get('/api/reporte/exportar', headers=headers).status_code, 403)

    def test_profile_failure_fails_closed(self):
        self.profile = {'ok': False}
        self.assertEqual(self.client.get('/api/me', headers=self.headers()).status_code, 403)
        api.lambda_client.invoke.side_effect = RuntimeError('simulado')
        with self.assertLogs(api.app.logger, level='ERROR'):
            self.assertEqual(self.client.get('/api/me', headers=self.headers()).status_code, 503)


class LambdaTests(unittest.TestCase):
    def setUp(self):
        users.cognito = Mock()
        users.sns = Mock()
        users.cognito.admin_get_user.return_value = {'Enabled': True, 'UserAttributes': [
            {'Name': 'sub', 'Value': 'user-1'}, {'Name': 'email', 'Value': 'verified@example.invalid'},
            {'Name': 'email_verified', 'Value': 'true'}, {'Name': 'custom:sede_id', 'Value': '1'},
        ]}
        users.cognito.admin_list_groups_for_user.return_value = {'Groups': []}
        self.event = {'accion': 'alta_transportista', 'username': 'user-1', 'sub': 'user-1', 'grupo': 'transportistas'}

    def test_lambda_rejects_privileged_groups_directly(self):
        for group in ('porteros', 'responsables_sede', 'administradores'):
            self.assertFalse(users.lambda_handler({**self.event, 'grupo': group}, None)['ok'])
        users.cognito.admin_add_user_to_group.assert_not_called()

    def test_signup_uses_verified_email_not_client_value(self):
        result = users.lambda_handler({**self.event, 'email': 'attacker@example.invalid'}, None)
        self.assertTrue(result['ok'])
        self.assertEqual(users.sns.subscribe.call_args.kwargs['Endpoint'], 'verified@example.invalid')

    def test_profile_wrong_identity_or_unverified_email_denied(self):
        self.assertFalse(users.lambda_handler({**self.event, 'accion': 'perfil', 'sub': 'another'}, None)['ok'])
        users.cognito.admin_get_user.return_value['UserAttributes'][2]['Value'] = 'false'
        self.assertFalse(users.lambda_handler(self.event, None)['ok'])

    def test_existing_staff_cannot_self_change_group(self):
        users.cognito.admin_list_groups_for_user.return_value = {'Groups': [{'GroupName': 'porteros'}]}
        self.assertFalse(users.lambda_handler(self.event, None)['ok'])
        users.cognito.admin_add_user_to_group.assert_not_called()

    def test_callback_keeps_class_workaround_and_state(self):
        env = {'FRONTEND_URL': 'http://site.example.invalid', 'COGNITO_CLIENT_ID': 'test-client',
               'COGNITO_DOMAIN': 'https://login.example.invalid', 'REDIRECT_URI': 'https://callback.example.invalid'}
        state = 'a' * 64
        with patch.dict(os.environ, env), patch.object(callback, '_get_client_secret', return_value='fake'), \
             patch.object(callback.urllib.request, 'urlopen', return_value=io.BytesIO(b'{"id_token":"id","access_token":"access"}')):
            result = callback.lambda_handler({'queryStringParameters': {'code': 'code', 'state': state}}, None)
            self.assertEqual(result['statusCode'], 302)
            url = result['headers']['Location']
            self.assertTrue(url.startswith('http://site.example.invalid/#'))
            self.assertIn('access_token=access', url)
            self.assertIn('state=' + state, url)
            invalid = callback.lambda_handler({'queryStringParameters': {'code': 'code'}}, None)
            self.assertIn('error=invalid_state', invalid['headers']['Location'])

    def test_new_request_notification_is_scoped_by_site(self):
        mail.sns = Mock()
        event = {'Records': [{'messageId': 'm1', 'body': json.dumps({'tipo': 'solicitud_creada', 'solicitud_id': 1, 'sede_id': 2})}]}
        self.assertEqual(mail.lambda_handler(event, None), {'batchItemFailures': []})
        self.assertEqual(mail.sns.publish.call_args.kwargs['MessageAttributes']['destinatario']['StringValue'], 'responsable-sede-2')
        event['Records'][0]['body'] = json.dumps({'tipo': 'solicitud_creada'})
        self.assertEqual(mail.lambda_handler(event, None), {'batchItemFailures': [{'itemIdentifier': 'm1'}]})


class ConfigurationTests(unittest.TestCase):
    def test_missing_cognito_does_not_silently_enable_demo(self):
        with patch.dict(ENV, {'COGNITO_JWKS': ''}):
            with self.assertRaisesRegex(RuntimeError, 'Falta COGNITO_JWKS'):
                load('missing_identity', 'backend/app.py')

    def test_demo_cannot_be_enabled_in_aws_configuration(self):
        with patch.dict(ENV, {'LOCAL_DEMO': 'true', 'APP_ENV': 'dev'}):
            with self.assertRaisesRegex(RuntimeError, 'LOCAL_DEMO'):
                load('unsafe_demo', 'backend/app.py')

    def test_operator_script_replaces_only_application_groups(self):
        script = load('role_script', 'scripts/asignar-rol.py')
        calls = []
        def aws(region, *args):
            calls.append(args)
            if args[1] == 'admin-list-groups-for-user':
                return {'Groups': [{'GroupName': 'administradores'}, {'GroupName': 'transportistas'}, {'GroupName': 'external-idp'}]}
            return {}
        args = ['asignar-rol.py', '--pool', 'test-pool', '--usuario', 'one@example.invalid', '--grupo', 'porteros', '--sede', '2']
        with patch('sys.argv', args), patch.object(script, 'aws', side_effect=aws), patch('sys.stdout', io.StringIO()):
            script.main()
        removed = [call[-1] for call in calls if call[1] == 'admin-remove-user-from-group']
        self.assertEqual(removed, ['administradores', 'transportistas'])
        self.assertEqual(calls[-1][-1], 'porteros')
        attrs = next(call[-1] for call in calls if call[1] == 'admin-update-user-attributes')
        self.assertEqual(json.loads(attrs), [{'Name': 'custom:sede_id', 'Value': '2'}])


if __name__ == '__main__':
    unittest.main()
