#!/usr/bin/env python3
"""Prepare one independent porting copy of an already-sanitized LOCAL clone.

No source SSH access, application execution, Git initialization, or automatic reset.
"""

import argparse
import datetime
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import subprocess
import sys

import yaml


spec = importlib.util.spec_from_file_location('warexo_clone_import', Path(__file__).resolve().parents[1] / 'clone/import_source.py')
clone = importlib.util.module_from_spec(spec)
spec.loader.exec_module(clone)
READY = '.warexo-porting-ready.json'
STARTED = '.warexo-porting-started'
LOCK = '.warexo-porting.lock'
EXCLUDES = clone.EXCLUDES + ['/var/cache', '/var/log', '/var/sessions', '/app/sessions', '/tmp', '/sessions', 'parameters.yml*']


class PortingError(RuntimeError):
    pass


def log(message):
    print('[warexo-porting] ' + message, flush=True)


def read_secret(environment, name):
    value = Path(environment[name + '_FILE']).read_text().rstrip('\r\n') if environment.get(name + '_FILE') else environment.get(name, '')
    if '\x00' in value:
        raise PortingError('Invalid secret value in ' + name + '.')
    return value


def settings(environment):
    # Coolify v4 can inject source variables into every container. Ignore them:
    # this initializer only reads the local clone and its separate database.
    result = dict(host=environment.get('PORTING_DB_HOST', 'db'), port=environment.get('PORTING_DB_PORT', '3306'),
                  name=environment.get('PORTING_DB_NAME', 'warexo_port'), user=environment.get('PORTING_DB_USER', 'warexo_port'),
                  reference=environment.get('REFERENCE_DB_NAME', 'warexo'), password=read_secret(environment, 'PORTING_DB_PASSWORD'))
    for key in ('name', 'user', 'reference'):
        if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,31}', result[key]):
            raise PortingError('Database and user names must be simple identifiers of at most 32 characters.')
    forbidden = {'mysql', 'information_schema', 'performance_schema', 'sys', 'root'}
    if result['name'].lower() in forbidden or result['user'].lower() in forbidden \
            or result['name'].lower() == result['reference'].lower() or result['user'].lower() == result['reference'].lower():
        raise PortingError('The porting database and user must be separate from the reference and system accounts.')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', result['host']):
        raise PortingError('PORTING_DB_HOST must be a local database service hostname.')
    try:
        result['port'] = int(result['port'])
    except ValueError:
        raise PortingError('PORTING_DB_PORT must be a port number.')
    if not 1 <= result['port'] <= 65535 or not 12 <= len(result['password'].encode()) <= 4096:
        raise PortingError('A valid database port and PORTING_DB_PASSWORD of at least 12 bytes are required.')
    return result


def paths(environment):
    values = tuple(Path(environment.get(key, default)) for key, default in (
        ('PORTING_REFERENCE_DIR', '/reference'), ('PORTING_REFERENCE_STATE_DIR', '/reference-state'),
        ('PORTING_WORKSPACE_DIR', '/workspace')))
    for path in values:
        if not path.is_absolute() or path == Path('/') or path.resolve() != path or (path.exists() and not path.is_dir()):
            raise PortingError('Workspace and reference paths must be absolute non-symlink directories, never filesystem roots.')
    if any(a == b or a in b.parents or b in a.parents for index, a in enumerate(values) for b in values[index + 1:]):
        raise PortingError('Reference and workspace directories must be separate and non-nested.')
    return values


def db_environment(config, admin=False, reference=False, environment=None):
    return dict(DB_HOST=config['host'], DB_PORT=str(config['port']),
                DB_NAME=config['reference'] if reference else config['name'],
                DB_USER='root' if admin else config['user'],
                DB_PASSWORD=read_secret(environment or {}, 'DB_ADMIN_PASSWORD') if admin else config['password'])


def identity(config):
    return {key: config[key] for key in ('host', 'port', 'name', 'user')}


