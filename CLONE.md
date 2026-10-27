# Clone an existing Warexo installation over SSH

Use `compose.clone.coolify.example.yaml` for a disposable copy of an existing installation. It copies the deployed application, its actual `vendor/` tree, uploaded files, and a database snapshot. The source installation is only read. The importer does not invoke its Warexo console, installer, Composer, or application kernel.

This is the legacy PHP 7.4 baseline for reproducing customer behavior before changing PHP/Symfony. It does not itself upgrade either version.

## Create the Coolify resource

1. Create a **new Docker Compose resource** on the development host. Use fresh code, state, and database volumes; do not point this at an existing development database.
2. Paste `compose.clone.coolify.example.yaml`.
3. Set the source variables below. Store the private key as a protected environment value in Coolify, not in Git.
4. Assign the development domain to **`gateway`**, port 80. Keep the existing authentication in front of that domain. Do not assign a domain or an additional externally routed network to `warexo`, `clone`, `db`, or `mailpit`.
5. Deploy. The `clone` service exits successfully when the import is complete. `warexo` then verifies the local clone, writes local configuration, warms its cache, and starts Apache. The gateway waits for the application health check.

```env
SOURCE_SSH_HOST=source.example.com
SOURCE_SSH_PORT=22
SOURCE_SSH_USER=clone-reader
SOURCE_SSH_PRIVATE_KEY=<private key, including BEGIN/END lines>
SOURCE_SSH_KNOWN_HOSTS=<verified known_hosts line for this host and port>
SOURCE_WAREXO_PATH=/var/www/customer/warexo
```

The path must be the application directory containing `app/config/parameters.yml`, `app/console`, `composer.lock`, `vendor/`, and `web/`. Paths with spaces are supported. The importer reads the database host, port/socket, name, user, and password from `parameters.yml`; separate live database inputs are not needed. Only resolved, literal YAML parameters for MySQL/MariaDB are supported.

`SOURCE_SSH_KNOWN_HOSTS` is mandatory. Obtain and verify the server's host key through a trusted channel. For a nonstandard port, the entry starts with `[source.example.com]:222`. An unverified `ssh-keyscan` result alone does not establish the server's identity. Multiline private keys can contain actual newlines or literal `\n` sequences.

Optional inputs:

| Variable | Default | Purpose |
| --- | --- | --- |
| `SOURCE_PHP_BIN` | `php` | Source PHP executable, such as `/usr/bin/php7.4`; one executable, no arguments |
| `SOURCE_RSYNC_BWLIMIT_KB` | `0` | rsync bandwidth limit in KiB/s; zero means unlimited |
| `CLONE_ADMIN_USERNAME` | empty | Existing user to keep for development; otherwise choose an enabled super-admin/admin deterministically |
| `WAREXO_LICENSE` | empty | Development license written to the local parameters file |
| `WAREXO_RUNTIME_IMAGE` | `ghcr.io/aggrosoft/warexo-dev-runtime:main` | Override for testing an explicitly built image |

Coolify supplies `SERVICE_PASSWORD_64_DBROOT`, `SERVICE_PASSWORD_64_DB`, `SERVICE_PASSWORD_64_CLONEADMIN`, and `SERVICE_BASE64_64_APP`. When using plain Docker Compose, set these four values yourself; the clone password needs at least 12 UTF-8 bytes. The source must be reachable **from the importer container**, including DNS and firewall access.

The importer also accepts `SOURCE_SSH_PRIVATE_KEY_FILE` and `SOURCE_SSH_KNOWN_HOSTS_FILE` for custom deployments using mounted secret files. The supplied Coolify template uses the inline variables above.

## First start and login

The importer checks the source, copies files with rsync, obtains a transactional dump over SSH, restores it into the empty target database, and then runs the neutralization step. Deployed vendor patches remain intact. It does not install dependencies or perform a schema migration. Tables, views, triggers, and routines are restored. Database event definitions are kept only as reference in the protected manifest; scheduled events are not installed in the target.

The clone keeps the selected user's roles and client memberships, sets a new local password, unlocks the account, and disables the other existing user accounts. It clears recognized shop/mail/plugin credentials and recipient addresses and resets tokens. The password is the Coolify-generated **`SERVICE_PASSWORD_64_CLONEADMIN`**, not the source user's password. It is not printed in logs.

Find the selected login and current state through the `warexo` container terminal or existing SSH bridge:

```bash
/opt/warexo/bin/warexo-clone-status
```

Add `--details` to inspect the snapshot manifest and neutralization counts.

