#!/usr/bin/env python3
"""Neutralize a disposable Warexo copy without loading copied application code."""
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sys

import yaml


class SanitizeError(RuntimeError):
    pass


TEXT_TYPES = {"char", "varchar", "text", "tinytext", "mediumtext", "longtext"}
CLEAR_COLUMNS = {
    "client": "shopUser shopPassword ftpServer ftpUserName ftpPassword smtpUserName smtpPassword smtpHost imapUserName imapPassword imapHost email cc bcc",
    "csvimport": "user password token",
    "csvexport": "token",
    "customer": "pass salt email paypalEMail emailForInvoice cc emailForContact emailForOrderConfirmation",
    "customercontact": "email",
    "customform": "googleRecaptchaSitekey googleRecaptchaSecret",
    "report": "token",
    "offer": "billingEMail",
    "mailjob": "cc",
    "vendororder": "updateToken",
    "vendororderdelivery": "secret",
    "usergoogleauthenticator": "secret",
}
TOKEN_COLUMNS = {
    "securetoken": "token", "storelogintoken": "token",
    "storeregistrationtoken": "token", "usertoken": "token",
    "productlicense": "licenseKey",
}
CREDENTIAL_PATTERN = r"(password|passwd|secret|token|api[_-]?key|private[_-]?key|license[_-]?key)"
CREDENTIAL_NAMES = ("key", "clientkey", "parentkey", "real_client_key", "amazon_awskey")


def identifier(value):
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        raise SanitizeError("Unsupported database identifier.")
    return "`" + value + "`"


def encoder_options(app):
    path = app / "app/config/security.yml"
    if not path.resolve().is_relative_to(app.resolve()) or path.stat().st_size > 1024 * 1024:
        raise SanitizeError("Unsafe security configuration path.")
    configuration = yaml.safe_load(path.read_text(encoding="utf-8"))
    encoders = configuration.get("security", {}).get("encoders", {})
    encoder = encoders.get("Aggrosoft\\WAWIBundle\\Entity\\User", encoders.get("FOS\\UserBundle\\Model\\UserInterface"))
    if encoder == "sha512":
        return 5000, True
    if not isinstance(encoder, dict) or encoder.get("algorithm") != "sha512":
        raise SanitizeError("Clone login requires the standard Warexo SHA512 encoder.")
    if set(encoder) - {"algorithm", "iterations", "encode_as_base64", "ignore_case"} or encoder.get("ignore_case", False):
        raise SanitizeError("Unsupported password encoder options.")
    iterations, as_base64 = encoder.get("iterations", 5000), encoder.get("encode_as_base64", True)
    if type(iterations) is not int or not 1 <= iterations <= 100000 or type(as_base64) is not bool:
        raise SanitizeError("Invalid password encoder options.")
    return iterations, as_base64


def encode_password(password, salt, iterations=5000, as_base64=True):
    # Symfony 2.3 MessageDigestPasswordEncoder: SHA512(raw{salt}), then stretch.
    salted = (password + ("{" + salt + "}" if salt else "")).encode("utf-8")
    digest = hashlib.sha512(salted).digest()
    for _ in range(1, iterations):
        digest = hashlib.sha512(digest + salted).digest()
    return base64.b64encode(digest).decode("ascii") if as_base64 else digest.hex()


def choose_user(rows, requested):
    if requested:
        matches = [row for row in rows if row["username"] == requested]
    else:
        def priority(row):
            roles = set(re.findall(r'"(ROLE_[A-Z0-9_]+)"', row.get("roles") or ""))
            return 2 if "ROLE_SUPER_ADMIN" in roles else 1 if "ROLE_ADMIN" in roles else 0
        matches = sorted((row for row in rows if row["enabled"] and priority(row)),
                         key=lambda row: (-priority(row), int(row["id"])))
    if not matches:
        raise SanitizeError("No suitable clone login account; set CLONE_ADMIN_USERNAME to an existing account.")
    return matches[0]


def credential_setting(name, field_type):
    return (field_type.lower() == "password" or name.lower() in CREDENTIAL_NAMES
            or bool(re.search(CREDENTIAL_PATTERN, name.lower())))


