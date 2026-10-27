import base64
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shlex
import tempfile
import types
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('import_source', ROOT / 'clone/import_source.py')
importer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(importer)


PARAMETERS = b'''parameters:
  database_driver: pdo_mysql
  database_host: localhost
  database_port: null
  database_name: warexo_live
  database_user: source-user
  database_password: "quote ' and dollar $ and backslash \\\\"
'''


class Cursor:
    def __init__(self, values):
        self.values = iter(values)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def execute(self, sql):
        self.sql = sql

    def fetchone(self):
        return (next(self.values),)

    def fetchall(self):
        return next(self.values)


class Connection:
    def __init__(self, values):
        self._cursor = Cursor(values)
        self.closed = False

    def cursor(self):
        return self._cursor

    def close(self):
        self.closed = True


class CloneTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.app = Path(self.temp.name) / 'app'
        self.state = Path(self.temp.name) / 'state'
        self.app.mkdir()
        self.state.mkdir()
        self.env = dict(SOURCE_SSH_HOST='source.example.invalid', SOURCE_SSH_PORT='222',
                        SOURCE_SSH_USER='source-user', SOURCE_WAREXO_PATH='/srv/warexo',
                        SOURCE_SSH_PRIVATE_KEY='PRIVATE KEY', SOURCE_SSH_KNOWN_HOSTS='[source.example.invalid]:222 ssh-ed25519 FAKE',
                        DB_HOST='db', DB_PASSWORD='local-password', CLONE_ADMIN_PASSWORD='local-admin-password')

    def test_reads_yaml_as_data_and_preserves_password(self):
        database = importer.source_database(PARAMETERS)
        self.assertEqual(database['port'], '3306')
        self.assertEqual(database['password'], "quote ' and dollar $ and backslash \\")
        with self.assertRaises(importer.CloneError):
            importer.source_database(b'!!python/object/apply:os.system ["touch /tmp/not-run"]')
        with self.assertRaises(importer.CloneError):
            importer.source_database(b'parameters: {database_name: "%env(DB)%", database_user: user}')

    def test_ssh_inputs_reject_options_and_control_characters(self):
        for field, value in [('SOURCE_SSH_HOST', '-oProxyCommand=bad'), ('SOURCE_SSH_USER', 'x\nHost *'),
                             ('SOURCE_SSH_PORT', '0'), ('SOURCE_PHP_BIN', 'php -r evil'),
                             ('SOURCE_WAREXO_PATH', '/'), ('SOURCE_WAREXO_PATH', '/srv\nwarexo')]:
            with self.subTest(field=field), self.assertRaises(importer.CloneError):
                importer.validate_source(dict(self.env, **{field: value}))

    def test_remote_paths_are_quoted_without_evaluation(self):
        path = "/srv/it's $HOME; $(touch bad) `id`/parameters.yml"
        with mock.patch.object(importer.subprocess, 'run', return_value=types.SimpleNamespace(returncode=0, stdout=b'ok')) as run:
            importer.remote(['ssh', '-F', '/tmp/config', 'warexo-source'], ['cat', '--', path], io.BytesIO())
        self.assertEqual(shlex.split(run.call_args.args[0][-1]), ['cat', '--', path])

    def test_strict_host_verification_and_secret_file_modes(self):
        source = importer.validate_source(self.env)
        arguments = importer.make_ssh_config(Path(self.temp.name), source, self.env)
        config = Path(self.temp.name, 'ssh_config').read_text()
        self.assertIn('StrictHostKeyChecking yes', config)
        self.assertIn('Port 222', config)
        self.assertNotIn('PRIVATE KEY', ' '.join(arguments))
        for name in ('identity', 'known_hosts', 'ssh_config'):
            self.assertEqual(Path(self.temp.name, name).stat().st_mode & 0o777, 0o600)
        with self.assertRaises(importer.CloneError):
            importer.make_ssh_config(Path(self.temp.name), source, dict(self.env, SOURCE_SSH_KNOWN_HOSTS=''))

    def test_source_credentials_only_enter_stdin_payload(self):
        source = importer.validate_source(self.env)
        database = importer.source_database(PARAMETERS)
        payload = importer.helper_payload(source, database, 'dump')
        self.assertNotIn(database['password'], ' '.join(importer.php_arguments(source)))
        encoded = payload.split(b"base64_decode('", 1)[1].split(b"'", 1)[0]
        self.assertEqual(json.loads(base64.b64decode(encoded))['database'], database)
        self.assertIn('opcache.enable_cli=0', importer.php_arguments(source))
        self.assertIn('opcache.preload=', importer.php_arguments(source))

    def test_nonempty_target_database_is_rejected(self):
        connection = Connection([3])
        with mock.patch.object(importer, 'target_connection', return_value=connection), self.assertRaises(importer.CloneError):
            importer.assert_empty_database(self.env)
        self.assertTrue(connection.closed)

    def test_events_must_be_disabled_at_server_startup(self):
        for value in ('ON', 'OFF'):
            with mock.patch.object(importer, 'target_connection', return_value=Connection([0, value])), self.assertRaises(importer.CloneError):
                importer.assert_empty_database(self.env)
        with mock.patch.object(importer, 'target_connection', return_value=Connection([0, 'DISABLED'])):
            importer.assert_empty_database(self.env)

    def test_table_count_mismatch_blocks_ready(self):
        with mock.patch.object(importer, 'target_connection', return_value=Connection([2])), self.assertRaises(importer.CloneError):
            importer.verify_restored_tables(self.env, 3)

    def test_definers_rewritten_only_in_recognized_headers(self):
        ddl = b'/*!50003 CREATE*/ /*!50017 DEFINER=`user``quote`@`localhost`*/ /*!50003 TRIGGER x */;\n'
        self.assertIn(b'DEFINER=CURRENT_USER', importer.normalize_definer(ddl))
        data = b"INSERT INTO text VALUES ('DEFINER=`real`@`host`');\n"
        self.assertEqual(importer.normalize_definer(data), data)
        with self.assertRaises(importer.CloneError):
            importer.normalize_definer(b'CREATE DEFINER="user"@"host" PROCEDURE x() SELECT 1;\n')
        self.assertEqual(importer.normalize_definer(b"/*!50003 SELECT 'DEFINER=`x`@`y`' */;\n"), b"/*!50003 SELECT 'DEFINER=`x`@`y`' */;\n")

    def test_multiline_routine_literal_is_not_rewritten(self):
        normalizer = importer.DumpNormalizer()
        header = b'CREATE DEFINER=`original`@`host` PROCEDURE test()\n'
        self.assertIn(b'DEFINER=CURRENT_USER', normalizer.feed(header))
        normalizer.feed(b"BEGIN\n")
        normalizer.feed(b"SET @text = 'literal beginning\n")
        embedded = b'CREATE DEFINER=`text`@`not-an-account` VIEW x AS SELECT 1;\n'
        self.assertEqual(normalizer.feed(embedded), embedded)
        normalizer.feed(b"literal end';\n")
        normalizer.feed(b"END;;\n")
        self.assertIn(b'DEFINER=CURRENT_USER', normalizer.feed(header))

    def test_restore_password_not_in_arguments_and_mysql_client_commands_disabled(self):
        dump = self.state / 'dump.sql'
        dump.write_bytes(b'SELECT 1;\n')
        stdin = mock.Mock()
        stdin.closed = False
        process = mock.Mock(stdin=stdin)
        process.wait.return_value = 0
        with mock.patch.object(importer.shutil, 'which', return_value='/usr/bin/mariadb'), mock.patch.object(importer.subprocess, 'Popen', return_value=process) as popen:
            importer.restore_database(self.env, dump, io.BytesIO())
        args = popen.call_args.args[0]
        self.assertIn('--binary-mode', args)
        self.assertNotIn('local-password', ' '.join(args))
        self.assertNotIn('SOURCE_SSH_PRIVATE_KEY', popen.call_args.kwargs['env'])
        self.assertEqual(popen.call_args.kwargs['env']['MYSQL_PWD'], 'local-password')

    def completed_state(self, minimum=2):
        manifest = self.state / 'clone-manifest.json'
        manifest.write_text(json.dumps(dict(source=dict(base_tables=minimum), target_database='warexo')))
        (self.state / importer.READY).write_text(json.dumps(dict(format=1, manifest_sha256=hashlib.sha256(manifest.read_bytes()).hexdigest())))

    def test_existing_clone_does_not_connect_to_source(self):
        self.completed_state()
        connection = Connection(['warexo', 'DISABLED', [('users',), ('client',)], 1, 1])
        with mock.patch.object(importer, 'validate_source') as source, mock.patch.object(importer, 'remote') as remote, \
                mock.patch.object(importer, 'target_connection', return_value=connection) as target:
            importer.import_once({}, self.app, self.state)
        source.assert_not_called()
        remote.assert_not_called()
        target.assert_called_once()
        self.assertTrue(connection.closed)

    def test_ready_check_allows_development_tables(self):
        self.completed_state()
        connection = Connection(['warexo', 'DISABLED', [('users',), ('client',), ('new_development_table',)], 1, 1])
        with mock.patch.object(importer, 'target_connection', return_value=connection):
            importer.verify_ready({}, self.state)

    def test_ready_check_blocks_missing_empty_or_wrong_database(self):
        self.completed_state()
        cases = [
            ['another_database'],
            ['warexo', 'ON'],
            ['warexo', 'DISABLED', []],
            ['warexo', 'DISABLED', [('users',), ('other_table',)]],
            ['warexo', 'DISABLED', [('users',), ('client',)], 0],
            ['warexo', 'DISABLED', [('users',), ('client',)], 1, 0],
        ]
        for values in cases:
            connection = Connection(values)
            with self.subTest(values=values), mock.patch.object(importer, 'target_connection', return_value=connection), self.assertRaises(importer.CloneError):
                importer.verify_ready({}, self.state)
            self.assertTrue(connection.closed)
        with mock.patch.object(importer, 'target_connection', side_effect=RuntimeError('private credential detail')), self.assertRaises(importer.CloneError) as error:
            importer.verify_ready({}, self.state)
        self.assertNotIn('private credential', str(error.exception))

    def test_ready_check_rejects_lost_tables_but_keeps_source_disconnected(self):
        self.completed_state(minimum=3)
        with mock.patch.object(importer, 'target_connection', return_value=Connection(['warexo', 'DISABLED', [('users',), ('client',)]])), \
                mock.patch.object(importer, 'remote') as remote, self.assertRaises(importer.CloneError):
            importer.import_once({}, self.app, self.state)
        remote.assert_not_called()

    def test_verify_ready_cli_never_imports_without_a_marker(self):
        with mock.patch.dict(os.environ, {'WAREXO_APP_DIR': str(self.app), 'WAREXO_STATE_DIR': str(self.state)}, clear=True), \
                mock.patch.object(importer, 'import_once') as run_import, mock.patch.object(importer, 'remote') as remote:
            self.assertEqual(importer.main(['--verify-ready']), 1)
        run_import.assert_not_called()
        remote.assert_not_called()
        self.assertEqual(list(self.state.iterdir()), [])

    def test_verify_ready_cli_needs_no_source_or_admin_password(self):
        self.completed_state()
        connection = Connection(['warexo', 'DISABLED', [('users',), ('client',)], 1, 1])
        with mock.patch.dict(os.environ, {'WAREXO_APP_DIR': str(self.app), 'WAREXO_STATE_DIR': str(self.state)}, clear=True), \
                mock.patch.object(importer, 'target_connection', return_value=connection), mock.patch.object(importer, 'remote') as remote:
            self.assertEqual(importer.main(['--verify-ready']), 0)
        remote.assert_not_called()

    def test_tampered_manifest_blocks_reuse_without_reimport(self):
        (self.state / 'clone-manifest.json').write_text('{}')
        (self.state / importer.READY).write_text('{"format": 1, "manifest_sha256": "wrong"}')
        with self.assertRaises(importer.CloneError), mock.patch.object(importer, 'remote') as remote:
            importer.import_once({}, self.app, self.state)
        remote.assert_not_called()

    def test_bad_admin_password_and_source_preflight_remain_retryable(self):
        with self.assertRaises(importer.CloneError):
            importer.import_once(dict(self.env, CLONE_ADMIN_PASSWORD='short'), self.app, self.state)
        self.assertFalse((self.state / importer.STARTED).exists())
        with mock.patch.object(importer, 'assert_empty_database'), mock.patch.object(importer, 'remote', side_effect=importer.CloneError('SSH preflight failed')):
            with self.assertRaises(importer.CloneError):
                importer.import_once(self.env, self.app, self.state)
        self.assertFalse((self.state / importer.STARTED).exists())
        self.assertEqual(list(self.app.iterdir()), [])

    def test_dirty_or_incomplete_target_is_never_overwritten(self):
        (self.app / 'work-in-progress.php').write_text('keep me')
        with self.assertRaises(importer.CloneError):
            importer.import_once(self.env, self.app, self.state)
        (self.app / 'work-in-progress.php').unlink()
        (self.state / importer.STARTED).write_text('started')
        with self.assertRaises(importer.CloneError):
            importer.import_once(self.env, self.app, self.state)

    def fake_remote(self, ssh, arguments, error_log, payload=None, output=None):
        if arguments[0] == 'head':
            return PARAMETERS
        if output is not None:
            output.write(b'CREATE TABLE fixture (id INT);\n')
            return None
        return b'{"files": {}, "base_tables": 1}'

    def run_import_with_sanitizer(self, returncode):
        with mock.patch.object(importer, 'assert_empty_database'), mock.patch.object(importer, 'remote', side_effect=self.fake_remote), \
                mock.patch.object(importer, 'copy_files'), mock.patch.object(importer, 'restore_database'), \
                mock.patch.object(importer, 'verify_restored_tables'), \
                mock.patch.object(importer.subprocess, 'run', return_value=types.SimpleNamespace(returncode=returncode)) as sanitizer:
            importer.import_once(self.env, self.app, self.state)
        return sanitizer

    def test_failed_sanitization_never_marks_ready(self):
        with self.assertRaises(importer.CloneError):
            self.run_import_with_sanitizer(1)
        self.assertTrue((self.state / importer.STARTED).is_file())
        self.assertFalse((self.state / importer.READY).exists())
        self.assertEqual((self.state / 'source-database.sql').stat().st_mode & 0o777, 0o600)

    def test_success_removes_dump_and_strips_source_credentials_from_sanitizer(self):
        sanitizer = self.run_import_with_sanitizer(0)
        ready = json.loads((self.state / importer.READY).read_text())
        self.assertEqual(ready['format'], 1)
        self.assertFalse((self.state / 'source-database.sql').exists())
        self.assertTrue((self.state / 'clone-manifest.json').is_file())
        self.assertNotIn('SOURCE_SSH_PRIVATE_KEY', sanitizer.call_args.kwargs['env'])
        self.assertEqual((self.state / importer.READY).stat().st_mode & 0o777, 0o644)


if __name__ == '__main__':
    unittest.main()
