import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest import mock


SPEC = importlib.util.spec_from_file_location("warexo_sanitize", Path(__file__).resolve().parents[1] / "clone/sanitize.py")
sanitizer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sanitizer)


def column(table, name, nullable="YES", kind="varchar", length=255):
    return {"TABLE_NAME": table, "COLUMN_NAME": name, "IS_NULLABLE": nullable,
            "DATA_TYPE": kind, "CHARACTER_MAXIMUM_LENGTH": length}


class FakeCursor:
    def __init__(self, schema, remaining=0):
        self.schema, self.remaining, self.calls, self.rowcount = schema, remaining, [], 3

    def execute(self, sql, values=()):
        self.calls.append((sql, values))

    def fetchall(self):
        return self.schema

    def fetchone(self):
        return {"n": self.remaining}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class SanitizerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.app = self.root / "app-root"
        (self.app / "app/config").mkdir(parents=True)
        (self.app / "web/uploads").mkdir(parents=True)
        (self.app / "app/config/security.yml").write_text(
            "security:\n  encoders:\n    FOS\\UserBundle\\Model\\UserInterface: sha512\n", encoding="utf-8")

    def test_sha512_reference_vector_and_source_defaults(self):
        self.assertEqual(sanitizer.encoder_options(self.app), (5000, True))
        self.assertEqual(sanitizer.encode_password("clone-test-password", "test-salt"),
                         "xj1bsNT/Q0gUkLGROdYPn8++lTsrNAgP+Ziw33u8kAYqZpj+uPtgn00Yi31b0zHr/txKDPPvDYtvc4s0R7pN9w==")
        self.assertEqual(sanitizer.encode_password("password", "", 1, False),
                         "b109f3bbbc244eb82441917ed06d618b9008dd09b3befd1b5e07394c706a8bb9"
                         "80b1d7785e5976ec049b46df5f1326af5a2ea6d103fd07c95385ffab0cacbc86")

    def test_encoder_override_is_respected(self):
        (self.app / "app/config/security.yml").write_text(
            "security:\n  encoders:\n    FOS\\UserBundle\\Model\\UserInterface: sha512\n"
            "    Aggrosoft\\WAWIBundle\\Entity\\User:\n      algorithm: sha512\n"
            "      iterations: 2\n      encode_as_base64: false\n", encoding="utf-8")
        self.assertEqual(sanitizer.encoder_options(self.app), (2, False))

    def test_unsupported_encoder_fails_before_database_access(self):
        (self.app / "app/config/security.yml").write_text(
            "security:\n  encoders:\n    FOS\\UserBundle\\Model\\UserInterface: bcrypt\n", encoding="utf-8")
        connection = mock.Mock()
        with self.assertRaises(sanitizer.SanitizeError):
            sanitizer.sanitize(connection, self.app, self.root / "state", "", "long-test-password")
        connection.cursor.assert_not_called()
        connection.commit.assert_not_called()

    def test_oversize_utf8_password_is_rejected_before_database_access(self):
        connection = mock.Mock()
        with self.assertRaises(sanitizer.SanitizeError):
            sanitizer.sanitize(connection, self.app, self.root / "state", "", "ä" * 3000)
        connection.cursor.assert_not_called()

    def test_selects_enabled_superadmin_then_lowest_id(self):
        rows = [
            {"id": 1, "username": "disabled", "enabled": 0, "roles": '["ROLE_SUPER_ADMIN"]'},
            {"id": 2, "username": "administrator", "enabled": 1, "roles": 'a:1:{i:0;s:10:"ROLE_ADMIN";}'},
            {"id": 4, "username": "second", "enabled": 1, "roles": '["ROLE_SUPER_ADMIN"]'},
            {"id": 3, "username": "first", "enabled": 1, "roles": 'a:1:{i:0;s:16:"ROLE_SUPER_ADMIN";}'},
        ]
        self.assertEqual(sanitizer.choose_user(rows, "")["username"], "first")
        self.assertEqual(sanitizer.choose_user(rows, "administrator")["id"], 2)
        self.assertEqual(sanitizer.choose_user(rows, "disabled")["id"], 1)

    def test_no_admin_or_unknown_requested_account_is_fatal(self):
        rows = [{"id": 1, "username": "ordinary", "enabled": 1, "roles": '["ROLE_ADMINISTRATOR"]'}]
        for requested in ("", "missing"):
            with self.assertRaises(sanitizer.SanitizeError):
                sanitizer.choose_user(rows, requested)

    def test_plugin_policy_does_not_classify_business_values_as_credentials(self):
        for name, kind in (("api_key", "text"), ("anything", "password"), ("amazon_awskey", "text"), ("oauthSecret", "text")):
            self.assertTrue(sanitizer.credential_setting(name, kind))
        for name in ("price", "sku", "inventory_threshold", "default_currency"):
            self.assertFalse(sanitizer.credential_setting(name, "text"))

    def test_identifier_injection_is_rejected(self):
        self.assertEqual(sanitizer.identifier("clientvalue"), "`clientvalue`")
        for name in ("x` SET password='' --", "a.b", "", "a\n"):
            with self.assertRaises(sanitizer.SanitizeError):
                sanitizer.identifier(name)

    def test_schema_cleanup_only_targets_present_credential_columns(self):
        cursor = FakeCursor([column("client", "shopPassword"), column("client", "title")])
        policy = sanitizer.DatabasePolicy(cursor)
        policy.clear("client", "shopPassword")
        policy.clear("client", "smtpHost")
        updates = [(sql, args) for sql, args in cursor.calls if sql.startswith("UPDATE")]
        self.assertEqual(updates, [("UPDATE `client` SET `shopPassword`=%s", (None,))])
        self.assertEqual(policy.skipped, ["client.smtpHost"])

    def test_credential_recreated_by_trigger_fails_verification(self):
        cursor = FakeCursor([column("client", "shopPassword")], remaining=1)
        with self.assertRaises(sanitizer.SanitizeError):
            sanitizer.DatabasePolicy(cursor).clear("client", "shopPassword")

    def test_missing_core_schema_fails_before_changes(self):
        cursor = FakeCursor([column("client", "id")])
        connection = mock.Mock()
        connection.cursor.return_value = cursor
        with self.assertRaises(sanitizer.SanitizeError):
            sanitizer.sanitize(connection, self.app, self.root / "state", "", "long-test-password")
        self.assertFalse(any(sql.startswith("UPDATE") for sql, _ in cursor.calls))
        connection.commit.assert_not_called()

    def test_secret_cleanup_preserves_business_files_and_unlinks_symlink_itself(self):
        outside = self.root / "outside-secret"
        outside.write_text("keep")
        (self.app / "app/config/parameters.yml").symlink_to(outside)
        (self.app / "web/install.php").write_text("installer")
        (self.app / "web/restore-old.php").write_text("restore")
        (self.app / "web/uploads/invoice.pdf").write_text("document")
        removed = sanitizer.remove_source_files(self.app)
        self.assertIn("app/config/parameters.yml", removed)
        self.assertIn("web/restore-old.php", removed)
        self.assertEqual(outside.read_text(), "keep")
        self.assertTrue((self.app / "web/uploads/invoice.pdf").exists())
        self.assertTrue((self.app / "app/config/security.yml").exists())

    def test_security_configuration_outside_app_is_rejected(self):
        original = self.app / "app/config/security.yml"
        outside = self.root / "outside-security.yml"
        original.rename(outside)
        original.symlink_to(outside)
        with self.assertRaises(sanitizer.SanitizeError):
            sanitizer.encoder_options(self.app)


if __name__ == "__main__":
    unittest.main()
