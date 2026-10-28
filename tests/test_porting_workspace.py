import importlib.util
import io
import json
from pathlib import Path
import shutil
import tempfile
import types
import unittest
from unittest import mock

import yaml


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('porting_workspace', ROOT / 'porting/workspace.py')
workspace = importlib.util.module_from_spec(spec)
spec.loader.exec_module(workspace)


class Connection:
    def __init__(self, rows):
        self.rows = iter(rows)
        self.statements = []
        self.closed = False

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def execute(self, sql, arguments=None):
        self.statements.append((sql, arguments))

    def fetchone(self):
        value = next(self.rows)
        return value if isinstance(value, tuple) else (value,)

    def fetchall(self):
        return next(self.rows)

    def close(self):
        self.closed = True


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.reference, self.state, self.work = [self.root / name for name in ('reference', 'state', 'workspace')]
        for path in (self.reference, self.state, self.work):
            path.mkdir()
        self.environment = dict(PORTING_DB_PASSWORD='scoped-porting-password', DB_ADMIN_PASSWORD='local-admin-password',
                                PORTING_APP_SECRET='new-porting-app-secret-with-at-least-32-bytes',
                                PORTING_REFERENCE_DIR=str(self.reference), PORTING_REFERENCE_STATE_DIR=str(self.state),
                                PORTING_WORKSPACE_DIR=str(self.work))
        self.config = workspace.settings(self.environment)

    def application(self, root):
        for name in ('composer.json', 'composer.lock', 'app/console', 'app/config/parameters.yml', 'vendor/autoload.php'):
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('synthetic ' + name)

    def ready(self):
        app = self.work / 'warexo-port'
        self.application(app)
        record = dict(format=1, app_dir=str(app), database=workspace.identity(self.config), minimum_tables=3)
        (self.work / workspace.READY).write_text(json.dumps(record))
        return record

    def ready_connection(self, tables=None):
        return Connection([('warexo_port', 'DISABLED'), tables if tables is not None else [('users',), ('client',), ('product',)]])

    def test_identifiers_and_source_credentials_fail_closed(self):
        for key, value in [('PORTING_DB_NAME', 'warexo'), ('PORTING_DB_NAME', 'Warexo'),
                           ('PORTING_DB_NAME', 'mysql'), ('PORTING_DB_NAME', 'unsafe;DROP'),
                           ('PORTING_DB_USER', 'root'), ('PORTING_DB_USER', 'warexo'),
                           ('PORTING_DB_HOST', '-o option'), ('PORTING_DB_PORT', '0'),
                           ('PORTING_DB_PASSWORD', ''), ('SOURCE_SSH_PRIVATE_KEY', 'not-allowed')]:
            with self.subTest(key=key, value=value), self.assertRaises(workspace.PortingError):
                workspace.settings(dict(self.environment, **{key: value}))

    def test_paths_reject_roots_nesting_and_symlinked_parents(self):
        for key, value in [('PORTING_WORKSPACE_DIR', '/'), ('PORTING_WORKSPACE_DIR', 'relative'),
                           ('PORTING_WORKSPACE_DIR', str(self.reference / 'nested')),
                           ('PORTING_REFERENCE_STATE_DIR', str(self.reference))]:
            with self.subTest(key=key, value=value), self.assertRaises(workspace.PortingError):
                workspace.paths(dict(self.environment, **{key: value}))
        alias = self.root / 'alias'
        alias.symlink_to(self.work, target_is_directory=True)
        with self.assertRaises(workspace.PortingError):
            workspace.paths(dict(self.environment, PORTING_WORKSPACE_DIR=str(alias / 'nested')))

    def test_existing_databases_users_engines_and_enabled_events_are_rejected(self):
        for rows in [['ON'], ['DISABLED', 1], ['DISABLED', 0, 1], ['DISABLED', 0, 0, (3, 1)], ['DISABLED', 0, 0, (0, None)]]:
            with self.subTest(rows=rows), self.assertRaises(workspace.PortingError):
                workspace.assert_available(Connection(rows), self.config)
        connection = Connection(['DISABLED', 0, 0, (3, 0)])
        self.assertEqual(workspace.assert_available(connection, self.config), 3)
        self.assertTrue(all(sql.startswith('SELECT ') for sql, args in connection.statements))

    def test_database_creation_scopes_grants_and_parameterizes_secret(self):
        connection = Connection([])
        workspace.create_database(connection, self.config)
        joined = '\n'.join(sql for sql, args in connection.statements)
        self.assertIn('`warexo\\_port`.*', joined)
        self.assertNotIn('`warexo_port`.*', joined)
        self.assertNotIn(self.config['password'], joined)
        self.assertEqual(connection.statements[1][1], ('warexo_port', '%', self.config['password']))
        self.assertNotIn('DROP ', joined)
        self.assertNotIn('ALTER USER', joined)

    def test_each_underscore_in_grant_scope_is_literal(self):
        connection = Connection([])
        workspace.create_database(connection, dict(self.config, name='team_a_port'))
        self.assertEqual(connection.statements[-1][0], 'GRANT ALL PRIVILEGES ON `team\\_a\\_port`.* TO %s@%s')
        self.assertIn('CREATE DATABASE `team_a_port`', connection.statements[0][0])

    def test_existing_ready_workspace_is_verified_without_admin_or_reimport(self):
        self.ready()
        edited = self.work / 'warexo-port/composer.json'
        edited.write_text('my local changes')
        connection = self.ready_connection()
        with mock.patch.object(workspace.clone, 'target_connection', return_value=connection) as connect, \
                mock.patch.object(workspace.clone, 'verify_ready') as source_verify, \
                mock.patch.object(workspace, 'copy_files') as copy, mock.patch.object(workspace, 'dump_database') as dump:
            workspace.initialize(self.config, {}, self.reference, self.state, self.work)
        self.assertEqual(edited.read_text(), 'my local changes')
        self.assertEqual(connect.call_args.args[0]['DB_USER'], 'warexo_port')
        self.assertNotEqual(connect.call_args.args[0]['DB_PASSWORD'], self.environment['DB_ADMIN_PASSWORD'])
        self.assertTrue(connection.closed)
        source_verify.assert_not_called()
        copy.assert_not_called()
        dump.assert_not_called()

    def test_incomplete_or_nonempty_workspaces_are_never_overwritten(self):
        for filename in (workspace.STARTED, 'user-owned-file'):
            path = self.work / filename
            path.write_text('keep me')
            with mock.patch.object(workspace.clone, 'target_connection') as connect, self.assertRaises(workspace.PortingError):
                workspace.initialize(self.config, self.environment, self.reference, self.state, self.work)
            self.assertEqual(path.read_text(), 'keep me')
            connect.assert_not_called()
            path.unlink()

    def test_marker_identity_mismatch_and_invalid_json_do_not_connect(self):
        record = self.ready()
        record['database']['name'] = 'some_other_database'
        marker = self.work / workspace.READY
        for contents in (json.dumps(record), 'invalid json'):
            marker.write_text(contents)
            with mock.patch.object(workspace.clone, 'target_connection') as connect, self.assertRaises(workspace.PortingError):
                workspace.verify_ready(self.config, self.work)
            connect.assert_not_called()

    def test_symlinked_marker_or_application_file_is_refused(self):
        self.ready()
        marker = self.work / workspace.READY
        actual = self.root / 'actual-marker'
        marker.rename(actual)
        marker.symlink_to(actual)
        with self.assertRaises(workspace.PortingError):
            workspace.verify_ready(self.config, self.work)
        marker.unlink()
        actual.rename(marker)
        manifest = self.work / 'warexo-port/composer.json'
        manifest.unlink()
        manifest.symlink_to(self.root / 'outside')
        (self.root / 'outside').write_text('keep')
        with self.assertRaises(workspace.PortingError):
            workspace.verify_ready(self.config, self.work)

    def test_reuse_allows_legitimate_framework_layout_changes(self):
        self.ready()
        for relative in ('app/console', 'app/config/parameters.yml', 'vendor/autoload.php'):
            (self.work / 'warexo-port' / relative).unlink()
        with mock.patch.object(workspace.clone, 'target_connection', return_value=self.ready_connection()):
            workspace.verify_ready(self.config, self.work)

    def test_stale_marker_cannot_recreate_missing_database_tables(self):
        self.ready()
        for tables in ([], [('users',), ('client',)], [('one',), ('two',), ('three',)]):
            connection = self.ready_connection(tables)
            with mock.patch.object(workspace.clone, 'target_connection', return_value=connection), self.assertRaises(workspace.PortingError):
                workspace.verify_ready(self.config, self.work)
            self.assertTrue(connection.closed)

    def test_connection_error_does_not_expose_credentials(self):
        self.ready()
        with mock.patch.object(workspace.clone, 'target_connection', side_effect=RuntimeError('SECRET-sensitive-password')):
            with self.assertRaises(workspace.PortingError) as error:
                workspace.verify_ready(self.config, self.work)
        self.assertNotIn('SECRET', str(error.exception))

    @unittest.skipUnless(shutil.which('rsync'), 'rsync not installed')
    def test_copy_retains_vendor_and_uploads_but_not_git_config_or_cache(self):
        self.application(self.reference)
        for name in ('.git/config', '.env.local', 'app/cache/old.cache', 'var/cache/foo', 'web/uploads/local.txt', 'vendor/patched.php'):
            path = self.reference / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('synthetic ' + name)
        target = self.work / 'warexo-port'
        with (self.work / 'synthetic-error.log').open('wb') as errors:
            workspace.copy_files(self.reference, target, errors)
        for name in ('.git', '.env.local', 'app/config/parameters.yml', 'app/cache', 'var/cache'):
            self.assertFalse((target / name).exists(), name)
        for name in ('vendor/patched.php', 'web/uploads/local.txt'):
            self.assertEqual((target / name).read_text(), (self.reference / name).read_text())
        self.assertTrue((self.reference / 'app/config/parameters.yml').exists())

    def test_external_reference_symlink_is_rejected_before_copy(self):
        self.application(self.reference)
        (self.reference / 'external').symlink_to(self.root / 'outside')
        (self.root / 'outside').write_text('private')
        with mock.patch.object(workspace.subprocess, 'run') as run, self.assertRaises(workspace.PortingError):
            workspace.copy_files(self.reference, self.work / 'warexo-port', io.BytesIO())
        run.assert_not_called()

    def test_dump_uses_local_secret_only_in_environment_and_never_imports_events(self):
        def run(command, **options):
            options['stdout'].write(b'SELECT 1;\n')
            options['stdout'].flush()
            return types.SimpleNamespace(returncode=0)
        dump = self.work / 'synthetic.sql'
        with mock.patch.object(workspace.shutil, 'which', return_value='/usr/bin/mariadb-dump'), \
                mock.patch.object(workspace.subprocess, 'run', side_effect=run) as execute:
            workspace.dump_database(self.config, self.environment, dump, io.BytesIO())
        arguments = execute.call_args.args[0]
        self.assertIn('--single-transaction', arguments)
        self.assertIn('--skip-events', arguments)
        self.assertNotIn(self.environment['DB_ADMIN_PASSWORD'], ' '.join(arguments))
        self.assertEqual(execute.call_args.kwargs['env']['MYSQL_PWD'], self.environment['DB_ADMIN_PASSWORD'])
        self.assertEqual(dump.stat().st_mode & 0o777, 0o600)
        self.assertEqual(arguments[-1], 'warexo')

    def test_generated_parameters_are_target_only_and_do_not_execute_app(self):
        target = self.work / 'warexo-port'
        target.mkdir()
        with mock.patch.object(workspace.subprocess, 'run') as run:
            workspace.write_parameters(target, self.config, self.environment)
        filename = target / 'app/config/parameters.yml'
        values = yaml.safe_load(filename.read_text())['parameters']
        self.assertEqual(values['database_name'], 'warexo_port')
        self.assertEqual(values['database_user'], 'warexo_port')
        self.assertEqual(values['database_password'], self.config['password'])
        self.assertEqual(values['mailer_host'], 'mailpit')
        self.assertEqual(filename.stat().st_mode & 0o777, 0o640)
        self.assertNotIn(self.environment['DB_ADMIN_PASSWORD'], filename.read_text())
        run.assert_not_called()

    def test_cookie_settings_follow_explicit_local_configuration(self):
        values = workspace.parameter_values(self.config, dict(self.environment, WAREXO_COOKIE_SECURE='false',
                                                             WAREXO_COOKIE_LIFETIME='3600', WAREXO_GC_MAX_LIFETIME='7200'))
        self.assertFalse(values['cookiesecure'])
        self.assertEqual(values['cookielifetime'], 3600)
        self.assertEqual(values['gcmaxlifetime'], 7200)
        for key, value in [('WAREXO_COOKIE_SECURE', 'maybe'), ('WAREXO_COOKIE_LIFETIME', '-1'), ('WAREXO_GC_MAX_LIFETIME', 'not-numeric')]:
            with self.subTest(key=key), self.assertRaises(workspace.PortingError):
                workspace.parameter_values(self.config, dict(self.environment, **{key: value}))


if __name__ == '__main__':
    unittest.main()
