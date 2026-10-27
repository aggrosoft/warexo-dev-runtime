#!/usr/bin/env python3
"""Import one existing Warexo installation into empty, disposable volumes."""

import argparse
import base64
import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile

import yaml


EXCLUDES = [
    '.git', '.ssh', 'node_modules', '/backups', '/app/cache', '/app/logs',
    '/app/temp', '/app/config/parameters.yml*', '/.env*', '*.pem', '*.key',
    '/web/install.php', '/web/app_dev.php', '/web/restore*',
]
READY = 'clone-ready'
STARTED = 'clone-started'
DEFINER = re.compile(rb'DEFINER\s*=\s*`(?:``|[^`])*`\s*@\s*`(?:``|[^`])*`')
DEFINER_HEADER = re.compile(rb'^(?:CREATE(?:\s+ALGORITHM=[A-Za-z_]+)?\s+|/\*![0-9]{5,6}\s+(?:CREATE\*/\s*/\*![0-9]{5,6}\s+)?)DEFINER\s*=')


class CloneError(RuntimeError):
    pass


def log(message):
    print('[warexo-clone] ' + message, flush=True)


def secret(environment, name):
    filename = environment.get(name + '_FILE')
    if filename:
        value = Path(filename).read_text()
    else:
        value = environment.get(name, '').replace('\\n', '\n')
    if not value.strip():
        raise CloneError(name + ' or ' + name + '_FILE is required.')
    return value.rstrip() + '\n'


def validate_source(environment):
    host = environment.get('SOURCE_SSH_HOST', '')
    user = environment.get('SOURCE_SSH_USER', '')
    path = environment.get('SOURCE_WAREXO_PATH', '')
    php = environment.get('SOURCE_PHP_BIN', 'php')
    if not re.fullmatch(r'[A-Za-z0-9\[\]][A-Za-z0-9_.:\[\]-]*', host):
        raise CloneError('SOURCE_SSH_HOST must be a host name or IP address.')
    if not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.@-]*', user):
        raise CloneError('Invalid SOURCE_SSH_USER.')
    try:
        port = int(environment.get('SOURCE_SSH_PORT', '22'))
        bandwidth = int(environment.get('SOURCE_RSYNC_BWLIMIT_KB', '0'))
    except ValueError:
        raise CloneError('SSH port and rsync bandwidth must be integers.')
    if not 1 <= port <= 65535 or bandwidth < 0:
        raise CloneError('Invalid SSH port or rsync bandwidth.')
    if not path.startswith('/') or path == '/' or any(c in path for c in '\x00\r\n'):
        raise CloneError('SOURCE_WAREXO_PATH must be an absolute application directory.')
    if not re.fullmatch(r'(?:/[A-Za-z0-9_.-]+)*[A-Za-z0-9_./-]*[A-Za-z0-9_.-]', php) or php.startswith('-'):
        raise CloneError('SOURCE_PHP_BIN must be one executable, without arguments.')
    return dict(host=host, user=user, port=port, path=path.rstrip('/'), php=php, bandwidth=bandwidth)


def source_database(raw):
    try:
        document = yaml.safe_load(raw)
        values = document['parameters']
    except (yaml.YAMLError, TypeError, KeyError):
        raise CloneError('Cannot parse the source app/config/parameters.yml as YAML data.')
    if not isinstance(values, dict) or values.get('database_driver', 'pdo_mysql') != 'pdo_mysql':
        raise CloneError('Only literal MySQL/MariaDB parameters are supported.')
    result = {}
    for key, default in [('host', 'localhost'), ('port', 3306), ('name', None), ('user', None), ('password', '')]:
        value = values.get('database_' + key, default)
        if value is None and key == 'port':
            value = 3306
        if not isinstance(value, (str, int)) or isinstance(value, bool):
            raise CloneError('Missing or unsupported database_' + key + ' in source configuration.')
        value = str(value)
        if '\x00' in value or re.search(r'%[^%]+%', value):
            raise CloneError('Source database parameters must contain resolved literal values.')
        result[key] = value
    if not result['user'] or not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_$.-]*', result['name']):
        raise CloneError('Invalid source database name or user.')
    if not result['port'].isdigit() or not 1 <= int(result['port']) <= 65535:
        raise CloneError('Invalid source database port.')
    socket = values.get('database_socket') or values.get('database_unix_socket')
    if socket is not None and (not isinstance(socket, str) or not socket.startswith('/') or '\x00' in socket):
        raise CloneError('Invalid source database socket.')
    result['socket'] = socket
    return result


