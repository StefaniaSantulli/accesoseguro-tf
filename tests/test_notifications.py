"""Flujo transaccional y fallos de SNS sin enviar correos ni usar credenciales."""
import json
import unittest
from datetime import datetime, timedelta
from unittest.mock import Mock, patch

from sqlalchemy import select
from test_auth_roles import api, users, load
import test_auth_roles as auth_tests
import notifications as n
import notification_jobs as jobs


class NotificationsTests(unittest.TestCase):
    def setUp(self):
        self.context = api.app.app_context()
        self.context.push()
        api.db.drop_all()
        api.db.create_all()
        api.db.session.add(api.Sede(id=1, nombre='Uno'))
        api.db.session.commit()
        self.engine = api.db.engine
        self.sns = Mock()
        self.sns.publish.return_value = {'MessageId': 'sns-1'}
        self.sns.get_paginator.return_value.paginate.return_value = [{'Subscriptions': [
            {'SubscriptionArn': 'arn:subscription', 'Protocol': 'email', 'Endpoint': 'one@example.invalid'}]}]
        self.sns.get_subscription_attributes.return_value = {'Attributes': {
            'PendingConfirmation': 'false', 'FilterPolicyScope': 'MessageAttributes',
            'FilterPolicy': json.dumps({'destinatario_id': ['usuario:user-1', 'responsable-sede-1']})}}
        self.now = datetime.utcnow()

    def tearDown(self):
        api.db.session.remove()
        self.context.pop()

    def request(self, id=1, state='aprobada', ingress=None, approved=None):
        api.db.session.add(api.Solicitud(id=id, sede_id=1, nombre_solicitante='Test', dni='123',
            estado=state, fecha_aprobacion=approved or self.now - timedelta(days=2),
            hora_ingreso=ingress, email_solicitante='one@example.invalid'))
        api.db.session.execute(n.owners.insert().values(solicitud_id=id, usuario_sub='user-1'))
        api.db.session.commit()

    def enqueue(self, tipo='solicitud_aprobada'):
        with self.engine.begin() as conn:
            return n.enqueue(conn, solicitud_id=1, tipo=tipo, sede_id=1, sede='Uno', estado='aprobada', now=self.now)

    def row(self, event_id):
        with self.engine.connect() as conn:
            return dict(conn.execute(select(n.outbox).where(n.outbox.c.evento_id == event_id)).mappings().one())

    def publish(self, event_id):
        with self.engine.begin() as conn:
            return n.publish_one(conn, self.sns, 'events', event_id=event_id, now=self.now)

    def send(self, payload):
        with self.engine.begin() as conn:
            return n.send_email(conn, self.sns, 'mail', 'http://site.invalid', payload)

    def test_no_pending_event_survives_transaction_rollback(self):
        with self.assertRaises(RuntimeError), self.engine.begin() as conn:
            n.enqueue(conn, solicitud_id=1, tipo='solicitud_creada', sede_id=1, sede='Uno', estado='pendiente')
            raise RuntimeError('database failure')
        with self.engine.connect() as conn:
            self.assertEqual(conn.execute(select(n.outbox)).all(), [])
        self.sns.publish.assert_not_called()

    def test_personal_notification_routes_by_sub_and_deduplicates(self):
        self.request()
        event_id = self.enqueue()
        self.assertTrue(self.publish(event_id))
        payload = json.loads(self.row(event_id)['contenido'])
        self.assertEqual(payload['destinatario_id'], 'usuario:user-1')
        self.assertNotIn('email', json.dumps(payload))
        self.assertEqual(self.send(payload), 'publicado')
        self.assertEqual(self.send(payload), 'duplicado')
        self.assertIsNone(self.publish(event_id))
        self.assertEqual(self.sns.publish.call_count, 2)  # SNS eventos y SNS correo
        self.assertIsNotNone(self.row(event_id)['mail_publicado'])
        message = self.sns.publish.call_args.kwargs
        self.assertEqual(message['MessageAttributes']['destinatario_id']['StringValue'], 'usuario:user-1')
        self.assertNotIn('dni', message['Message'].lower())

    def test_sns_outage_retains_event_and_retry_preserves_id(self):
        self.request()
        event_id = self.enqueue()
        self.sns.publish.side_effect = TimeoutError('network')
        self.assertFalse(self.publish(event_id))
        row = self.row(event_id)
        self.assertIsNone(row['publicado'])
        self.assertEqual(row['intentos'], 1)
        self.sns.publish.side_effect = None
        with self.engine.begin() as conn:
            self.assertTrue(n.publish_one(conn, self.sns, 'events', now=self.now + timedelta(minutes=2)))
        self.assertEqual(json.loads(self.sns.publish.call_args.kwargs['Message'])['evento_id'], event_id)

    def test_event_without_owner_stays_pending_without_email_fallback(self):
        event_id = self.enqueue()
        self.assertFalse(self.publish(event_id))
        self.assertIsNone(self.row(event_id)['publicado'])
        self.sns.publish.assert_not_called()

    def test_mail_failure_is_retryable_not_marked_success(self):
        self.request()
        event_id = self.enqueue()
        self.publish(event_id)
        payload = json.loads(self.row(event_id)['contenido'])
        self.sns.publish.side_effect = TimeoutError('network')
        with self.assertRaises(TimeoutError):
            self.send(payload)
        self.assertIsNone(self.row(event_id)['mail_publicado'])
        self.sns.publish.side_effect = None
        self.assertEqual(self.send(payload), 'publicado')

    def test_site_notification_and_unconfirmed_subscription(self):
        event_id = self.enqueue('solicitud_creada')
        self.publish(event_id)
        payload = json.loads(self.row(event_id)['contenido'])
        self.assertEqual(payload['destinatario_id'], 'responsable-sede-1')
        self.sns.get_subscription_attributes.return_value['Attributes']['PendingConfirmation'] = 'true'
        with self.assertRaisesRegex(ValueError, 'confirmada'):
            self.send(payload)
        self.assertIsNone(self.row(event_id)['mail_publicado'])

    def test_unfiltered_subscription_blocks_broadcast(self):
        self.request()
        event_id = self.enqueue()
        self.publish(event_id)
        self.sns.get_subscription_attributes.return_value['Attributes']['FilterPolicy'] = '{}'
        with self.assertRaisesRegex(ValueError, 'inseguro'):
            self.send(json.loads(self.row(event_id)['contenido']))
        self.assertEqual(self.sns.publish.call_count, 1)

    def test_periodic_worker_processes_real_pending_rows(self):
        handler = load('periodic_test', 'lambdas/vencer-permisos/handler.py')
        self.request()
        self.enqueue('solicitud_creada')
        with patch.object(handler.notification_jobs, 'database_engine', return_value=self.engine), \
             patch.object(self.engine, 'dispose'), patch.object(handler, 'sns', self.sns), \
             patch.dict(handler.os.environ, {'SEGUNDOS_VENCIMIENTO': '86400'}):
            result = handler.lambda_handler({}, None)
        self.assertEqual(result, {'vencidas': 1, 'publicados': 2, 'fallidos': 0, 'pendientes': 0})

    def test_event_cannot_change_recipient_and_missing_config_fails(self):
        self.request()
        event_id = self.enqueue()
        self.publish(event_id)
        payload = json.loads(self.row(event_id)['contenido'])
        with self.assertRaises(ValueError):
            self.send({**payload, 'destinatario_id': 'usuario:attacker'})
        with self.assertRaises(ValueError), self.engine.begin() as conn:
            n.send_email(conn, self.sns, '', 'http://site.invalid', payload)

    def test_expiration_real_sql_is_atomic_and_idempotent(self):
        self.request(1)
        self.request(2, ingress=self.now)
        self.request(3, approved=self.now)
        with self.engine.begin() as conn:
            self.assertEqual(jobs.expire(conn, 86400, now=self.now), 1)
        with self.engine.begin() as conn:
            self.assertEqual(jobs.expire(conn, 86400, now=self.now), 0)
        api.db.session.expire_all()
        self.assertEqual(api.db.session.get(api.Solicitud, 1).estado, 'vencida')
        self.assertEqual(api.db.session.get(api.Solicitud, 2).estado, 'aprobada')
        self.assertEqual(api.AuditoriaSede.query.count(), 1)
        self.assertEqual(len(api.db.session.execute(select(n.outbox)).all()), 1)

    def test_expiration_rolls_back_if_event_insert_fails(self):
        self.request()
        with patch.object(n, 'enqueue', side_effect=RuntimeError('database failure')):
            with self.assertRaises(RuntimeError), self.engine.begin() as conn:
                jobs.expire(conn, 86400, now=self.now)
        api.db.session.expire_all()
        self.assertEqual(api.db.session.get(api.Solicitud, 1).estado, 'aprobada')
        self.assertEqual(api.LogAuditoria.query.count(), 0)

    def test_partial_batch_retries_only_failed_record(self):
        handler = load('notification_batch_test', 'lambdas/notificar-mail/handler.py')
        self.request()
        event_id = self.enqueue()
        self.publish(event_id)
        body = self.row(event_id)['contenido']
        records = [{'messageId': 'good', 'body': body}, {'messageId': 'bad', 'body': '{invalid json'}]
        with patch.object(handler.notification_jobs, 'database_engine', return_value=self.engine), \
             patch.object(self.engine, 'dispose'), patch.object(handler, 'sns', self.sns), \
             patch.object(handler, 'SITE_URL', 'http://site.invalid'):
            result = handler.lambda_handler({'Records': records}, None)
        self.assertEqual(result, {'batchItemFailures': [{'itemIdentifier': 'bad'}]})