The existing SSH bridge enters the `warexo` service; the copied workspace is `/var/www/html`. The copy excludes `.git` and does not invent a Git history for customer files. `warexo-checkout` is for fresh Git installations and must not be used to overwrite this baseline. Connect the migration working tree deliberately once the imported behavior has been verified.

## Source requirements

- A Linux SSH account that can read the application, deployed vendor tree, and uploaded documents. Root is not required.
- `rsync`, `head`, PHP CLI 7.4 or newer, and a compatible `mysqldump`/`mariadb-dump` plus database client available to that account. The PHP helper is supplied over SSH stdin; no helper is installed on the source. Auto-prepend/append files and CLI OPcache preloading are disabled for that invocation.
- The database account in the source parameters must be able to read all tables and dump views, triggers, routines, and events when present. Permission failures stop the import.
- All source base tables must use InnoDB. The importer rejects nontransactional tables instead of silently claiming a consistent snapshot or locking a live installation.
- Relative symlinks that stay inside the application are supported. External, absolute, and broken symlinks are rejected; arrange a self-contained source snapshot first when the installation uses shared directories.

The database snapshot is transactional. The file copy and database dump are **not one atomic snapshot**. Avoid deployments/schema changes during import. If business operations must align exactly with generated files, import a coordinated backup/snapshot or use an agreed quiet window. The manifest records selected bootstrap/vendor fingerprints and database metadata; it is not a full source-tree checksum.

## Isolation

Only the one-shot importer receives the source SSH credentials and has an outbound network. The application, database, and Mailpit share an internal backend network. A separate nginx gateway exposes a fixed upstream to Warexo. The database event scheduler is disabled at server startup, and event definitions are archived without restoring them (MariaDB also disallows event DDL when the scheduler is `DISABLED`). Warexo cron jobs are absent, and the installer/debug/restore entrypoints are removed or blocked. Mail is configured to the local Mailpit service.

Keep that network topology when deploying in Coolify. Adding a routed network to `warexo` bypasses the outbound boundary. Git fetches, Composer downloads, and agents that need external APIs should run in a separate development/controller service; the imported application intentionally has no general internet access.

Known secrets are neutralized, but arbitrary custom integrations and business fields cannot be fully classified automatically. The clone retains customer/business documents and personal data. Treat access to it accordingly; this is not a public demo dataset. A valid development license and installation-specific configuration may still be needed for the real application to start.

## Boundaries and recovery

- **Normal restart/redeploy:** preserves code and database edits. The importer validates the local readiness record and target schema without connecting to the source, and does not repeat the copy, reset passwords, or restore the dump. Application startup repeats local checks and cache warmup.
- **Wrong SSH key, host, path, or another preflight error:** correct the inputs and redeploy. No target application import has started yet.
- **Interrupted copy/restore or failed neutralization:** the `clone-started` marker prevents a second import from overwriting partial data. Inspect the logs, then create a new disposable resource with fresh code, state, and database volumes. There is no automatic destructive reset.
- **Warmup failure after a successful import:** the copied workspace remains available for inspection through the SSH bridge. Apache stays stopped and the service is unhealthy. Fix local configuration/code and restart the application; do not reimport unnecessarily.
- **Missing/mismatched volumes:** keep the code, state, and database volumes together. Startup checks the readiness manifest, essential tables, and a minimum table count. These are structural checks, not a cryptographic identity for a database with an otherwise identical schema.
- **Unsupported password encoder or source schema:** neutralization fails before application startup. The current login reset supports the legacy Symfony SHA512 encoder and its configured iterations/base64 setting.
- **Unsupported database syntax/collation:** restoration fails before application startup. The target is MariaDB 10.11; the importer does not silently rewrite arbitrary schemas or promise compatibility with every MySQL/MariaDB version.

Protected diagnostic files are in `/var/lib/warexo`: `clone-error.log`, `clone-manifest.json`, and `clone-sanitization.json`. The SQL dump is deleted after successful neutralization; on failure it remains protected for diagnosis. Source SSH keys are temporary files in the importer and are not copied to the application volume.

## Verification

The workflow runs unit tests and builds the actual runtime image. Before publishing it, it runs `tests/test-clone-integration.sh` against a synthetic SSH source and MariaDB. That exercises file/database import, vendor preservation, login reset, routine/trigger restoration, event archiving, source preservation, outbound isolation, failed-host-key startup, and a restart after removing the source. This synthetic fixture does not replace the first acceptance check against a representative existing Warexo installation.
