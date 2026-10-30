"""Keep the optional Coolify template aligned with its additive Compose overlay."""
import copy
from pathlib import Path
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]


def merge(base, extra, key=None):
    """Compose merge subset used here: mappings, unique lists, replace commands."""
    if isinstance(base, dict) and isinstance(extra, dict):
        result = copy.deepcopy(base)
        for name, value in extra.items():
            result[name] = merge(result[name], value, name) if name in result else copy.deepcopy(value)
        return result
    if isinstance(base, list) and isinstance(extra, list) and key not in ('command', 'entrypoint', 'test'):
        return base + [item for item in extra if item not in base]
    return copy.deepcopy(extra)


class PortingComposeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base = yaml.safe_load((ROOT / 'compose.clone.coolify.example.yaml').read_text())
        cls.extra = yaml.safe_load((ROOT / 'compose.porting.override.yaml').read_text())
        cls.full = yaml.safe_load((ROOT / 'compose.clone.porting.coolify.example.yaml').read_text())
        cls.services = cls.full['services']

    def test_standalone_template_is_the_overlay_applied_to_clone(self):
        self.assertEqual(self.full, merge(self.base, self.extra))

    def test_existing_templates_and_image_do_not_enable_porting(self):
        for filename in ('compose.clone.coolify.example.yaml', 'compose.coolify.example.yaml'):
            text = (ROOT / filename).read_text()
            self.assertNotIn('WAREXO_PORTING_ENABLED', text)
            self.assertNotIn('main-porting', text)
            self.assertNotIn('warexo_porting_workspace', text)
        self.assertNotIn('porting', (ROOT / 'Dockerfile').read_text().lower())

    def test_reference_database_and_volumes_are_preserved(self):
        for service_name in ('db', 'mailpit'):
            old = self.base['services'][service_name]
            new = copy.deepcopy(self.services[service_name])
            for key in self.source_keys():
                new['environment'].pop(key)
            self.assertEqual(new, old)
        for name, value in self.base['volumes'].items():
            self.assertEqual(self.full['volumes'][name], value)
        old = self.base['services']['warexo']
        new = self.services['warexo']
        for key, value in old['environment'].items():
            self.assertEqual(new['environment'][key], value)
        self.assertTrue(set(old['volumes']).issubset(new['volumes']))
        self.assertEqual(new['healthcheck'], old['healthcheck'])
        self.assertEqual(self.services['clone']['environment'], self.base['services']['clone']['environment'])

    def source_keys(self):
        return {key for key in self.base['services']['clone']['environment'] if key.startswith('SOURCE_')}

    def test_coolify_v4_env_file_injection_cannot_reach_non_clone_source_credentials(self):
        # Coolify v4 adds the project-wide .env as env_file to every service.
        # Compose environment entries, including explicit '', override env_file.
        source_keys = self.source_keys()
        self.assertTrue(source_keys)
        self.assertEqual(set(self.full['x-source-credentials-redacted']), source_keys)
        self.assertEqual(set(self.extra['x-source-credentials-redacted']), source_keys)
        for name, service in self.services.items():
            if name == 'clone':
                continue
            self.assertTrue(source_keys.issubset(service['environment']), name)
            for key in source_keys:
                self.assertEqual(service['environment'][key], '', name + '.' + key)

    def test_application_has_one_internal_network_and_no_admin_credentials(self):
        app = self.services['warexo']
        self.assertEqual(app['networks'], ['backend'])
        self.assertTrue(self.full['networks']['backend']['internal'])
        self.assertEqual(app['environment']['WAREXO_PORTING_ENABLED'], '1')
        for key, value in app['environment'].items():
            if key.startswith('SOURCE_'):
                self.assertEqual(value, '')
            if 'ADMIN_PASSWORD' in key:
                self.assertEqual(value, '')
            self.assertNotIn('DBROOT', str(value))
            self.assertNotIn(key.lower(), ('http_proxy', 'https_proxy', 'all_proxy'))
        for name, service in self.services.items():
            self.assertFalse(service.get('privileged'), name)
            self.assertNotEqual(service.get('network_mode'), 'host', name)
            self.assertNotIn('ports', service, name)
            for volume in service.get('volumes', []):
                self.assertNotIn('docker.sock', volume)

    def test_preview_environment_is_scoped_to_the_porting_application(self):
        app = self.services['warexo']['environment']
        self.assertEqual(app['PORTING_APP_ENV'], '${PORTING_APP_ENV:-dev}')
        self.assertEqual(app['PORTING_APP_DEBUG'], '${PORTING_APP_DEBUG:-0}')
        self.assertNotIn('PORTING_APP_ENV', self.base['services']['warexo']['environment'])
        self.assertNotIn('PORTING_APP_DEBUG', self.base['services']['warexo']['environment'])
        self.assertNotIn('PORTING_APP_ENV', self.services['porting-init']['environment'])
        self.assertNotIn('PORTING_APP_DEBUG', self.services['porting-init']['environment'])

    def test_selectable_aggrowawi_php84_vhost_keeps_next_as_default(self):
        app = self.services['warexo']['environment']
        self.assertEqual(app['WAREXO_PORTING_TARGET'], '${WAREXO_PORTING_TARGET:-next}')
        self.assertEqual(self.extra['services']['warexo']['environment']['WAREXO_PORTING_TARGET'],
                         app['WAREXO_PORTING_TARGET'])
        self.assertNotIn('WAREXO_PORTING_TARGET', self.base['services']['warexo']['environment'])
        entrypoint = (ROOT / 'porting/entrypoint.sh').read_text()
        self.assertIn('WAREXO_PORTING_TARGET:-next', entrypoint)
        self.assertIn('apache84-legacy.conf', entrypoint)
        self.assertIn('apache84.conf', entrypoint)

        next_vhost = (ROOT / 'porting/apache84.conf').read_text()
        legacy_vhost = (ROOT / 'porting/apache84-legacy.conf').read_text()
        self.assertIn('DocumentRoot /workspace/repos/next/backend/public', next_vhost)
        self.assertIn('DirectoryIndex index.php', next_vhost)
        self.assertIn('DocumentRoot /workspace/repos/aggrowawi-php84/web', legacy_vhost)
        self.assertIn('<Directory /workspace/repos/aggrowawi-php84/web>', legacy_vhost)
        self.assertIn('DirectoryIndex app.php', legacy_vhost)
        self.assertIn('FallbackResource /app.php', legacy_vhost)
        self.assertIn('updates', legacy_vhost)
        self.assertNotIn('/var/www/html', legacy_vhost)
        self.assertNotIn('backend/public', legacy_vhost)

    def test_initializer_only_reads_reference_and_gates_app_start(self):
        init = self.services['porting-init']
        self.assertEqual(init['networks'], ['backend'])
        self.assertEqual(init['entrypoint'], ['python3', '/opt/warexo/porting/workspace.py'])
        self.assertIsNone(init['command'])
        self.assertIsNone(self.services['clone']['command'])
        self.assertIn('warexo_code:/reference:ro', init['volumes'])
        self.assertIn('warexo_state:/reference-state:ro', init['volumes'])
        self.assertIn('warexo_porting_workspace:/workspace', init['volumes'])
        self.assertEqual(init['depends_on']['clone']['condition'], 'service_completed_successfully')
        self.assertEqual(self.services['warexo']['depends_on']['porting-init']['condition'], 'service_completed_successfully')
        self.assertFalse(any(key.startswith('SOURCE_') and value
                             for key, value in init['environment'].items()))

    def test_target_database_credentials_are_consistent_and_distinct(self):
        app = self.services['warexo']['environment']
        init = self.services['porting-init']['environment']
        for key in ('PORTING_DB_HOST', 'PORTING_DB_PORT', 'PORTING_DB_NAME', 'PORTING_DB_USER', 'PORTING_DB_PASSWORD', 'PORTING_APP_SECRET'):
            self.assertEqual(app[key], init[key])
        self.assertNotEqual(app['PORTING_DB_NAME'], app['DB_NAME'])
        self.assertNotEqual(app['PORTING_DB_USER'], app['DB_USER'])
        self.assertNotEqual(app['PORTING_DB_PASSWORD'], app['DB_PASSWORD'])
        self.assertEqual(init['REFERENCE_DB_NAME'], app['DB_NAME'])

    def test_gateways_have_separate_fixed_upstreams_and_same_guards(self):
        for name, port in (('gateway', 80), ('porting-gateway', 8084)):
            service = self.services[name]
            command = service['command'][0]
            self.assertIn('proxy_pass http://warexo:' + str(port) + ';', command)
            self.assertIn('(install|app_dev|restore)', command)
            self.assertIn('proxy_buffer_size 128k;', command)
            self.assertIn('proxy_buffers 4 128k;', command)
            self.assertEqual(set(service['networks']), {'default', 'backend'})
        self.assertIn('php84-ready', self.services['porting-gateway']['healthcheck']['test'][1])

    def test_dependency_proxy_does_not_mount_application_or_receive_secrets(self):
        proxy = self.services['deps-proxy']
        self.assertEqual(set(proxy['networks']), {'backend', 'porting_egress'})
        self.assertFalse(proxy.get('volumes'))
        self.assertFalse(proxy.get('ports'))
        self.assertEqual(set(proxy['environment']) - self.source_keys(), {'PORTING_ALLOWED_HOSTS'})
        hosts = proxy['environment']['PORTING_ALLOWED_HOSTS']
        self.assertNotIn('*', hosts)
        self.assertNotIn('openai', hosts)
        self.assertEqual(self.services['warexo']['environment']['WAREXO_DEPS_PROXY'], 'http://deps-proxy:3128')


if __name__ == '__main__':
    unittest.main()