class ApiNotificationTests(unittest.TestCase):
    setUp = auth_tests.ApiTests.setUp
    headers = auth_tests.ApiTests.headers
    def test_create_commits_outbox_when_sns_is_down(self):
        api.sns.publish.side_effect = TimeoutError('network')
        response = self.client.post('/api/solicitudes', headers=self.headers(), json={
            'nombre_solicitante': 'Test', 'dni': '123', 'sede_id': 1})
        self.assertEqual(response.status_code, 201)
        with api.app.app_context():
            row = api.db.session.execute(select(n.outbox)).mappings().one()
            self.assertIsNone(row['publicado'])
            owner = select(n.owners.c.usuario_sub).where(n.owners.c.solicitud_id == response.get_json()['id'])
            self.assertEqual(api.db.session.execute(owner).scalar_one(), 'user-1')

    def test_resolve_twice_does_not_create_two_events(self):
        headers = self.headers('responsables_sede')
        self.assertEqual(self.client.post('/api/solicitudes/1/resolver', headers=headers, json={'accion': 'aprobada'}).status_code, 200)
        self.assertEqual(self.client.post('/api/solicitudes/1/resolver', headers=headers, json={'accion': 'aprobada'}).status_code, 409)
        with api.app.app_context():
            self.assertEqual(len(api.db.session.execute(select(n.outbox)).all()), 1)

    def test_subscription_endpoint_never_trusts_client_identity(self):
        result = self.client.post('/api/notificaciones/suscripcion', headers=self.headers(),
                                  json={'sub': 'attacker', 'email': 'attacker@example.invalid'})
        self.assertEqual(result.status_code, 200)
        payload = json.loads(api.lambda_client.invoke.call_args.kwargs['Payload'])
        self.assertEqual(payload, {'accion': 'sincronizar_notificaciones', 'username': 'user-1', 'sub': 'user-1'})


