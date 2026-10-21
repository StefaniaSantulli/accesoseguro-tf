"""Ejecuta el script real con una AWS CLI falsa: nunca contacta AWS."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / 'terraform/modules/s3-bucket/bucket.sh'
FAKE_AWS = '''
import json, os, sys
from pathlib import Path
import jmespath
path = Path(os.environ['AWS_TEST_STEPS'])
steps = json.loads(path.read_text())
if not steps:
    sys.exit('Llamada inesperada: ' + ' '.join(sys.argv[1:]))
step = steps.pop(0)
path.write_text(json.dumps(steps))
assert sys.argv[1:3] == ['s3api', step['command']], sys.argv
if 'delete' in step:
    value = sys.argv[sys.argv.index('--delete') + 1]
    actual = json.loads(Path(value.removeprefix('file://')).read_text())
    assert actual['Objects'] == step['delete'], actual
value = step.get('output', {})
if '--query' in sys.argv:
    value = jmespath.search(sys.argv[sys.argv.index('--query') + 1], value)
print(json.dumps(value))
print(step.get('error', ''), file=sys.stderr)
sys.exit(step.get('code', 0))
'''


class BucketTests(unittest.TestCase):
    def run_bucket(self, steps, action='delete', force='true'):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake = root / 'aws'
            fake.write_text('#!' + sys.executable + '\n' + FAKE_AWS)
            fake.chmod(0o755)
            pending = root / 'steps.json'
            pending.write_text(json.dumps(steps))
            env = {**os.environ, 'PATH': directory + os.pathsep + os.environ['PATH'],
                   'AWS_TEST_STEPS': str(pending)}
            result = subprocess.run(['bash', str(SCRIPT), action, 'test-bucket', 'us-east-1', force],
                                    env=env, capture_output=True, text=True, timeout=10)
            self.assertEqual(json.loads(pending.read_text()), [], result.stderr)
            return result

    def test_missing_bucket_is_successful_noop(self):
        result = self.run_bucket([{'command': 'head-bucket', 'code': 254,
                                  'error': 'An error occurred (404) when calling HeadBucket'}])
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_access_denied_or_expired_credentials_are_not_absence(self):
        for action in ('create', 'delete'):
            for error in ('403', 'ExpiredToken'):
                with self.subTest(action=action, error=error):
                    result = self.run_bucket([{'command': 'head-bucket', 'code': 254,
                                              'error': f'An error occurred ({error}) when calling HeadBucket'}], action)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn(error, result.stderr)

    def test_marker_page_does_not_hide_later_versions_or_unversioned_objects(self):
        marker = {'Key': 'a.pdf', 'VersionId': 'marker'}
        version = {'Key': 'b.pdf', 'VersionId': 'version'}
        plain = {'Key': 'c.pdf', 'VersionId': 'null'}
        result = self.run_bucket([
            {'command': 'head-bucket'},
            {'command': 'list-object-versions', 'output': {'DeleteMarkers': [marker], 'IsTruncated': True}},
            {'command': 'delete-objects', 'delete': [marker]},
            {'command': 'list-object-versions', 'output': {'Versions': [version, plain]}},
            {'command': 'delete-objects', 'delete': [version, plain]},
            {'command': 'list-object-versions'},
            {'command': 'delete-bucket'},
        ])
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_partial_delete_failure_stops_without_retrying_forever(self):
        version = {'Key': 'locked.pdf', 'VersionId': 'version'}
        result = self.run_bucket([
            {'command': 'head-bucket'},
            {'command': 'list-object-versions', 'output': {'Versions': [version]}},
            {'command': 'delete-objects', 'delete': [version],
             'output': {'Errors': [{'Key': 'locked.pdf', 'Code': 'AccessDenied'}]}},
        ])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('AccessDenied', result.stderr)

    def test_without_force_nonempty_bucket_is_not_forgotten_or_emptied(self):
        result = self.run_bucket([
            {'command': 'head-bucket'},
            {'command': 'delete-bucket', 'code': 254, 'error': 'BucketNotEmpty'},
        ], force='false')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('BucketNotEmpty', result.stderr)


if __name__ == '__main__':
    unittest.main()
