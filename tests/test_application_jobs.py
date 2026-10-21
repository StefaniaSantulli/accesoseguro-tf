"""SQL real con SQLite; clientes AWS aislados. PostgreSQL se prueba además en CI."""
import io
import json
import os
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from unittest.mock import Mock, patch

from sqlalchemy import create_engine, select, text
import test_auth_roles as auth_tests
from test_auth_roles import ENV, api, load
import application_jobs as jobs
import database_schema as schema

with patch.dict(ENV, {'DOCS_BUCKET': 'documents', 'AUDIT_BUCKET': 'reports'}):
    document_lambda = load('document_lambda', 'lambdas/validar-documento/handler.py')
    report_lambda = load('report_lambda', 'lambdas/generar-reporte/handler.py')


class JobsTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine('sqlite://')
        schema.upgrade(self.engine, api.db.metadata)
        requests = api.db.metadata.tables['solicitud']
        with self.engine.begin() as conn:
            conn.execute(requests.insert(), [
                dict(id=1, sede_id=1, nombre_solicitante='=1+1', dni='111', estado='pendiente',
                     fecha_creacion=datetime(2026, 10, 20, 23, 59), documento_s3_key='empresas/a/1.pdf'),
                dict(id=2, sede_id=2, nombre_solicitante='Otra sede', dni='222', estado='pendiente',
                     fecha_creacion=datetime(2026, 10, 20), documento_s3_key=None),
                dict(id=3, sede_id=1, nombre_solicitante='Otro día', dni='333', estado='pendiente',
                     fecha_creacion=datetime(2026, 10, 21), documento_s3_key=None),
            ])
            conn.execute(jobs.documents.insert().values(solicitud_id=1, s3_key='empresas/a/1.pdf',
                                                        estado='pendiente', actualizado=datetime.now()))
        document_lambda.s3 = Mock()
        document_lambda.s3.head_object.return_value = {'ContentLength': 20, 'ContentType': 'application/pdf'}
        document_lambda.s3.get_object.side_effect = lambda **kwargs: {'Body': io.BytesIO(b'%PDF-1.7')}
        report_lambda.s3 = Mock()
        report_lambda.s3.generate_presigned_url.return_value = 'https://reports.invalid/signed'

    def tearDown(self):
        self.engine.dispose()

    def document(self):
        with self.engine.connect() as conn:
            return conn.execute(select(jobs.documents)).mappings().one()

    def record(self, **overrides):
        obj = {'key': 'empresas/a/1.pdf', 'versionId': 'version-1', 'sequencer': '00A', **overrides}
        return {'s3': {'bucket': {'name': 'documents'}, 'object': obj}}

    def event(self, record=None):
        return {'Records': [{'messageId': 'one', 'body': json.dumps({'Records': [record or self.record()]})}]}

    def test_migration_repeat_preserves_rows_and_seeds(self):
        schema.upgrade(self.engine, api.db.metadata)
        with self.engine.connect() as conn:
            schema.check_ready(conn)
            self.assertEqual(conn.execute(text('SELECT COUNT(*) FROM sede')).scalar(), 3)
            self.assertEqual(conn.execute(text('SELECT COUNT(*) FROM solicitud')).scalar(), 3)

    def test_report_uses_actual_rows_date_boundary_site_and_formula_escape(self):
        with self.engine.connect() as conn:
            content, count = jobs.report_csv(conn, 1, '2026-10-20', '2026-10-20')
        self.assertEqual(count, 1)
        self.assertIn("'=1+1", content)
        self.assertNotIn('Otra sede', content)
        self.assertNotIn('Otro día', content)

    def test_report_empty_has_headers_no_fabricated_rows(self):
        with self.engine.connect() as conn:
            content, count = jobs.report_csv(conn, 3)
        self.assertEqual(count, 0)
        self.assertEqual(len(content.splitlines()), 1)

    def test_report_rejects_bad_dates_missing_site_and_overflow(self):
        with self.engine.connect() as conn:
            for args in [(1, 'bad', None), (1, '2026-10-21', '2026-10-20'), (99, None, None)]:
                with self.assertRaises(ValueError):
                    jobs.report_csv(conn, *args)
            with self.assertRaisesRegex(ValueError, 'acotá'):
                jobs.report_csv(conn, 1, limit=1)

    def test_report_lambda_queries_db_and_writes_csv_to_s3(self):
        with patch.object(report_lambda, 'database_engine', return_value=self.engine), patch.object(self.engine, 'dispose'):
            result = report_lambda.lambda_handler({'sede_id': 1, 'desde': '2026-10-20', 'hasta': '2026-10-20'}, None)
        self.assertTrue(result['ok'])
        self.assertEqual(result['filas'], 1)
        put = report_lambda.s3.put_object.call_args.kwargs
        self.assertTrue(put['Key'].startswith('reportes/1/'))
        self.assertIn("'=1+1", put['Body'].decode('utf-8-sig'))

    def test_document_lambda_persists_actual_result_and_pins_version(self):
        with patch.object(document_lambda, 'database_engine', return_value=self.engine), patch.object(self.engine, 'dispose'):
            self.assertEqual(document_lambda.lambda_handler(self.event(), None), {'batchItemFailures': []})
        row = self.document()
        self.assertEqual((row['estado'], row['version_id']), ('valido', 'version-1'))
        self.assertEqual(document_lambda.s3.get_object.call_args.kwargs['VersionId'], 'version-1')

    def test_document_rejects_fake_pdf_empty_large_and_wrong_mime(self):
        cases = [({'ContentLength': 20, 'ContentType': 'text/plain'}, b'%PDF-'),
                 ({'ContentLength': 0, 'ContentType': 'application/pdf'}, b''),
                 ({'ContentLength': document_lambda.MAX_BYTES + 1, 'ContentType': 'application/pdf'}, b'%PDF-'),
                 ({'ContentLength': 20, 'ContentType': 'application/pdf'}, b'hello')]
        for head, content in cases:
            document_lambda.s3.head_object.return_value = head
            document_lambda.s3.get_object.side_effect = lambda **kwargs: {'Body': io.BytesIO(content)}
            self.assertIsNotNone(document_lambda.validate_object(self.record())[-1])

    def test_document_retries_only_failed_message_and_handles_test_event(self):
        event = self.event()
        event['Records'] += [{'messageId': 'bad', 'body': '{'},
                             {'messageId': 'test', 'body': '{"Event":"s3:TestEvent"}'}]
        with patch.object(document_lambda, 'database_engine', return_value=self.engine), patch.object(self.engine, 'dispose'):
            result = document_lambda.lambda_handler(event, None)
        self.assertEqual(result, {'batchItemFailures': [{'itemIdentifier': 'bad'}]})
        self.assertEqual(self.document()['estado'], 'valido')

    def test_document_db_failure_is_retryable(self):
        with patch.object(document_lambda, 'database_engine', side_effect=RuntimeError('DB down')):
            result = document_lambda.lambda_handler(self.event(), None)
        self.assertEqual(result, {'batchItemFailures': [{'itemIdentifier': 'one'}]})
        self.assertEqual(self.document()['estado'], 'pendiente')

    def test_document_duplicate_old_event_and_old_key_cannot_overwrite(self):
        with self.engine.begin() as conn:
            self.assertTrue(jobs.save_document_result(conn, 'empresas/a/1.pdf', 'new', '10', 20, None))
            for key, sequence in [('empresas/a/1.pdf', '010'), ('empresas/a/1.pdf', 'F'), ('empresas/a/old.pdf', '11')]:
                self.assertFalse(jobs.save_document_result(conn, key, 'old', sequence, 20, 'inválido'))
        self.assertEqual(self.document()['version_id'], 'new')

    def test_approved_request_keeps_validated_version(self):
        with self.engine.begin() as conn:
            jobs.save_document_result(conn, 'empresas/a/1.pdf', 'approved', '10', 20, None)
            conn.execute(text("UPDATE solicitud SET estado='aprobada' WHERE id=1"))
            self.assertFalse(jobs.save_document_result(conn, 'empresas/a/1.pdf', 'replacement', '11', 20, None))
        self.assertEqual(self.document()['version_id'], 'approved')


