"""Only synthetic fixtures: ensure porting writes cannot silently change baseline."""
import json
import os
from pathlib import Path

import pymysql
import yaml

ref, app = Path('/var/www/html'), Path('/workspace/warexo-port')
# Coolify v4 injects the shared .env; explicit empty service values must win.
assert not any(key.startswith('SOURCE_') and value for key, value in os.environ.items())
assert 'DB_ADMIN_PASSWORD' not in os.environ
proxy_keys = [key for key in ('http_proxy', 'https_proxy', 'HTTP_PROXY', 'HTTPS_PROXY') if os.environ.get(key)]
assert not proxy_keys, 'Unintended nonempty proxy settings: ' + ', '.join(proxy_keys)
assert (app / 'vendor/custom-patch.txt').read_bytes() == (ref / 'vendor/custom-patch.txt').read_bytes()
assert (app / 'web/uploads/customer.txt').read_bytes() == (ref / 'web/uploads/customer.txt').read_bytes()
assert (app / 'vendor/custom-patch.txt').stat().st_ino != (ref / 'vendor/custom-patch.txt').stat().st_ino
assert not (app / '.git').exists(), 'Never invent a Git history containing customer data.'
assert not (app / 'app/warmup-ran').exists(), 'Initializer executed Legacy application code.'
params = yaml.safe_load((app / 'app/config/parameters.yml').read_text())['parameters']
assert params['database_name'] == 'warexo_port'
assert params['database_user'] == 'warexo_port'
assert params['database_password'] == os.environ['PORTING_DB_PASSWORD']
assert params['secret'] == os.environ['PORTING_APP_SECRET']
assert params['mailer_host'] == 'mailpit'
db = pymysql.connect(host=os.environ['PORTING_DB_HOST'], user=os.environ['PORTING_DB_USER'],
                     password=os.environ['PORTING_DB_PASSWORD'], database=os.environ['PORTING_DB_NAME'])
with db.cursor() as cursor:
    cursor.execute('SELECT value FROM clone_fixture WHERE id=1')
    assert cursor.fetchone()[0] == 'unchanged business value'
    try:
        cursor.execute('SELECT value FROM warexo.clone_fixture WHERE id=1')
    except pymysql.MySQLError as exc:
        assert exc.args[0] in (1044, 1142)
    else:
        raise AssertionError('Porting user can read reference database.')
    try:
        cursor.execute('INSERT INTO warexoXport.guard VALUES (1)')
    except pymysql.MySQLError as exc:
        assert exc.args[0] in (1044, 1142)
    else:
        raise AssertionError('Porting database grant matches a sibling schema through an underscore wildcard.')
    cursor.execute("UPDATE clone_fixture SET value='retained porting edit' WHERE id=1")
db.commit()
db.close()
reference = pymysql.connect(host=os.environ['DB_HOST'], user=os.environ['DB_USER'],
                           password=os.environ['DB_PASSWORD'], database=os.environ['DB_NAME'])
with reference.cursor() as cursor:
    cursor.execute('SELECT value FROM clone_fixture WHERE id=1')
    assert cursor.fetchone()[0] == 'unchanged business value'
    try:
        cursor.execute('SELECT value FROM warexo_port.clone_fixture WHERE id=1')
    except pymysql.MySQLError as exc:
        assert exc.args[0] in (1044, 1142)
    else:
        raise AssertionError('Reference user can read porting database.')
reference.close()
(app / 'web/porting-edit.txt').write_text('retained porting file')
(app / 'web/porting-check.php').write_text('''<?php
class VersionProbe { public private(set) string $value = 'php84'; }
new VersionProbe();
echo json_encode([
    'version' => PHP_VERSION_ID,
    'child' => trim(shell_exec('php -r ' . escapeshellarg('echo PHP_VERSION;'))),
    'session' => ini_get('session.save_path'),
    'database' => getenv('DB_NAME'),
    'has_proxy' => (bool) (getenv('HTTP_PROXY') ?: getenv('HTTPS_PROXY')),
]);
''')
marker = json.loads(Path('/workspace/.warexo-porting-ready.json').read_text())
assert 'password' not in json.dumps(marker).lower()
print('Independent workspace, vendor data, credentials, grants and mutations verified.')