def verify_ready(config, workspace):
    """Read-only plausibility check of the existing worktree and its scoped DB."""
    try:
        marker = workspace / READY
        if marker.is_symlink():
            raise ValueError()
        record = json.loads(marker.read_text())
        app = workspace / 'warexo-port'
        if record.get('format') != 1 or record.get('database') != identity(config) \
                or record.get('app_dir') != str(app) or int(record['minimum_tables']) < 1:
            raise ValueError()
        # The port may legitimately move app/console, replace parameters.yml, or
        # reinstall vendor. Readiness must not force the old Symfony layout.
        if not app.is_dir() or app.is_symlink() or app.resolve() != app:
            raise ValueError()
        for relative in ('composer.json',):
            candidate = app / relative
            if not candidate.is_file() or app not in candidate.resolve().parents:
                raise ValueError()
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        raise PortingError('No matching completed porting workspace exists; refusing to recopy or reimport.')
    connection = None
    try:
        connection = clone.target_connection(db_environment(config))
        with connection.cursor() as cursor:
            cursor.execute('SELECT DATABASE(), @@event_scheduler')
            database, scheduler = cursor.fetchone()
            if database != config['name'] or str(scheduler).upper() != 'DISABLED':
                raise PortingError('The porting database identity or disabled event scheduler does not match.')
            cursor.execute("SELECT TABLE_NAME FROM information_schema.tables WHERE table_schema=DATABASE() AND TABLE_TYPE='BASE TABLE'")
            tables = {str(row[0]).lower() for row in cursor.fetchall()}
            if len(tables) < record['minimum_tables'] or not {'users', 'client'}.issubset(tables):
                raise PortingError('The porting database is missing its copied tables; no automatic restore will occur.')
    except PortingError:
        raise
    except Exception:
        raise PortingError('Cannot verify the existing porting database with its scoped credentials; nothing was reimported.')
    finally:
        if connection is not None:
            connection.close()
    return record


def assert_available(connection, config):
    with connection.cursor() as cursor:
        cursor.execute('SELECT @@event_scheduler')
        if str(cursor.fetchone()[0]).upper() != 'DISABLED':
            raise PortingError('The local database requires event_scheduler=DISABLED.')
        cursor.execute('SELECT COUNT(*) FROM information_schema.schemata WHERE SCHEMA_NAME=%s', (config['name'],))
        if cursor.fetchone()[0]:
            raise PortingError('The target database already exists; use a fresh porting database, never overwrite it.')
        cursor.execute('SELECT COUNT(*) FROM mysql.user WHERE User=%s', (config['user'],))
        if cursor.fetchone()[0]:
            raise PortingError('The target database user already exists; refusing to change its password or permissions.')
        cursor.execute("SELECT COUNT(*), SUM(ENGINE <> 'InnoDB') FROM information_schema.tables WHERE TABLE_SCHEMA=%s AND TABLE_TYPE='BASE TABLE'", (config['reference'],))
        count, unsupported = cursor.fetchone()
        if not count or unsupported:
            raise PortingError('The reference must contain InnoDB tables for a transactional local snapshot.')
        return int(count)


def create_database(connection, config):
    # Identifiers were strictly validated; data values always remain parameters.
    # Database-level GRANT names are wildcard patterns even inside backticks.
    grant_database = config['name'].replace('_', r'\_')
    with connection.cursor() as cursor:
        cursor.execute('CREATE DATABASE `' + config['name'] + '` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci')
        cursor.execute('CREATE USER %s@%s IDENTIFIED BY %s', (config['user'], '%', config['password']))
        cursor.execute('GRANT ALL PRIVILEGES ON `' + grant_database + '`.* TO %s@%s', (config['user'], '%'))


def assert_local_symlinks(app):
    for folder, directories, files in os.walk(app, followlinks=False):
        for name in directories + files:
            candidate = Path(folder) / name
            if candidate.is_symlink() and (app not in candidate.resolve().parents or not candidate.resolve().exists()):
                raise PortingError('Copied code contains an unsafe or broken symlink.')