class ApiJobsTests(unittest.TestCase):
    setUp = auth_tests.ApiTests.setUp
    headers = auth_tests.ApiTests.headers

    def test_invalid_upload_size_preserves_previous_document(self):
        headers = self.headers()
        original = self.client.post('/api/solicitudes/1/documento', json={'tamano': 100}, headers=headers).get_json()
        for size in (0, -1, True, '100', None, api.MAX_PDF_SIZE_MB * 1024 * 1024 + 1):
            with self.subTest(size=size):
                result = self.client.post('/api/solicitudes/1/documento', json={'tamano': size}, headers=headers)
                self.assertEqual(result.status_code, 400)
        with api.app.app_context():
            self.assertEqual(api.db.session.get(api.Solicitud, 1).documento_s3_key, original['key'])
            self.assertEqual(api.db.session.execute(select(jobs.documents.c.s3_key)).scalar_one(), original['key'])

    def test_upload_records_pending_and_blocks_approval_until_validated(self):
        self.assertEqual(self.client.post('/api/solicitudes/1/documento', json={'tamano': 100}, headers=self.headers()).status_code, 200)
        self.current_groups = ['responsables_sede']
        headers = self.headers('responsables_sede')
        request = self.client.get('/api/solicitudes/pendientes', headers=headers).get_json()[0]
        self.assertEqual(request['documento_estado'], 'pendiente')
        self.assertIsNone(request['url_documento'])
        self.assertEqual(self.client.post('/api/solicitudes/1/resolver', json={'accion': 'aprobada'}, headers=headers).status_code, 409)
        with api.app.app_context(), api.db.engine.begin() as conn:
            key = conn.execute(select(jobs.documents.c.s3_key)).scalar_one()
            jobs.save_document_result(conn, key, 'valid-version', 'A', 20, None)
        request = self.client.get('/api/solicitudes/pendientes', headers=headers).get_json()[0]
        self.assertEqual(request['documento_estado'], 'valido')
        self.assertEqual(api.s3.generate_presigned_url.call_args.kwargs['Params']['VersionId'], 'valid-version')
        self.assertEqual(self.client.post('/api/solicitudes/1/resolver', json={'accion': 'aprobada'}, headers=headers).status_code, 200)
        self.current_groups = ['transportistas']
        self.assertEqual(self.client.post('/api/solicitudes/1/documento', headers=self.headers()).status_code, 409)

    def test_cloud_report_passes_trusted_site_to_lambda_and_handles_errors(self):
        original = api.lambda_client.invoke.side_effect
        calls = []
        def invoke(**kwargs):
            if kwargs['FunctionName'] != 'report':
                return original(**kwargs)
            calls.append(json.loads(kwargs['Payload']))
            return {'Payload': io.BytesIO(b'{"ok":true,"url":"https://s3.invalid/signed","filas":0}')}
        api.lambda_client.invoke.side_effect = invoke
        with patch.object(api, 'REPORT_LAMBDA_NAME', 'report'):
            response = self.client.get('/api/reporte/exportar?desde=2026-10-20', headers=self.headers('responsables_sede'))
            self.assertEqual(response.status_code, 200)
            self.assertEqual(str(calls[0]['sede_id']), '1')
            # Un sede_id del navegador se ignora: la Lambda recibe siempre la sede del perfil.
            self.assertEqual(self.client.get('/api/reporte/exportar?sede_id=2', headers=self.headers('responsables_sede')).status_code, 200)
            self.assertEqual([str(call['sede_id']) for call in calls], ['1', '1'])
            def fail(**kwargs):
                return {'FunctionError': 'Unhandled'} if kwargs['FunctionName'] == 'report' else original(**kwargs)
            api.lambda_client.invoke.side_effect = fail
            with self.assertLogs(api.app.logger, level='ERROR'):
                self.assertEqual(self.client.get('/api/reporte/exportar', headers=self.headers('responsables_sede')).status_code, 503)

    def test_cloud_startup_does_not_create_schema(self):
        from sqlalchemy import inspect
        with patch.dict(ENV, {'AUTO_INIT_DB': 'false'}):
            fresh = load('fresh_api_no_schema', 'backend/app.py')
        with fresh.app.app_context():
            self.assertEqual(inspect(fresh.db.engine).get_table_names(), [])
        self.assertEqual(fresh.app.test_client().get('/health').status_code, 503)

    def test_health_fails_when_db_or_schema_is_unavailable(self):
        with patch.object(api, 'check_ready', side_effect=RuntimeError('missing schema')):
            self.assertEqual(self.client.get('/health').status_code, 503)
        self.assertEqual(self.client.get('/health').status_code, 200)


