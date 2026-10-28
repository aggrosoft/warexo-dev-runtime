"""Exercise optional entrypoint/wrappers without PHP, Docker or application code."""
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]


class PortingRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.workspace = self.root / 'workspace'
        self.legacy = self.root / 'reference'
        self.local = self.root / 'local/bin'
        self.php84 = self.root / 'php84'
        self.apache = self.root / 'apache'
        self.config = self.root / 'porting'
        self.bin = self.root / 'bin'
        for path in (self.workspace, self.legacy, self.local, self.php84 / 'bin',
                     self.php84 / 'sbin', self.apache / 'sites-enabled',
                     self.apache / 'sites-available', self.config, self.bin):
            path.mkdir(parents=True, exist_ok=True)
        self.events = self.root / 'events'
        self.env = dict(os.environ, PATH=str(self.bin) + ':' + os.environ['PATH'],
                        EVENTS=str(self.events), WAREXO_PORTING_ENABLED='1',
                        WAREXO_BOOTSTRAP_MODE='clone')
        self.command(self.bin / 'python3', 'echo verify >> "$EVENTS"\nexit "${VERIFY_EXIT:-0}"\n')
        self.command(self.bin / 'a2enmod', 'echo modules >> "$EVENTS"\n')
        self.command(self.bin / 'chown', 'exit 0\n')
        self.command(self.local / 'warexo-entrypoint', 'echo legacy >> "$EVENTS"\nexit 0\n')
        self.command(self.php84 / 'sbin/php-fpm', 'echo fpm >> "$EVENTS"\nexec sleep 30\n')
        (self.config / 'apache84.conf').write_text('# isolated test vhost\n')
        self.entrypoint = self.render('porting/entrypoint.sh', self.bin / 'entrypoint')

    def command(self, path, content):
        path.write_text('#!/usr/bin/env bash\nset -eu\n' + content)
        path.chmod(0o755)
        return path

    def render(self, source, target):
        text = (ROOT / source).read_text()
        for original, replacement in (
            ('/opt/warexo/porting', self.config), ('/usr/local/bin', self.local),
            ('/opt/php84', self.php84), ('/etc/apache2', self.apache),
            ('/var/www/html', self.legacy), ('/workspace', self.workspace),
        ):
            text = text.replace(original, str(replacement))
        target.write_text(text)
        target.chmod(0o755)
        return target

    def calls(self):
        return self.events.read_text().splitlines() if self.events.exists() else []

    def run_entrypoint(self, **extra):
        return subprocess.run([str(self.entrypoint), 'application'],
                              env=dict(self.env, **extra), text=True,
                              capture_output=True, timeout=5)

    def ready(self):
        (self.workspace / '.warexo-porting-ready.json').write_text('{}')

    def test_disabled_is_legacy_only_and_removes_own_previous_vhost(self):
        vhost = self.apache / 'sites-enabled/warexo-porting.conf'
        vhost.write_text('old optional vhost')
        untouched = self.apache / 'sites-enabled/000-default.conf'
        untouched.write_text('legacy')
        result = self.run_entrypoint(WAREXO_PORTING_ENABLED='0')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls(), ['legacy'])
        self.assertFalse(vhost.exists())
        self.assertEqual(untouched.read_text(), 'legacy')

    def test_invalid_switch_and_fresh_mode_refuse_start(self):
        for extra in ({'WAREXO_PORTING_ENABLED': 'yes'}, {'WAREXO_BOOTSTRAP_MODE': 'fresh'}):
            with self.subTest(extra=extra):
                self.assertNotEqual(self.run_entrypoint(**extra).returncode, 0)
        self.assertEqual(self.calls(), [])

    def test_missing_or_invalid_workspace_never_starts_services(self):
        self.assertNotEqual(self.run_entrypoint().returncode, 0)
        self.assertEqual(self.calls(), [])
        self.ready()
        result = self.run_entrypoint(VERIFY_EXIT='23')
        self.assertEqual(result.returncode, 23)
        self.assertEqual(self.calls(), ['verify'])

    def test_child_failure_stops_other_service_and_propagates_exit(self):
        self.ready()
        self.command(self.local / 'warexo-entrypoint', 'echo legacy >> "$EVENTS"\nexec sleep 30\n')
        self.command(self.php84 / 'sbin/php-fpm', 'echo fpm >> "$EVENTS"\nsleep 0.1\nexit 31\n')
        result = self.run_entrypoint()
        self.assertEqual(result.returncode, 31, result.stderr)
        self.assertCountEqual(self.calls(), ['verify', 'modules', 'legacy', 'fpm'])
        self.assertTrue((self.apache / 'sites-enabled/warexo-porting.conf').is_symlink())

    def test_termination_stops_both_service_groups(self):
        self.ready()
        self.command(self.local / 'warexo-entrypoint', 'echo legacy >> "$EVENTS"\nexec sleep 30\n')
        process = subprocess.Popen([str(self.entrypoint), 'application'], env=self.env,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 3
            while not {'legacy', 'fpm'}.issubset(self.calls()) and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue({'legacy', 'fpm'}.issubset(self.calls()))
            process.terminate()
            _, stderr = process.communicate(timeout=3)
            self.assertEqual(process.returncode, 143, stderr)
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=3)

    def test_php84_has_own_ini_path_and_only_porting_db_environment(self):
        wrapper = self.render('porting/bin/php84', self.local / 'php84')
        self.command(self.php84 / 'bin/php',
                     'printf "scan=%s\\nphprc=%s\\ndb=%s\\npassword_file=%s\\nurl=%s\\nsecret=%s\\npath=%s\\n" '
                     '"$PHP_INI_SCAN_DIR" "${PHPRC-unset}" "${DB_NAME-unset}" '
                     '"${DB_PASSWORD_FILE-unset}" "${DATABASE_URL-unset}" "${WAREXO_SECRET-unset}" "$PATH"\n'
                     'printf "arg=%s\\n" "$@"\n')
        result = subprocess.run([str(wrapper), '-v'], env=dict(self.env,
                                PHPRC='/legacy.ini', PHP_INI_SCAN_DIR='/legacy.d',
                                DB_NAME='reference', DB_PASSWORD_FILE='/legacy-password',
                                DATABASE_URL='reference-url', WAREXO_SECRET='reference-secret',
                                PORTING_DB_NAME='porting', PORTING_APP_SECRET='porting-secret'),
                                capture_output=True, text=True, timeout=3)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('scan=' + str(self.php84 / 'etc/conf.d'), result.stdout)
        self.assertIn('phprc=unset', result.stdout)
        self.assertIn('db=porting', result.stdout)
        self.assertIn('password_file=unset', result.stdout)
        self.assertIn('url=unset', result.stdout)
        self.assertIn('secret=porting-secret', result.stdout)
        self.assertIn('path=' + str(self.php84 / 'bin') + ':', result.stdout)
        self.assertIn('arg=' + str(self.php84 / 'etc/php.ini'), result.stdout)

    def test_php74_explicitly_selects_existing_runtime(self):
        wrapper = self.render('porting/bin/php74', self.local / 'php74')
        self.command(self.local / 'php', 'printf "%s\\n" "$PHP_INI_SCAN_DIR" "${PHPRC-unset}" "$@"\n')
        result = subprocess.run([str(wrapper), '-v'], env=dict(self.env, PHPRC='/wrong.ini'),
                                capture_output=True, text=True, timeout=3)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), ['/usr/local/etc/php/conf.d', 'unset', '-c',
                                                      '/usr/local/etc/php/php.ini', '-v'])

    def test_composer_rejects_reference_working_directory_and_manifest(self):
        wrapper = self.render('porting/bin/composer84', self.local / 'composer84')
        self.command(self.local / 'php84', 'echo composer >> "$EVENTS"\n')
        for args, cwd, extra in (
            (['install'], self.legacy, {}),
            (['--working-dir=' + str(self.legacy), 'install'], self.workspace, {}),
            (['-d', str(self.legacy), 'install'], self.workspace, {}),
            (['install'], self.workspace, {'COMPOSER': str(self.legacy / 'composer.json')}),
        ):
            with self.subTest(args=args, cwd=cwd):
                result = subprocess.run([str(wrapper), *args], cwd=cwd,
                                        env=dict(self.env, **extra), text=True,
                                        capture_output=True, timeout=3)
                self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.calls(), [])

    def test_composer_uses_explicit_php84_and_own_home(self):
        wrapper = self.render('porting/bin/composer84', self.local / 'composer84')
        self.command(self.local / 'php84', 'printf "%s\\n" "$COMPOSER_HOME" "$COMPOSER_CACHE_DIR" "$@"\n')
        result = subprocess.run([str(wrapper), '--version'], cwd=self.legacy, env=self.env,
                                capture_output=True, text=True, timeout=3)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), [str(self.workspace / '.composer84'),
                                                      str(self.workspace / '.composer84/cache'),
                                                      str(self.php84 / 'share/composer.phar'), '--version'])


if __name__ == '__main__':
    unittest.main()
