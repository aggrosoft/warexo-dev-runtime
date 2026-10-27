"""Exercise actual shell entrypoints with harmless commands and temporary paths."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.app, self.state, self.bin = (self.root / name for name in ('app', 'state', 'bin'))
        for path in (self.app, self.state, self.bin):
            path.mkdir()
        (self.app / '.git').mkdir()
        (self.app / 'vendor').mkdir()
        (self.app / 'app').mkdir()
        (self.app / 'vendor/autoload.php').touch()
        (self.app / 'app/console').touch()
        self.events = self.root / 'events'
        self.env = dict(os.environ, PATH=str(self.bin) + ':' + os.environ['PATH'], EVENTS=str(self.events))
        for name in ('setup-git-ssh', 'clone-warexo', 'fix-runtime-permissions', 'create-parameters', 'install-app', 'sudo', 'application'):
            self.command(name, 'echo ' + name + ' >> "$EVENTS"\nexit 0\n')
        self.command('python3', 'exit 0\n')
        self.command('sleep', 'exit 77\n')
        for original, name in (('docker/entrypoint.sh', 'entrypoint'), ('bin/start-clone', 'start-clone')):
            text = (ROOT / original).read_text().replace('/opt/warexo/bin', str(self.bin))
            text = text.replace('/var/lib/warexo', str(self.state)).replace('/var/www/html', str(self.app))
            self.command(name, text, shebang=False)

    def command(self, name, content, shebang=True):
        path = self.bin / name
        path.write_text(('#!/usr/bin/env bash\n' if shebang else '') + content)
        path.chmod(0o755)

    def run_entrypoint(self, mode):
        return subprocess.run([str(self.bin / 'entrypoint'), 'application'],
                              env=dict(self.env, WAREXO_BOOTSTRAP_MODE=mode), capture_output=True, text=True)

    def calls(self):
        return self.events.read_text().splitlines() if self.events.exists() else []

    def test_clone_without_completed_import_never_runs_application(self):
        result = self.run_entrypoint('clone')
        self.assertEqual(result.returncode, 77)
        self.assertEqual(self.calls(), [])
        self.assertFalse((self.state / 'clone-app-ready').exists())

    def test_failed_warmup_keeps_clone_stopped(self):
        (self.state / 'clone-ready').write_text('{}')
        self.command('sudo', 'exit 12\n')
        result = self.run_entrypoint('clone')
        self.assertEqual(result.returncode, 77)
        self.assertNotIn('application', self.calls())
        self.assertFalse((self.state / 'clone-app-ready').exists())
        self.assertFalse((self.state / 'health/ready.txt').exists())

    def test_missing_target_database_keeps_clone_stopped(self):
        (self.state / 'clone-ready').write_text('{}')
        self.command('python3', 'exit 1\n')
        result = self.run_entrypoint('clone')
        self.assertEqual(result.returncode, 77)
        self.assertEqual(self.calls(), [])
        self.assertFalse((self.state / 'clone-app-ready').exists())

    def test_clone_never_runs_git_or_composer_bootstrap(self):
        (self.state / 'clone-ready').write_text('{}')
        result = self.run_entrypoint('clone')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls(), ['fix-runtime-permissions', 'create-parameters', 'sudo', 'application'])
        self.assertEqual((self.state / 'health/ready.txt').read_text(), 'ready\n')

    def test_fresh_failed_install_does_not_write_success_marker(self):
        self.command('install-app', 'exit 19\n')
        result = self.run_entrypoint('fresh')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.state / 'runtime-initialized').exists())
        self.assertTrue((self.state / 'bootstrap-failed').exists())

    def test_unknown_mode_never_runs_application(self):
        self.assertNotEqual(self.run_entrypoint('unknown').returncode, 0)
        self.assertEqual(self.calls(), [])


if __name__ == '__main__':
    unittest.main()