@unittest.skipUnless(os.environ.get('TEST_POSTGRES_URL'), 'Requiere PostgreSQL de pruebas; configurado en CI')
class SchemaPostgresTests(unittest.TestCase):
    def setUp(self):
        self.namespace = 'test_schema_' + uuid.uuid4().hex
        self.admin = create_engine(os.environ['TEST_POSTGRES_URL'])
        with self.admin.begin() as conn:
            conn.exec_driver_sql(f'CREATE SCHEMA {self.namespace}')
        self.engine = create_engine(os.environ['TEST_POSTGRES_URL'],
                                    connect_args={'options': f'-c search_path={self.namespace}'})

    def tearDown(self):
        self.engine.dispose()
        with self.admin.begin() as conn:
            conn.exec_driver_sql(f'DROP SCHEMA {self.namespace} CASCADE')
        self.admin.dispose()

    def test_two_initializers_produce_one_schema_and_seed(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(schema.upgrade, self.engine, api.db.metadata) for _ in range(2)]
            for future in futures:
                future.result(timeout=15)
        with self.engine.connect() as conn:
            schema.check_ready(conn)
            self.assertEqual(conn.execute(text('SELECT COUNT(*) FROM sede')).scalar(), 3)
            content, count = jobs.report_csv(conn, 1)
            self.assertEqual(count, 0)
            self.assertIn('DNI', content)

    def test_new_document_table_has_real_foreign_key(self):
        from sqlalchemy.exc import IntegrityError
        schema.upgrade(self.engine, api.db.metadata)
        with self.assertRaises(IntegrityError), self.engine.begin() as conn:
            conn.execute(jobs.documents.insert().values(solicitud_id=999, s3_key='missing',
                                                        estado='pendiente', actualizado=datetime.now()))