def target_connection(environment):
    import pymysql
    return pymysql.connect(
        host=environment.get('DB_HOST', 'db'), port=int(environment.get('DB_PORT', '3306')),
        user=environment.get('DB_USER', 'root'), password=environment.get('DB_PASSWORD', ''),
        database=environment.get('DB_NAME', 'warexo'), charset='utf8mb4', connect_timeout=20,
    )


def assert_empty_database(environment):
    connection = target_connection(environment)
    try:
        with connection.cursor() as cursor:
            cursor.execute('SELECT COUNT(*) FROM information_schema.tables WHERE table_schema=DATABASE()')
            if cursor.fetchone()[0] != 0:
                raise CloneError('Target database is not empty; refusing to overwrite it.')
            cursor.execute('SELECT @@event_scheduler')
            if str(cursor.fetchone()[0]).upper() != 'DISABLED':
                raise CloneError('Target database requires event_scheduler=DISABLED.')
    finally:
        connection.close()


def make_ssh_config(directory, source, environment):
    key = directory / 'identity'
    known = directory / 'known_hosts'
    config = directory / 'ssh_config'
    key.write_text(secret(environment, 'SOURCE_SSH_PRIVATE_KEY'))
    known.write_text(secret(environment, 'SOURCE_SSH_KNOWN_HOSTS'))
    config.write_text('\n'.join([
        'Host warexo-source', '  HostName ' + source['host'], '  User ' + source['user'],
        '  Port ' + str(source['port']), '  IdentityFile ' + str(key),
        '  UserKnownHostsFile ' + str(known), '  GlobalKnownHostsFile /dev/null',
        '  StrictHostKeyChecking yes', '  IdentitiesOnly yes', '  BatchMode yes',
        '  PasswordAuthentication no', '  KbdInteractiveAuthentication no',
        '  ForwardAgent no', '  RequestTTY no', '  ConnectTimeout 30',
        '  ServerAliveInterval 30', '  ServerAliveCountMax 3', '  LogLevel ERROR', '',
    ]))
    for file in (key, known, config):
        file.chmod(0o600)
    return ['ssh', '-F', str(config), 'warexo-source']


def remote(ssh, arguments, error_log, payload=None, output=None):
    # OpenSSH executes a remote shell: quote EACH argument, never interpolate source paths.
    command = ssh + [shlex.join(arguments)]
    result = subprocess.run(command, input=payload, stdout=output or subprocess.PIPE, stderr=error_log)
    if result.returncode:
        raise CloneError('Source read failed; inspect the protected clone-error.log.')
    return result.stdout


def helper_payload(source, connection, operation):
    settings = dict(root=source['path'], database=connection, operation=operation)
    encoded = base64.b64encode(json.dumps(settings).encode()).decode()
    helper = Path(__file__).with_name('source_database.php').read_text()
    return helper.replace('__WAREXO_CLONE_CONFIG__', encoded).encode()


def php_arguments(source):
    return [source['php'], '-d', 'auto_prepend_file=', '-d', 'auto_append_file=',
            '-d', 'opcache.enable_cli=0', '-d', 'opcache.preload=',
            '-d', 'display_errors=0', '-d', 'log_errors=0']


def copy_files(ssh, source, app, error_log):
    command = ['rsync', '--archive', '--no-owner', '--no-group', '--safe-links',
               '--protect-args', '--info=progress2,name0,stats1',
               '-e', shlex.join(ssh[:-1])]
    command += ['--exclude=' + pattern for pattern in EXCLUDES]
    if source['bandwidth']:
        command.append('--bwlimit=' + str(source['bandwidth']))
    command += ['--', 'warexo-source:' + source['path'] + '/', str(app) + '/']
    if subprocess.run(command, stderr=error_log).returncode:
        raise CloneError('File copy failed; inspect the protected clone-error.log.')
    for required in ('composer.json', 'composer.lock', 'app/console', 'vendor/autoload.php'):
        if not (app / required).is_file():
            raise CloneError('Source is missing a required Warexo application or vendor file.')
    # Defence in depth: no copied link may resolve outside the new code volume.
    for folder, dirs, files in os.walk(app, followlinks=False):
        for name in dirs + files:
            candidate = Path(folder) / name
            if candidate.is_symlink():
                resolved = candidate.resolve()
                if app not in resolved.parents or not resolved.exists():
                    raise CloneError('The source contains an unsafe or broken symbolic link.')


