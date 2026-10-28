import importlib.util
import os
from pathlib import Path
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("deps_proxy", ROOT / "porting/deps_proxy.py")
proxy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(proxy)


class ProxyTests(unittest.TestCase):
    def test_defaults_are_exact_and_limited(self):
        hosts = proxy.allowed_hosts(proxy.DEFAULT_HOSTS)
        self.assertIn("repo.packagist.org", hosts)
        self.assertIn("github.com", hosts)
        self.assertNotIn("api.openai.com", hosts)
        self.assertEqual(proxy.allowed_hosts("GITHUB.com github.com"), ["github.com"])

    def test_rejects_wildcards_ips_and_config_injection(self):
        for value in ("", "*.github.com", ".github.com", "https://github.com",
                      "github.com:443", "127.0.0.1", "[::1]", "-n", "internal",
                      "github.com\nhttp_access allow all", "git_host.example.com"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                proxy.allowed_hosts(value)

    def test_policy_is_deny_by_default_and_blocks_private_destinations(self):
        config = proxy.configuration(["github.com"])
        self.assertIn("http_access deny !CONNECT", config)
        self.assertIn("http_access deny !tls_port", config)
        self.assertIn("169.254.0.0/16", config)
        self.assertIn("fc00::/7", config)
        self.assertLess(config.index("http_access deny private_destinations"),
                        config.index("http_access allow CONNECT"))
        self.assertIn("http_access deny all", config)
        self.assertIn("access_log none", config)

    def test_wrapper_scopes_environment_to_child(self):
        env = dict(os.environ, WAREXO_DEPS_PROXY="http://deps-proxy:3128",
                   NO_PROXY="*", ALL_PROXY="http://wrong:99")
        result = subprocess.run(["bash", str(ROOT / "porting/bin/warexo-net"), "env"],
                                env=env, capture_output=True, text=True, check=True)
        values = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
        self.assertEqual(values["HTTPS_PROXY"], "http://deps-proxy:3128")
        self.assertEqual(values["NO_PROXY"], "localhost,127.0.0.1,::1")
        self.assertNotIn("ALL_PROXY", values)
        self.assertEqual(env["NO_PROXY"], "*")

    def test_wrapper_rejects_proxy_credentials_and_empty_command(self):
        for arguments, value in (([], "http://deps-proxy:3128"),
                                 (["true"], "http://secret@deps-proxy:3128")):
            result = subprocess.run(["bash", str(ROOT / "porting/bin/warexo-net"), *arguments],
                                    env=dict(os.environ, WAREXO_DEPS_PROXY=value),
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertNotIn("secret@", result.stderr)


if __name__ == "__main__":
    unittest.main()
