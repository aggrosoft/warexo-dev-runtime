# Optional dual-PHP porting workspace

The normal fresh-install and SSH-clone templates still use the unchanged
PHP 7.4 image. This feature is opt-in: it adds PHP 8.4 and Composer 2 to the
**same `warexo` application container**, while retaining the working clone.
It does not port Symfony or change Warexo's UI or business behavior.

| Purpose | Image / template |
| --- | --- |
| Existing PHP 7.4 clone | `ghcr.io/aggrosoft/warexo-dev-runtime:main`, `compose.clone.coolify.example.yaml` |
| Optional dual-PHP clone | `ghcr.io/aggrosoft/warexo-dev-runtime:main-porting`, `compose.clone.porting.coolify.example.yaml` |
| Explicit dependency proxy | `ghcr.io/aggrosoft/warexo-dev-runtime:main-porting-proxy` |

The extra image defaults to `WAREXO_PORTING_ENABLED=0`; its original entrypoint
is used in that mode. The optional Compose definition explicitly sets `1` and
adds the workspace initializer, separate porting ingress, and dependency proxy.
The standard image and templates do not acquire any of these services.

## Enable on an already working clone in Coolify

1. Back up the working **local development** database, code, and files. Finish
   any concurrent edits first: a transactional database dump and a file copy
   are not one atomic cross-resource snapshot.
2. Edit the **existing Coolify Compose resource**. Keep its resource/project
   identity, existing volume mappings, reference domain, and generated secrets.
   Do not create a replacement resource or rename the reference volumes.
3. Apply the additions in `compose.porting.override.yaml`. For an otherwise
   unmodified clone resource, the complete equivalent is
   `compose.clone.porting.coolify.example.yaml`. Compare any local adjustments
   before replacing the saved Compose text; preserve explicit external volume
   names or bind mounts if your resource uses them.
4. Let Coolify create `SERVICE_PASSWORD_64_PORTDB` and
   `SERVICE_BASE64_64_PORTAPP`. Retain their values on later deployments.
   The reference passwords and secret do not change.
5. Optionally assign a second protected development URL to
   **`porting-gateway`**, port 80. Keep the original URL on `gateway`.
   Never assign a public URL, host port, or routed network to `warexo`,
   `porting-init`, `db`, or `deps-proxy`.
6. Redeploy and reconnect through the same SSH bridge to service `warexo`.

Do not rerun the source import to enable this feature. With the same reference
volumes and a valid completed clone, the existing importer verifies its local
state and makes no source connection. The porting initializer copies only from
the already neutralized local clone. It **never reads or uses `SOURCE_*`** and
does not require any source SSH access.

**Coolify v4 Compose caveat:** Coolify can inject the resource-wide `.env`
into **every** container via `env_file`, even when a service does not declare
those variables. The porting initializer deliberately ignores inherited
`SOURCE_*` values instead of failing on them. Both porting Compose files also
set the known source variables to empty strings in non-clone services as a
best-effort reduction of accidental credential exposure. Do not rely on these
entries as a security boundary: Coolify v4 can still expose project-wide secrets
to other services. Keep this development deployment private; use separately
scoped resources when strict per-container secret isolation is required.

For ordinary Docker Compose, the additive equivalent is:

```bash
docker compose -f compose.clone.coolify.example.yaml -f compose.porting.override.yaml up -d
```

Run that only in the existing project's context with its existing environment.
Set the two new generated-secret equivalents yourself when not using Coolify.
This is an operator/CI command, not a command that needs to work inside `warexo`.

For a **new** clone resource, use the complete porting template and follow
[CLONE.md](CLONE.md) for the original source inputs and first import.

## What starts, and what does not

`porting-init` is a one-shot local initializer. It reads the reference files
through read-only mounts, copies code including deployed `vendor` changes into
`warexo_porting_workspace`, and restores the local database into `warexo_port`.
That database receives its own user and password. Only the initializer receives
the local database administrator password; the application does not.

The initializer does not execute copied PHP, Composer, the installer, a schema
update, cron, or a worker. Database events stay disabled and are not imported.
It writes separate local parameters and the completion marker
`/workspace/.warexo-porting-ready.json` only after successful initialization.
A normal restart verifies the completed state; it does not recopy files,
reimport data, or erase porting work. Refreshing the reference does not silently
refresh the porting database either.

