"""Assertions against a synthetic imported installation, run inside the application."""
import base64
import hashlib
import json
import os
from pathlib import Path

import pymysql
import yaml

app, state = Path('/var/www/html'), Path('/var/lib/warexo')
assert not any(name.startswith('SOURCE_') for name in os.environ)
assert (app / 'vendor/custom-patch.txt').read_text() == 'deployed vendor patch must survive\n'
assert (app / 'web/uploads/customer.txt').read_text() == 'synthetic uploaded document\n'
assert (app / 'app/warmup-ran').is_file()
assert not (app / 'web/install.php').exists()
assert not (state / 'source-database.sql').exists()
assert json.loads((state / 'clone-ready').read_text())['format'] == 1
events = json.loads((state / 'clone-manifest.json').read_text())['source']['events']
assert len(events) == 1 and events[0]['name'] == 'fixture_disabled_event'
event_sql = ' '.join(events[0]['create_statement'].split())
assert event_sql.startswith('CREATE DEFINER=`fixture_definer`@`localhost` EVENT `fixture_disabled_event`')
assert 'ON SCHEDULE EVERY 1 DAY' in event_sql and 'DISABLE' in event_sql
assert 'DO UPDATE clone_fixture SET value=value' in event_sql
parameters = yaml.safe_load((app / 'app/config/parameters.yml').read_text())['parameters']
assert parameters['database_host'] == 'db' and parameters['mailer_host'] == 'mailpit'
assert 'source-fixture-password' not in (app / 'app/config/parameters.yml').read_text()
db = pymysql.connect(host=os.environ['DB_HOST'], user=os.environ['DB_USER'],
                     password=os.environ['DB_PASSWORD'], database=os.environ['DB_NAME'])
with db.cursor() as cur:
    cur.execute('SELECT @@event_scheduler')
    assert cur.fetchone()[0] == 'DISABLED'
    cur.execute('SELECT shopUser,shopPassword,smtpHost,smtpUserName,smtpPassword,email FROM client')
    assert cur.fetchone() == (None,) * 6
    cur.execute('SELECT email FROM customer')
    assert cur.fetchone()[0] is None
    cur.execute('SELECT enabled FROM users WHERE id=2')
    assert cur.fetchone()[0] == 0
    cur.execute('SELECT password,salt,enabled,locked,expired,credentials_expired FROM users WHERE id=1')
    encoded, salt, *flags = cur.fetchone()
    # Independent comparison with the documented Symfony SHA512 stretching algorithm.
    message = ('clone-fixture-password{' + salt + '}').encode()
    digest = hashlib.sha512(message).digest()
    for _ in range(4999):
        digest = hashlib.sha512(digest + message).digest()
    assert encoded == base64.b64encode(digest).decode() and flags == [1, 0, 0, 0]
    cur.execute('SELECT value FROM clone_fixture WHERE id=1')
    assert cur.fetchone()[0] == 'unchanged business value'
    cur.execute('SELECT value FROM clone_fixture WHERE id=2')
    assert cur.fetchone()[0] == 'DEFINER=`data-user`@`data-host`'
    for catalog, schema_column, name_column, name in (
        ('TRIGGERS', 'TRIGGER_SCHEMA', 'TRIGGER_NAME', 'fixture_update'),
        ('ROUTINES', 'ROUTINE_SCHEMA', 'ROUTINE_NAME', 'fixture_definer_probe'),
    ):
        cur.execute('SELECT DEFINER FROM information_schema.' + catalog
                    + ' WHERE ' + schema_column + '=DATABASE() AND ' + name_column + '=%s', (name,))
        assert cur.fetchone()[0].startswith('root@'), 'Source-only DEFINER survived: ' + name
    cur.execute('CALL fixture_definer_probe()')
    assert cur.fetchone()[0] == 'first line\nCREATE DEFINER=`body-user`@`body-host` VIEW unchanged\nlast line'
    while cur.nextset():
        pass
    # Executes the restored trigger; an unresolved source-only definer would fail.
    cur.execute('UPDATE clone_fixture SET value=value WHERE id=1')
    # With event_scheduler=DISABLED MariaDB also refuses information_schema.EVENTS
    # reads. The host-side test inspects mysql.event with the test root account.
db.rollback()
db.close()
print('Imported application, vendor, credentials, login, routine/trigger, inert event evidence and unchanged SQL string data verified.')