def normalize_definer(line):
    # Match only the DEFINER at a CREATE header or mysqldump's dedicated view-definer line.
    stripped = line.lstrip()
    if DEFINER_HEADER.match(stripped):
        match = DEFINER.search(stripped)
        if not match:
            raise CloneError('Unsupported DEFINER syntax in the source dump.')
        suffix = stripped[match.end():]
        if not re.match(rb'(?:\s+SQL SECURITY (?:DEFINER|INVOKER))?\s+(?:PROCEDURE|FUNCTION|TRIGGER|EVENT|VIEW)\b', suffix) \
                and not re.match(rb'\s*\*/\s*/\*![0-9]{5,6}\s+(?:PROCEDURE|FUNCTION|TRIGGER|EVENT|VIEW)\b', suffix) \
                and not re.match(rb'\s+SQL SECURITY (?:DEFINER|INVOKER)\s*\*/;?\s*$', suffix):
            raise CloneError('Unsupported DEFINER header in the source dump.')
        return DEFINER.sub(b'DEFINER=CURRENT_USER', line, count=1)
    return line


class DumpNormalizer:
    """Track SQL quotes across lines so routine string literals remain byte-identical."""
    def __init__(self):
        self.quote = None
        self.comment = False

    def feed(self, line):
        # mysqldump emits complete INSERT statements on a single escaped line. These
        # can contain large blobs and never contain object headers to rewrite.
        if not self.quote and not self.comment and line.startswith(b'INSERT INTO ') and line.rstrip().endswith(b';'):
            return line
        result = line if self.quote or self.comment else normalize_definer(line)
        index = 0
        while index < len(line):
            char = line[index:index + 1]
            pair = line[index:index + 2]
            if self.comment:
                if pair == b'*/':
                    self.comment = False
                    index += 2
                else:
                    index += 1
            elif self.quote:
                if char == b'\\':
                    index += 2
                elif char == self.quote:
                    if line[index + 1:index + 2] == self.quote:
                        index += 2
                    else:
                        self.quote = None
                        index += 1
                else:
                    index += 1
            elif char in (b"'", b'"', b'`'):
                self.quote = char
                index += 1
            elif char == b'#' or (pair == b'--' and line[index + 2:index + 3] in (b' ', b'\t', b'\r', b'\n', b'')):
                break
            elif pair == b'/*' and line[index + 2:index + 3] != b'!':
                self.comment = True
                index += 2
            else:
                index += 1
        return result


def restore_database(environment, dump, error_log):
    client = shutil.which('mariadb') or shutil.which('mysql')
    if not client:
        raise CloneError('The local MariaDB client is missing.')
    child_environment = dict(PATH=os.environ.get('PATH', '/usr/bin:/bin'), LANG='C.UTF-8',
                             MYSQL_PWD=environment.get('DB_PASSWORD', ''), MYSQL_TEST_LOGIN_FILE='/dev/null')
    command = [client, '--no-defaults', '--binary-mode', '--default-character-set=utf8mb4',
               '--host=' + environment.get('DB_HOST', 'db'), '--port=' + environment.get('DB_PORT', '3306'),
               '--user=' + environment.get('DB_USER', 'root'), '--database=' + environment.get('DB_NAME', 'warexo')]
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=error_log, env=child_environment)
    try:
        normalizer = DumpNormalizer()
        with dump.open('rb') as source:
            for line in source:
                process.stdin.write(normalizer.feed(line))
        process.stdin.close()
        if process.wait() != 0:
            raise CloneError('Database restore failed; the application remains blocked.')
    except BaseException:
        if process.stdin and not process.stdin.closed:
            process.stdin.close()
        if process.poll() is None:
            process.terminate()
        process.wait()
        raise