class DatabasePolicy:
    def __init__(self, cursor):
        self.cursor, self.tables, self.changes, self.skipped = cursor, {}, {}, []
        cursor.execute("SELECT TABLE_NAME, COLUMN_NAME, IS_NULLABLE, DATA_TYPE, CHARACTER_MAXIMUM_LENGTH "
                       "FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE()")
        for column in cursor.fetchall():
            table = column["TABLE_NAME"]
            entry = self.tables.setdefault(table.lower(), {"name": table, "columns": {}})
            if entry["name"] != table:
                raise SanitizeError("Ambiguous case-sensitive table names.")
            entry["columns"][column["COLUMN_NAME"].lower()] = column

    def columns(self, table, names, required=False):
        entry = self.tables.get(table.lower())
        missing = [name for name in names if not entry or name.lower() not in entry["columns"]]
        if missing:
            if required:
                raise SanitizeError("Required Warexo schema is missing: " + table + ".")
            self.skipped.extend(table + "." + name for name in missing)
            return None
        return [entry["columns"][name.lower()] for name in names]

    def table(self, table):
        return identifier(self.tables[table.lower()]["name"])

    def col(self, table, name):
        return identifier(self.columns(table, [name], required=True)[0]["COLUMN_NAME"])

    def execute(self, label, sql, values=()):
        self.cursor.execute(sql, values)
        self.changes[label] = self.cursor.rowcount

    def ensure_zero(self, sql, values=()):
        self.cursor.execute(sql, values)
        if int(next(iter(self.cursor.fetchone().values()))) != 0:
            raise SanitizeError("Credential neutralization verification failed.")

    def clear(self, table, name):
        found = self.columns(table, [name])
        if not found:
            return
        column = found[0]
        if column["DATA_TYPE"] not in TEXT_TYPES:
            raise SanitizeError("Unsupported credential column type: " + table + "." + name)
        value = None if column["IS_NULLABLE"] == "YES" else ""
        col, tab = self.col(table, name), self.table(table)
        self.execute(table + "." + name, "UPDATE " + tab + " SET " + col + "=%s", (value,))
        check = col + " IS NOT NULL" if value is None else col + "<>'' OR " + col + " IS NULL"
        self.ensure_zero("SELECT COUNT(*) AS n FROM " + tab + " WHERE " + check)

    def tokens(self, table, name):
        found = self.columns(table, [name])
        if not found:
            return
        self.columns(table, ["id"], required=True)
        if found[0]["DATA_TYPE"] not in TEXT_TYPES:
            raise SanitizeError("Unsupported token column type.")
        limit = min(found[0]["CHARACTER_MAXIMUM_LENGTH"] or 64, 64)
        seed = secrets.token_hex(32)
        expression = "LEFT(SHA2(CONCAT(%s,CAST(" + self.col(table, "id") + " AS CHAR)),256),%s)"
        tab, col = self.table(table), self.col(table, name)
        self.execute(table + "." + name, "UPDATE " + tab + " SET " + col + "=" + expression, (seed, limit))
        self.ensure_zero("SELECT COUNT(*) AS n FROM " + tab + " WHERE " + col + " IS NULL OR " + col + "<>" + expression, (seed, limit))

    def plugins(self):
        if not self.columns("clientvalue", ["value", "pluginsettings_id"]) or not self.columns("pluginsettings", ["id", "fieldType", "name"]):
            return
        value, name = self.col("clientvalue", "value"), self.col("pluginsettings", "name")
        joins = (self.table("clientvalue") + " cv JOIN " + self.table("pluginsettings") + " ps ON cv."
                 + self.col("clientvalue", "pluginsettings_id") + "=ps." + self.col("pluginsettings", "id"))
        condition = ("(LOWER(ps." + self.col("pluginsettings", "fieldType") + ")='password' OR LOWER(ps."
                     + name + ") REGEXP %s OR LOWER(ps." + name + ") IN (" + ",".join(["%s"] * len(CREDENTIAL_NAMES)) + "))")
        values = (CREDENTIAL_PATTERN,) + CREDENTIAL_NAMES
        self.execute("plugin_credentials", "UPDATE " + joins + " SET cv." + value + "='' WHERE " + condition, values)
        self.ensure_zero("SELECT COUNT(*) AS n FROM " + joins + " WHERE " + condition + " AND (cv." + value + "<>'' OR cv." + value + " IS NULL)", values)

    def users(self, requested, password, options):
        self.columns("users", ["id", "username", "enabled", "roles", "password", "salt"], required=True)
        self.cursor.execute("SELECT " + ",".join(self.col("users", key) + " AS " + identifier(key)
                                                  for key in ("id", "username", "enabled", "roles")) + " FROM " + self.table("users"))
        selected = choose_user(self.cursor.fetchall(), requested)
        self.tokens("users", "password")
        self.tokens("users", "salt")
        self.execute("users.disabled", "UPDATE " + self.table("users") + " SET " + self.col("users", "enabled") + "=0")
        for field in ("confirmation_token", "posPin"):
            if field == "posPin" and self.columns("users", [field]):
                self.execute("users.posPin", "UPDATE " + self.table("users") + " SET " + self.col("users", field) + "=NULL")
            elif field != "posPin":
                self.clear("users", field)
        for field in ("email", "email_canonical"):
            if self.columns("users", [field]):
                self.execute("users." + field, "UPDATE " + self.table("users") + " SET " + self.col("users", field)
                             + "=CONCAT('clone-user-'," + self.col("users", "id") + ",'@example.invalid')")
        salt = secrets.token_hex(16)
        encoded = encode_password(password, salt, *options)
        if (self.columns("users", ["password"], required=True)[0]["CHARACTER_MAXIMUM_LENGTH"] or 0) < len(encoded):
            raise SanitizeError("Password column cannot store the configured encoder result.")
        updates = {"password": encoded, "salt": salt, "enabled": 1}
        for field in ("locked", "expired", "credentials_expired"):
            if self.columns("users", [field]):
                updates[field] = 0
        for field in ("expires_at", "credentials_expire_at", "password_requested_at"):
            column = self.columns("users", [field])
            if column:
                if column[0]["IS_NULLABLE"] != "YES":
                    raise SanitizeError("Unsupported non-nullable account expiry field.")
                updates[field] = None
        self.execute("users.clone_login", "UPDATE " + self.table("users") + " SET "
                     + ",".join(self.col("users", key) + "=%s" for key in updates)
                     + " WHERE " + self.col("users", "id") + "=%s", tuple(updates.values()) + (selected["id"],))
        self.ensure_zero("SELECT COUNT(*) AS n FROM " + self.table("users") + " WHERE " + self.col("users", "id")
                         + "<>%s AND " + self.col("users", "enabled") + "<>0", (selected["id"],))
        self.cursor.execute("SELECT " + self.col("users", "password") + " AS password," + self.col("users", "salt")
                            + " AS salt FROM " + self.table("users") + " WHERE " + self.col("users", "id") + "=%s", (selected["id"],))
        actual = self.cursor.fetchone()
        if not actual or actual["password"] != encoded or actual["salt"] != salt:
            raise SanitizeError("Clone login verification failed.")
        # Keep memberships/permissions; remove only copied source-network restrictions.
        self.clear("usergroup", "allowedIps")
        return selected["username"]