| Item | Reference | Porting work |
| --- | --- | --- |
| Code | `/var/www/html` | `/workspace/warexo-port` (isolated deployed snapshot), `/workspace/repos/next` or `/workspace/repos/aggrowawi-php84` (selected clean Git checkout) |
| PHP CLI | existing `php`, also `php74` | `php84` |
| Composer | existing Composer 1 | `composer84`, using Composer 2 and PHP 8.4 |
| Dependencies | existing reference `vendor` | separately copied `vendor`, then deliberately updated |
| Database / user | `warexo` / `warexo` | `warexo_port` / `warexo_port` |
| HTTP inside the container | Apache port 80 | Apache port 8084, PHP-8.4-FPM serving the selected checkout only |
| Public ingress | `gateway` | optional `porting-gateway` |

PHP 8.4 has separate INI files, extensions, cache/session/temp directories, and
process context. The porting entrypoint does **not** warm up the old application
under PHP 8.4. Expect the old Symfony application to need code and dependency
changes before its routes work. The porting health endpoint tests the PHP 8.4
runtime only; a green health check is not proof of successful Warexo migration.
The porting URL serves the selected **clean Git checkout**, not the PHP 7.4
reference at `/var/www/html` or the deployed snapshot in `/workspace/warexo-port`.
`WAREXO_PORTING_TARGET=next` (the default) serves `/workspace/repos/next/backend/public`.
`WAREXO_PORTING_TARGET=aggrowawi` serves `/workspace/repos/aggrowawi-php84/web`
and uses the original `web/app.php` front controller. The PHP 7.4 gateway and
all existing volumes remain unchanged in either mode. The chosen checkout must
be prepared before switching the target in Coolify. Without that checkout or
compatible dependencies, application pages can return 404/500 while the PHP
8.4 health endpoint stays green. No automatic checkout, Composer execution,
or application bootstrap occurs at container start.

Inside the existing SSH/IDE session:

```bash
php74 -v
php84 -v
composer84 --version
warexo-port-status
curl --fail http://127.0.0.1/__warexo_runtime_health
curl --fail http://127.0.0.1:8084/__warexo_porting_health
# After the selected checkout and its dependencies are prepared:
curl -i http://127.0.0.1:8084/login
```

In `next` mode the porting Apache uses Symfony's `backend/public/index.php`;
missing assets route through Symfony's existing customization hooks. In
`aggrowawi` mode it uses `web/app.php` with Apache fallback routing and
ignores the historic `.htaccess`. The PHP 8.4 preview blocks direct
installation, development, restore and update endpoints, including Legacy's
`/updates/composer` schema-update command. This is not a blanket sandbox for
other Legacy HTTP side effects: keep the preview access-restricted, review
integrations and jobs, and use only an isolated database.
PHP-FPM receives allowlisted preview database and secret settings;
`WAREXO_USER_ASSET_DIR` points to the isolated local copy at
`/workspace/warexo-port/web`. The optional Compose sets `PORTING_APP_ENV=dev`
and `PORTING_APP_DEBUG=0` (for Next only). The original aggrowawi reads
`app/config/parameters.yml`: provide a separate untracked copy configured for
the isolated porting database; never reuse production credentials. PHP changes
are picked up without rebuilding the image because OPcache checks timestamps.

In Coolify, assign the protected HTTPS domain to the **`porting-gateway`
service on port 80**, never to `warexo` itself. This is separate from the
PHP 7.4 `gateway` domain. SSL issuance and Traefik routes are managed by
Coolify and still require correct DNS, service mapping and an operational proxy.
An external `no available server` error is a proxy-routing issue even if the
internal PHP 8.4 health URL works.

Work on the selected clean checkout in `/workspace/repos`, never on
`/var/www/html`. The separate `/workspace/warexo-port` is a deployed-source
reference and user-asset store, not a Git worktree. Invoke porting PHP explicitly;
review hard-coded PHP paths and subprocess calls in ported Warexo code too. A source-level
absolute `/usr/bin/php` is not repaired merely by providing a `php84` command.

## Git and package access without opening the application network

The `/workspace` volume is persistent and can also hold clean checkouts under
`/workspace/repos`. The deployed copy intentionally has **no invented Git
history**. It includes customer files and may contain installation-specific
source changes. Do not run `git init`, `git add .`, or `warexo-checkout` over that
copy. Reconcile selected source files and vendor patches with the actual Legacy
Git revision before transferring changes into the clean target repository.

`warexo-net` enables the dependency proxy for one explicit command. Proxy
variables are not globally injected into PHP, Apache, or application processes.
For example, using an already configured HTTPS repository credential helper:

```bash
mkdir -p /workspace/repos
warexo-net git clone https://github.com/warexo/next.git /workspace/repos/next
# For the original application's PHP 8.4 compatibility branch instead:
warexo-net git clone --branch port/php84-compat --single-branch \
  https://github.com/warexo/aggrowawi.git /workspace/repos/aggrowawi-php84
```

Do not switch `WAREXO_PORTING_TARGET=aggrowawi` in Coolify until the Git checkout
exists and its ignored `app/config/parameters.yml` is configured for a
separate porting database. The existing `warexo_port` account is read-only;
test writes require a separately authorized, isolated writable copy.
Do not run setup/install/update scripts as part of switching the runtime.
The runtime does not repurpose source-installation credentials. Private
repositories still require your normal repository authorization. Do not put
tokens in command-line URLs, images, or tracked files. Missing authorization is
a blocker, not a reason to inspect the source importer's secrets.

After reviewing the porting dependency changes and entering the correct
porting working directory, package operations can use:

```bash
warexo-net composer84 install --no-scripts --no-plugins
```

Do not run this as an automatic first boot action. The old lockfile may not yet
be compatible with Composer 2 or PHP 8.4. Review scripts and plugins before
allowing either, and never solve that mismatch by updating reference `vendor`.

The proxy permits HTTPS CONNECT on port 443 to these exact public hostnames:
`github.com`, `api.github.com`, `codeload.github.com`,
`raw.githubusercontent.com`, `objects.githubusercontent.com`,
`release-assets.githubusercontent.com`, `packagist.org`, `repo.packagist.org`,
and `getcomposer.org`. Other hosts, private/local destination addresses,
unrestricted HTTP, and direct application internet access are denied.

`PORTING_ALLOWED_HOSTS` replaces that whitespace-separated list when explicitly
set. Add only a required repository/package host after review; hostnames are
validated and wildcards are not accepted. The proxy does not inspect encrypted
URL paths or distinguish PHP from an agent. Code could deliberately use it to
reach an allowed host, including write APIs on that host. This is a limited
outbound allowance, not a security boundary between the two applications or a
guarantee against data exfiltration. Codex API access is not enabled by this
default package list.

Both application runtimes continue to use the internal backend network. The
extra proxy is infrastructure only: agents do not need to enter it or receive
the Docker socket. No clone source keys are mounted into it.

## Restart, disable, and failure handling

- Keep the workspace volume, porting database, and porting secrets together.
  Existing successful work is reused rather than reset.
- A partial initialization or inconsistent marker/database fails closed.
  Inspect the error and protect any work before recovery. If discarding a failed
  initialization is intended, identify and remove **only** its disposable
  workspace and `warexo_port` database/user through a separately reviewed
  operator action. There is no automatic destructive reset command.
- Do not delete readiness markers to force an overwrite. Never delete the
  reference `warexo` database, `warexo_code`, `warexo_state`, or `warexo_db` volumes
  as part of porting recovery.
- To disable the feature, restore the original clone Compose definition and
  reference image in the **same resource**. Stop/remove only the optional
  `porting-init`, `porting-gateway`, and `deps-proxy` services if your orchestrator
  leaves them behind. Retain the workspace volume and porting database for later
  use; do not run `down -v` or a volume prune.
- Merely switching `WAREXO_PORTING_ENABLED` off stops extra runtime startup but
  does not remove the optional Compose services. Use the original template for
  a complete return to the standard clone setup.

The reference and porting process share a container and application account.
Directory and database separation reduce accidental cross-writes but are not
hostile-agent isolation. Preserve an independent known-good local backup and
keep customer-derived data protected and out of repositories and CI.

## Maintainer build

The optional image builds PHP 8.4 in a separate prefix on the same base ABI and
preserves `/usr/local/bin/php`, Apache/PHP 7.4, and Composer 1. Release downloads
are versioned and checksum-verified. The ordinary Dockerfile is unchanged.

On a build machine with Docker, the basic sequence is:

```bash
docker build -t warexo-runtime:base .
docker build -f Dockerfile.porting --build-arg BASE_IMAGE=warexo-runtime:base -t warexo-runtime:porting .
docker build -f Dockerfile.porting-proxy -t warexo-runtime:porting-proxy .
```

The published `main-porting` and `main-porting-proxy` variants are produced by
the repository workflow after merge and successful verification. A pull request
or this document alone does not deploy a resource or alter an existing clone.