def verify_restored_tables(environment, expected):
    connection = target_connection(environment)
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) FROM information_schema.tables WHERE table_schema=DATABASE() AND TABLE_TYPE='BASE TABLE'")
            if int(cursor.fetchone()[0]) != int(expected) or int(expected) == 0:
                raise CloneError('Restored table count does not match the source snapshot.')
    finally:
        connection.close()


def write_json(path, value, mode=0o600):
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + '\n')
    temporary.chmod(mode)
    temporary.replace(path)


def verify_ready(environment, state):
    """Read-only plausibility check; never contacts the source or starts an import."""
    ready = state / READY
    try:
        record = json.loads(ready.read_text())
        manifest_path = state / 'clone-manifest.json'
        if record.get('format') != 1 or not manifest_path.is_file() \
                or record.get('manifest_sha256') != hashlib.sha256(manifest_path.read_bytes()).hexdigest():
            raise ValueError()
        manifest = json.loads(manifest_path.read_text())
        minimum_tables = int(manifest['source']['base_tables'])
        if minimum_tables < 1:
            raise ValueError()
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        raise CloneError('No valid completed clone marker and manifest are available; refusing to alter this instance.')
    connection = None
    try:
        connection = target_connection(environment)
        with connection.cursor() as cursor:
            cursor.execute('SELECT DATABASE()')
            database = cursor.fetchone()[0]
            if database != manifest.get('target_database', environment.get('DB_NAME', 'warexo')):
                raise CloneError('The target database name does not match the completed clone.')
            cursor.execute('SELECT @@event_scheduler')
            if str(cursor.fetchone()[0]).upper() != 'DISABLED':
                raise CloneError('The existing clone requires event_scheduler=DISABLED.')
            cursor.execute("SELECT TABLE_NAME FROM information_schema.tables WHERE table_schema=DATABASE() AND TABLE_TYPE='BASE TABLE'")
            tables = {str(row[0]).lower(): str(row[0]) for row in cursor.fetchall()}
            if len(tables) < minimum_tables or not {'users', 'client'}.issubset(tables):
                raise CloneError('The target database is missing clone tables; check that the original database volume is attached.')
            for required in ('users', 'client'):
                identifier = '`' + tables[required].replace('`', '``') + '`'
                cursor.execute('SELECT EXISTS(SELECT 1 FROM ' + identifier + ' LIMIT 1)')
                if not cursor.fetchone()[0]:
                    raise CloneError('The target database has no Warexo users or clients; the clone cannot start.')
    except CloneError:
        raise
    except Exception:
        raise CloneError('The existing clone database cannot be verified; check its volume, connection and read permissions.')
    finally:
        if connection is not None:
            connection.close()


