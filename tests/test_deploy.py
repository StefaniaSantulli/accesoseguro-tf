"""Pruebas del orden y de fallos del despliegue, sin crear recursos AWS."""
import importlib.util
from pathlib import Path
import unittest
import tempfile
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location('deploy', Path(__file__).resolve().parents[1] / 'scripts/deploy.py')
deploy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deploy)


class DeploymentTests(unittest.TestCase):
    def fake_pipeline(self, failure=None):
        from contextlib import ExitStack
        manager = Mock()
        out = {'ecr_repository_url': 'ecr/repo'}
        with ExitStack() as stack:
            for name in ['run', 'bootstrap', 'tf', 'pause_existing', 'apply_phase', 'push_image', 'migrate', 'activate']:
                mocked = stack.enter_context(patch.object(deploy, name))
                manager.attach_mock(mocked, name)
                if name == failure:
                    mocked.side_effect = RuntimeError('test failure')
            stack.enter_context(patch.object(deploy, 'outputs', return_value=out))
            if failure:
                with self.assertRaises(RuntimeError):
                    deploy.deploy('sha', 'bucket', 'account')
            else:
                deploy.deploy('sha', 'bucket', 'account')
        return manager

    def test_migration_precedes_activation_and_image_precedes_migration(self):
        manager = self.fake_pipeline()
        names = [call[0] for call in manager.mock_calls]
        self.assertLess(names.index('push_image'), names.index('migrate'))
        self.assertLess(names.index('migrate'), len(names) - 2)
        self.assertEqual(manager.apply_phase.call_args_list[0].args, ('sha', False))
        self.assertEqual(manager.apply_phase.call_args_list[1].args, ('sha', True))
        self.assertEqual(names[-1], 'activate')

    def test_failed_migration_or_image_does_not_activate_service(self):
        for step in ('migrate', 'push_image'):
            manager = self.fake_pipeline(step)
            manager.activate.assert_not_called()
            self.assertEqual(manager.apply_phase.call_count, 1)

    def test_nonzero_task_exit_fails_deployment(self):
        out = {'ecs_cluster_name': 'cluster', 'deployment': {'task_definition': 'definition', 'network_configuration': {}}}
        with patch.object(deploy, 'aws_json', side_effect=[{'tasks': [{'taskArn': 'task'}]}, {},
                                                          {'tasks': [{'containers': [{'exitCode': 1}]}]}]):
            with self.assertRaisesRegex(RuntimeError, 'inicialización'):
                deploy.migrate(out)

    def test_run_task_failure_is_not_success(self):
        out = {'ecs_cluster_name': 'cluster', 'deployment': {'task_definition': 'definition', 'network_configuration': {}}}
        with patch.object(deploy, 'aws_json', return_value={'failures': [{'reason': 'capacity'}]}):
            with self.assertRaisesRegex(RuntimeError, 'iniciar'):
                deploy.migrate(out)

    def test_partial_deployment_pauses_ecs_without_creating_missing_scaling_or_rule(self):
        out = {'ecs_cluster_name': 'cluster', 'ecs_service_name': 'api'}
        with patch.object(deploy, 'aws_json', side_effect=[{'ScalableTargets': []}, {}, {}, {'Rules': []}]) as aws:
            deploy.pause_existing(out)
        commands = [call.args[1] for call in aws.call_args_list]
        self.assertIn('update-service', commands)
        self.assertNotIn('register-scalable-target', commands)
        self.assertNotIn('disable-rule', commands)

    def test_update_waits_for_already_disabling_worker_and_keeps_audit_active(self):
        out = {'ecs_cluster_name': 'cluster', 'ecs_service_name': 'api',
               'lambda_functions': {'notificar-mail': 'mail', 'archivar-auditoria': 'audit'}}
        with patch.object(deploy, 'aws_json', side_effect=[
            {'ScalableTargets': [{'MinCapacity': 2}]}, {}, {}, {},
            {'EventSourceMappings': [{'UUID': 'mapping', 'State': 'Disabling'}]},
            {'Rules': [{'Name': 'accesoseguro-dev-vencer-permisos'}]}, {},
        ]) as aws, patch.object(deploy, 'wait_mapping_disabled') as wait:
            deploy.pause_existing(out)
        wait.assert_called_once_with('mapping')
        commands = [call.args[1] for call in aws.call_args_list]
        self.assertIn('register-scalable-target', commands)
        self.assertIn('disable-rule', commands)
        self.assertNotIn('update-event-source-mapping', commands)
        self.assertFalse(any('audit' in call.args for call in aws.call_args_list))

    def test_existing_immutable_image_is_reused_but_access_errors_fail(self):
        with patch.object(deploy, 'run', return_value=Mock(stdout='password')) as run:
            with patch.object(deploy, 'aws', return_value=Mock(returncode=0)):
                deploy.push_image('sha', 'ecr/repo')
            self.assertFalse(any('push' in call.args[0] for call in run.call_args_list))
            with patch.object(deploy, 'aws', return_value=Mock(returncode=1, stderr='AccessDenied')):
                with self.assertRaisesRegex(RuntimeError, 'ECR'):
                    deploy.push_image('sha', 'ecr/repo')

    def test_remote_bootstrap_state_is_not_overwritten_by_old_local_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            directory = root / "terraform/bootstrap"
            directory.mkdir(parents=True)
            (directory / "terraform.tfstate").write_text('{"serial":1}')
            with patch.object(deploy, 'ROOT', root), patch.object(deploy, 'bucket_exists', return_value=True), \
                    patch.object(deploy, 'aws', return_value=Mock(returncode=0)), patch.object(deploy, 'tf') as tf:
                deploy.bootstrap('bucket', 'account')
            init = tf.call_args_list[0].args
            self.assertIn('-reconfigure', init)
            self.assertNotIn('-force-copy', init)

    def test_fresh_bootstrap_migrates_state_to_s3_after_apply(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            directory = root / "terraform/bootstrap"
            directory.mkdir(parents=True)
            def terraform(stack, command, *args):
                if command == 'apply':
                    (directory / "terraform.tfstate").write_text('{"serial":1}')
            with patch.object(deploy, 'ROOT', root), patch.object(deploy, 'bucket_exists', side_effect=[False, True]), \
                    patch.object(deploy, 'tf', side_effect=terraform) as tf:
                deploy.bootstrap('bucket', 'account')
            self.assertIn('-migrate-state', tf.call_args_list[-1].args)
            self.assertIn('-backend-config=use_lockfile=true', tf.call_args_list[-1].args)