def remove_source_files(app):
    names = ["app/config/parameters.yml", "web/install.php", "web/app_dev.php", "web/restore.php",
             ".env", ".env.local", ".env.local.php", ".env.prod", ".env.prod.local"]
    names += [str(path.relative_to(app)) for path in (app / "web").glob("restore*.php")]
    names += [str(path.relative_to(app)) for path in (app / "app/config").glob("parameters.yml.*")]
    removed = []
    for name in set(names):
        path = app / name
        if not path.parent.resolve().is_relative_to(app.resolve()):
            raise SanitizeError("Unsafe credential-file path.")
        if path.is_symlink() or path.is_file():
            path.unlink()
            removed.append(name)
        elif path.exists():
            raise SanitizeError("Unexpected directory at credential-file path.")
    return sorted(removed)


def sanitize(connection, app, state, username, password):
    if not isinstance(password, str) or not 12 <= len(password.encode("utf-8")) <= 4096 or "\0" in password:
        raise SanitizeError("CLONE_ADMIN_PASSWORD must contain 12 to 4096 characters.")
    options = encoder_options(app)
    with connection.cursor() as cursor:
        policy = DatabasePolicy(cursor)
        policy.columns("client", ["id"], required=True)
        login = policy.users(username, password, options)
        for table, names in CLEAR_COLUMNS.items():
            for name in names.split():
                policy.clear(table, name)
        for table, name in TOKEN_COLUMNS.items():
            policy.tokens(table, name)
        policy.plugins()
        if policy.columns("usergoogleauthenticator", ["active"]):
            policy.execute("two_factor_disabled", "UPDATE " + policy.table("usergoogleauthenticator") + " SET " + policy.col("usergoogleauthenticator", "active") + "=0")
        if policy.columns("mailjob", ["id", "email"]):
            policy.execute("mailjob.email", "UPDATE " + policy.table("mailjob") + " SET " + policy.col("mailjob", "email") + "=CONCAT('clone-mail-'," + policy.col("mailjob", "id") + ",'@example.invalid')")
    removed = remove_source_files(app)
    connection.commit()
    result = {"policy": "warexo-clone-v1", "login_username": login, "changes": policy.changes,
              "absent_optional_columns": sorted(set(policy.skipped)), "removed_files": removed,
              "fully_anonymized": False, "application_outbound_network_required": "blocked"}
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = state / "clone-sanitization.json.tmp"
    with temporary.open("x", encoding="utf-8") as output:
        os.chmod(temporary, 0o600)
        json.dump(result, output, indent=2)
        output.write("\n")
    temporary.replace(state / "clone-sanitization.json")
    return result


def main():
    import pymysql
    os.umask(0o077)
    connection = None
    try:
        connection = pymysql.connect(host=os.environ.get("DB_HOST", "db"), port=int(os.environ.get("DB_PORT", "3306")),
                                     database=os.environ.get("DB_NAME", "warexo"), user=os.environ.get("DB_USER", "root"),
                                     password=os.environ["DB_PASSWORD"], charset="utf8mb4", autocommit=False,
                                     cursorclass=pymysql.cursors.DictCursor, connect_timeout=15)
        sanitize(connection, Path(os.environ.get("WAREXO_APP_DIR", "/var/www/html")),
                 Path(os.environ.get("WAREXO_STATE_DIR", "/var/lib/warexo")),
                 os.environ.get("CLONE_ADMIN_USERNAME", ""), os.environ.get("CLONE_ADMIN_PASSWORD", ""))
        print("[warexo-clone] Credentials neutralized; clone login is recorded in protected clone-sanitization.json.")
    except Exception as error:
        if connection:
            connection.rollback()
        detail = str(error) if isinstance(error, SanitizeError) else "Check the schema, encoder and clone account settings."
        print("[warexo-clone] Sanitization failed. " + detail + " Application remains blocked.", file=sys.stderr)
        return 1
    finally:
        if connection:
            connection.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
