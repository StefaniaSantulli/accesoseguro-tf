"""Pruebas de bloqueo reales. TEST_POSTGRES_URL apunta SOLO a una base de pruebas."""
import json
import os
import sys
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from unittest.mock import Mock

from sqlalchemy import create_engine, select
from sqlalchemy.exc import OperationalError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
import notifications as n


@unittest.skipUnless(os.environ.get('TEST_POSTGRES_URL'), 'Requiere PostgreSQL de pruebas; configurado en CI')
class PostgresConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.schema = 'test_notif_' + uuid.uuid4().hex
        self.admin = create_engine(os.environ['TEST_POSTGRES_URL'])
        with self.admin.begin() as conn:
            conn.exec_driver_sql(f'CREATE SCHEMA {self.schema}')
        self.engine = create_engine(os.environ['TEST_POSTGRES_URL'],
                                    connect_args={'options': f'-c search_path={self.schema}'})
        n.metadata.create_all(self.engine)
        with self.engine.begin() as conn:
            self.event_id = n.enqueue(conn, solicitud_id=1, tipo='solicitud_creada', sede_id=1, sede='Uno', estado='pendiente')
        self.sns = Mock()
        self.sns.publish.return_value = {'MessageId': 'sns-1'}
        self.sns.get_paginator.return_value.paginate.return_value = [{'Subscriptions': [
            {'Protocol': 'email', 'SubscriptionArn': 'arn:test'}]}]
        self.sns.get_subscription_attributes.return_value = {'Attributes': {
            'FilterPolicy': '{"destinatario_id":["responsable-sede-1"]}', 'PendingConfirmation': 'false'}}

    def tearDown(self):
        self.engine.dispose()
        with self.admin.begin() as conn:
            conn.exec_driver_sql(f'DROP SCHEMA {self.schema} CASCADE')
        self.admin.dispose()

    def publish(self):
        with self.engine.begin() as conn:
            return n.publish_one(conn, self.sns, 'events')

    def test_two_publishers_do_not_take_the_same_pending_event(self):
        entered, release = Event(), Event()
        def delayed_publish(**kwargs):
            entered.set()
            if not release.wait(5):
                raise TimeoutError('test timeout')
            return {'MessageId': 'sns-1'}
        self.sns.publish.side_effect = delayed_publish
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(self.publish)
            try:
                self.assertTrue(entered.wait(5))
                self.assertIsNone(self.publish())  # SKIP LOCKED
            finally:
                release.set()
            self.assertTrue(future.result(timeout=5))
        self.assertEqual(self.sns.publish.call_count, 1)

    def test_concurrent_email_delivery_retries_then_skips_completed_event(self):
        self.publish()
        with self.engine.connect() as conn:
            payload = json.loads(conn.execute(select(n.outbox.c.contenido)).scalar_one())
        entered, release = Event(), Event()
        def delayed_publish(**kwargs):
            entered.set()
            if not release.wait(5):
                raise TimeoutError('test timeout')
            return {'MessageId': 'mail-1'}
        self.sns.publish.reset_mock()
        self.sns.publish.side_effect = delayed_publish
        def send():
            with self.engine.begin() as conn:
                return n.send_email(conn, self.sns, 'mail', 'http://site.invalid', payload)
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(send)
            try:
                self.assertTrue(entered.wait(5))
                with self.assertRaises(OperationalError):
                    send()  # NOWAIT: SQS reintentará esta entrega concurrente
            finally:
                release.set()
            self.assertEqual(future.result(timeout=5), 'publicado')
        self.assertEqual(send(), 'duplicado')
        self.assertEqual(self.sns.publish.call_count, 1)

    def test_commit_failure_keeps_same_event_for_retry(self):
        with self.assertRaises(RuntimeError), self.engine.begin() as conn:
            self.assertTrue(n.publish_one(conn, self.sns, 'events'))
            raise RuntimeError('simulated crash after SNS acceptance')
        self.assertTrue(self.publish())
        ids = [json.loads(call.kwargs['Message'])['evento_id'] for call in self.sns.publish.call_args_list]
        self.assertEqual(ids, [self.event_id, self.event_id])