class SubscriptionTests(unittest.TestCase):
    def setUp(self):
        self.sns = Mock()
        self.sns.get_paginator.return_value.paginate.return_value = [{'Subscriptions': []}]
        self.profile = {'email': 'one@example.invalid', 'sede_id': '2'}
        self.patch = patch.object(users, 'sns', self.sns)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_new_subscription_is_pending_and_stable(self):
        result = users.sincronizar_notificaciones('user-1', self.profile, ['transportistas'])
        self.assertEqual(result['estado'], 'pendiente')
        policy = json.loads(self.sns.subscribe.call_args.kwargs['Attributes']['FilterPolicy'])
        self.assertEqual(policy, {'destinatario_id': ['usuario:user-1']})

    def test_confirmed_subscription_changes_site_in_place(self):
        self.sns.get_paginator.return_value.paginate.return_value = [{'Subscriptions': [
            {'Protocol': 'email', 'Endpoint': self.profile['email'], 'SubscriptionArn': 'arn:existing'}]}]
        self.sns.get_subscription_attributes.return_value = {'Attributes': {'PendingConfirmation': 'false',
            'FilterPolicy': '{"destinatario_id":["usuario:user-1", "responsable-sede-1"]}'}}
        result = users.sincronizar_notificaciones('user-1', self.profile, ['responsables_sede'])
        self.assertEqual(result['estado'], 'confirmada')
        policy = json.loads(self.sns.set_subscription_attributes.call_args_list[0].kwargs['AttributeValue'])
        self.assertEqual(policy, {'destinatario_id': ['usuario:user-1', 'responsable-sede-2']})
        self.sns.subscribe.assert_not_called()

    def test_no_role_or_missing_site_cannot_subscribe_as_staff(self):
        for groups, site in [([], '1'), (['porteros', 'responsables_sede'], '1'), (['responsables_sede'], None)]:
            with self.assertRaises(ValueError):
                users.sincronizar_notificaciones('user-1', {**self.profile, 'sede_id': site}, groups)
        self.sns.subscribe.assert_not_called()

    def test_failed_subscription_does_not_undo_role_and_is_retryable(self):
        event = {'accion': 'alta_transportista', 'username': 'user-1', 'sub': 'user-1', 'grupo': 'transportistas'}
        cognito = Mock()
        cognito.admin_list_groups_for_user.return_value = {'Groups': []}
        self.sns.subscribe.side_effect = RuntimeError('SNS down')
        with patch.object(users, '_perfil', return_value=self.profile), patch.object(users, 'cognito', cognito):
            result = users.lambda_handler(event, None)
        self.assertTrue(result['ok'])
        self.assertEqual(result['notificaciones']['estado'], 'error')
        cognito.admin_add_user_to_group.assert_called_once()