def copy_files(reference, app, error_log):
    assert_local_symlinks(reference)
    app.mkdir()
    command = ['rsync', '--archive', '--no-owner', '--no-group', '--safe-links']
    command += ['--exclude=' + item for item in EXCLUDES]
    command += ['--', str(reference) + '/', str(app) + '/']
    if subprocess.run(command, stdout=subprocess.DEVNULL, stderr=error_log).returncode:
        raise PortingError('Local file copy failed; inspect the protected porting error log.')
    assert_local_symlinks(app)
    for required in ('composer.json', 'composer.lock', 'app/console', 'vendor/autoload.php'):
        if not (app / required).is_file():
            raise PortingError('The reference is missing required deployed Warexo files.')


def dump_database(config, environment, dump, error_log):
    client = shutil.which('mariadb-dump') or shutil.which('mysqldump')
    if not client:
        raise PortingError('The local database dump client is missing.')
    command = [client, '--no-defaults', '--host=' + config['host'], '--port=' + str(config['port']), '--user=root',
               '--single-transaction', '--quick', '--skip-lock-tables', '--no-tablespaces', '--hex-blob',
               '--routines', '--triggers', '--skip-events', '--default-character-set=utf8mb4', config['reference']]
    child_env = dict(PATH=os.environ.get('PATH', '/usr/bin:/bin'), LANG='C.UTF-8',
                     MYSQL_PWD=read_secret(environment, 'DB_ADMIN_PASSWORD'), MYSQL_TEST_LOGIN_FILE='/dev/null')
    with dump.open('xb') as output:
        dump.chmod(0o600)
        if subprocess.run(command, stdout=output, stderr=error_log, env=child_env).returncode or not dump.stat().st_size:
            raise PortingError('The local reference database snapshot failed; inspect the protected porting error log.')


def parameter_values(config, environment):
    try:
        cookie_lifetime = int(environment.get('WAREXO_COOKIE_LIFETIME', '86400'))
        session_lifetime = int(environment.get('WAREXO_GC_MAX_LIFETIME', '1440'))
        secure = environment.get('WAREXO_COOKIE_SECURE', 'true').lower()
        if cookie_lifetime < 0 or session_lifetime < 0 or secure not in ('true', 'false', '1', '0'):
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise PortingError('Invalid local cookie/session settings.')
    values = dict(database_driver='pdo_mysql', database_host=config['host'], database_port=config['port'],
                  database_name=config['name'], database_user=config['user'], database_password=config['password'],
                  database_path=None, mailer_transport='smtp', mailer_host='mailpit', mailer_user='', mailer_password='',
                  locale=environment.get('WAREXO_LOCALE', 'de_DE'), secret=read_secret(environment, 'PORTING_APP_SECRET'),
                  license=environment.get('WAREXO_LICENSE', ''), cookielifetime=cookie_lifetime,
                  gcmaxlifetime=session_lifetime, cookiesecure=secure in ('true', '1'))
    return values


def write_parameters(app, config, environment):
    values = parameter_values(config, environment)
    filename = app / 'app/config/parameters.yml'
    filename.parent.mkdir(parents=True, exist_ok=True)
    filename.write_text(yaml.safe_dump({'parameters': values}, sort_keys=False, allow_unicode=True))
    filename.chmod(0o640)


def prepare_permissions(workspace, app):
    account = pwd.getpwnam('developer')
    for relative in ('repos', 'worktrees', '.composer84', '.runtime/php84/sessions', '.runtime/php84/tmp', '.runtime/php84/cache', '.runtime/php84/logs'):
        (workspace / relative).mkdir(parents=True, exist_ok=True)
    for top in (app, workspace / 'repos', workspace / 'worktrees', workspace / '.composer84', workspace / '.runtime'):
        for folder, directories, files in os.walk(top, followlinks=False):
            os.chown(folder, account.pw_uid, account.pw_gid)
            for name in directories + files:
                os.chown(Path(folder) / name, account.pw_uid, account.pw_gid, follow_symlinks=False)
    # Existing rsync modes remain intact; newly created private dirs must be traversable by the app user.
    workspace.chmod(0o755)