def import_once(environment, app, state):
    ready = state / READY
    if ready.is_file():
        verify_ready(environment, state)
        log('Existing completed clone retained; no source connection or reimport.')
        return
    if (state / STARTED).exists():
        raise CloneError('An earlier import did not complete. Use new disposable code, state and database volumes; automatic retry would overwrite data.')
    if any(app.iterdir()):
        raise CloneError('Target code volume is not empty; refusing to overwrite it.')
    source = validate_source(environment)
    password = environment.get('CLONE_ADMIN_PASSWORD', '')
    if not 12 <= len(password.encode('utf-8')) <= 4096 or '\x00' in password:
        raise CloneError('CLONE_ADMIN_PASSWORD must contain 12 to 4096 bytes.')
    assert_empty_database(environment)
    with tempfile.TemporaryDirectory(prefix='warexo-source-') as temporary:
        ssh = make_ssh_config(Path(temporary), source, environment)
        error_path = state / 'clone-error.log'
        with error_path.open('ab') as error_log:
            error_path.chmod(0o600)
            log('Reading source configuration without starting Warexo.')
            raw = remote(ssh, ['head', '-c', str(1024 * 1024 + 1), '--', source['path'] + '/app/config/parameters.yml'], error_log)
            if len(raw) > 1024 * 1024:
                raise CloneError('Source parameters file exceeds the allowed size.')
            database = source_database(raw)
            log('Checking source files, database engines and snapshot provenance.')
            metadata = json.loads(remote(ssh, php_arguments(source), error_log,
                                         helper_payload(source, database, 'inspect')))
            # Preflight failures remain retryable. From this first target mutation onwards,
            # an interrupted import must never overwrite a partially initialized instance.
            (state / STARTED).write_text(datetime.datetime.now(datetime.timezone.utc).isoformat() + '\n')
            log('Copying deployed application and vendor files.')
            copy_files(ssh, source, app, error_log)
            for relative, expected in metadata['files'].items():
                # This is a fixed allowlist returned by our helper, not arbitrary paths from source data.
                if relative not in ('composer.json', 'composer.lock', 'app/console', 'app/AppKernel.php',
                                    'vendor/autoload.php', 'vendor/composer/installed.json'):
                    raise CloneError('Unexpected source provenance path.')
                if hashlib.sha256((app / relative).read_bytes()).hexdigest() != expected:
                    raise CloneError('Copied application provenance does not match the source.')
            dump = state / 'source-database.sql'
            log('Streaming a transactional database dump over SSH.')
            with dump.open('wb') as output:
                dump.chmod(0o600)
                remote(ssh, php_arguments(source), error_log, helper_payload(source, database, 'dump'), output)
            if not dump.stat().st_size:
                raise CloneError('Source database dump is empty.')
            after = json.loads(remote(ssh, php_arguments(source), error_log,
                                      helper_payload(source, database, 'inspect')))
            if metadata['files'] != after['files']:
                raise CloneError('Selected source fingerprints changed during import; use a stable source snapshot.')
            log('Restoring into the empty local database.')
            restore_database(environment, dump, error_log)
            verify_restored_tables(environment, metadata['base_tables'])
            log('Neutralizing credentials and preparing local development access.')
            sanitizer = Path(__file__).with_name('sanitize.py')
            child_env = {key: value for key, value in environment.items() if not key.startswith('SOURCE_')}
            child_env.update(WAREXO_APP_DIR=str(app), WAREXO_STATE_DIR=str(state))
            if subprocess.run([sys.executable, str(sanitizer)], env=child_env, stderr=error_log).returncode:
                raise CloneError('Clone sanitization failed; the application remains blocked.')
            manifest = dict(format=1, created_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                            source=metadata, target_database=environment.get('DB_NAME', 'warexo'), excluded_paths=EXCLUDES,
                            fingerprint_scope='Selected bootstrap, lock and vendor metadata files; not a complete source-tree hash.',
                            consistency='Transactional database snapshot; files and database are not an atomic combined snapshot.',
                            definer_policy='mysqldump DDL definers mapped to local importing user')
            manifest_path = state / 'clone-manifest.json'
            write_json(manifest_path, manifest)
            dump.unlink()
            write_json(ready, dict(format=1, manifest_sha256=hashlib.sha256(manifest_path.read_bytes()).hexdigest()), 0o644)
            log('Clone ready. Normal restarts preserve the imported database and working files.')


def main(arguments=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--verify-ready', action='store_true', help='Only verify an existing clone and its local database; never import.')
    options = parser.parse_args(arguments)
    os.umask(0o077)
    environment = dict(os.environ)
    app = Path(environment.get('WAREXO_APP_DIR', '/var/www/html'))
    state = Path(environment.get('WAREXO_STATE_DIR', '/var/lib/warexo'))
    try:
        if not app.is_absolute() or not state.is_absolute() or app.is_symlink() or state.is_symlink():
            raise CloneError('Application and state paths must be absolute, non-symlink directories.')
        if options.verify_ready:
            if not state.is_dir():
                raise CloneError('No completed clone state directory is available.')
            verify_ready(environment, state)
            log('Existing clone and local database verified; no source contact.')
            return 0
        app.mkdir(parents=True, exist_ok=True)
        state.mkdir(parents=True, exist_ok=True)
        app, state = app.resolve(), state.resolve()
        if app == Path('/') or state == Path('/') or app == state or app in state.parents or state in app.parents:
            raise CloneError('Application and state directories must be distinct and non-nested.')
        with (state / 'clone.lock').open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise CloneError('Another import owns these volumes.')
            import_once(environment, app, state)
        return 0
    except Exception as error:
        # Exceptions from clients may contain private connection data. Only own messages are public.
        message = str(error) if isinstance(error, CloneError) else 'Import failed; inspect the protected clone-error.log and runtime configuration.'
        log('ERROR: ' + message)
        return 1


if __name__ == '__main__':
    sys.exit(main())