def initialize(config, environment, reference, state, workspace):
    if (workspace / READY).exists():
        verify_ready(config, workspace)
        log('Existing porting workspace retained; no copy, password reset, or database import.')
        return
    if (workspace / STARTED).exists() or (workspace / STARTED).is_symlink():
        raise PortingError('An earlier initialization was interrupted. Inspect it; automatic retry would overwrite data.')
    if any(item.name != LOCK for item in workspace.iterdir()):
        raise PortingError('The workspace is not empty; refusing to overwrite existing files.')
    if not read_secret(environment, 'DB_ADMIN_PASSWORD') or len(read_secret(environment, 'PORTING_APP_SECRET').encode()) < 32:
        raise PortingError('New initialization needs DB_ADMIN_PASSWORD and a PORTING_APP_SECRET of at least 32 bytes.')
    parameter_values(config, environment)  # Validate settings before making durable changes.
    reference_env = db_environment(config, admin=True, reference=True, environment=environment)
    clone.verify_ready(reference_env, state)
    manifest_hash = hashlib.sha256((state / 'clone-manifest.json').read_bytes()).hexdigest()
    connection = clone.target_connection(reference_env)
    try:
        expected = assert_available(connection, config)
        (workspace / STARTED).write_text(datetime.datetime.now(datetime.timezone.utc).isoformat() + '\n')
        error_file = workspace / '.warexo-porting-error.log'
        with error_file.open('xb') as error_log:
            error_file.chmod(0o600)
            app = workspace / 'warexo-port'
            log('Copying local deployed files into a separate workspace; no application code is executed.')
            copy_files(reference, app, error_log)
            dump = workspace / '.warexo-porting-database.sql'
            dump_database(config, environment, dump, error_log)
            create_database(connection, config)
            clone.restore_database(db_environment(config), dump, error_log)
            clone.verify_restored_tables(db_environment(config), expected)
            write_parameters(app, config, environment)
            prepare_permissions(workspace, app)
            record = dict(format=1, app_dir=str(app), database=identity(config), minimum_tables=expected,
                          reference_manifest_sha256=manifest_hash, created_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                          consistency='Transactional local DB snapshot; files and database are not one atomic snapshot.')
            dump.unlink()
            clone.write_json(workspace / READY, record, 0o644)
        log('Porting workspace ready. Reference unchanged; application compatibility with PHP 8.4 is not yet established.')
    finally:
        connection.close()


def main(arguments=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--verify-ready', action='store_true', help='Verify existing workspace and scoped database; never initialize.')
    mode.add_argument('--status', action='store_true', help='Show only safe local workspace status; no database access.')
    options = parser.parse_args(arguments)
    os.umask(0o077)
    environment = dict(os.environ)
    try:
        reference, state, workspace = paths(environment)
        if options.status:
            print(json.dumps(dict(workspace=str(workspace), app_dir=str(workspace / 'warexo-port'),
                                  ready=(workspace / READY).is_file(), started=(workspace / STARTED).is_file())))
            return 0
        config = settings(environment)
        if options.verify_ready:
            verify_ready(config, workspace)
            log('Existing porting workspace and local database verified; no mutations.')
            return 0
        workspace.mkdir(parents=True, exist_ok=True)
        if (workspace / LOCK).is_symlink():
            raise PortingError('Refusing a symlinked workspace lock.')
        with (workspace / LOCK).open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise PortingError('Another initializer owns this workspace.')
            initialize(config, environment, reference, state, workspace)
        return 0
    except Exception as error:
        message = str(error) if isinstance(error, (PortingError, clone.CloneError)) else 'Porting initialization failed; inspect its protected error log and local configuration.'
        log('ERROR: ' + message)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
