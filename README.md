# Warexo Dev Runtime

Disposable Warexo development environments for Coolify.

This repository publishes the runtime image:

```text
ghcr.io/aggrosoft/warexo-dev-runtime:main
```

Do **not** deploy this repository as a normal GitHub application in Coolify. Create a **Docker Compose** resource and use `compose.coolify.example.yaml` as the template.

## Runtime

The image intentionally keeps the legacy application stack stable:

- PHP 7.4 + Apache
- Symfony production environment
- Composer 1 for the committed legacy lock file
- MariaDB 10.11 in the Coolify template
- Mailpit for development mail
- no Warexo cron jobs by default
- Git working copy persisted in a named volume

The Warexo source itself is not part of this repository. It is cloned from the configured self-hosted Git remote into:

```text
/var/www/html
```

## Create a new instance in Coolify

1. Create a **Docker Compose** resource.
2. Paste/use `compose.coolify.example.yaml`.
3. Set the required Git variables.
4. Deploy.

The `warexo` service exposes port 80 through Coolify using:

```text
SERVICE_URL_WAREXO_80=/
SERVICE_FQDN_WAREXO
```

Coolify generates the internal database passwords and application secret through its `SERVICE_PASSWORD_*` / `SERVICE_BASE64_*` variables.

## Required environment

Only source-repository access is required for a blank development instance:

```env
WAREXO_GIT_URL=ssh://user@git-host.example/path/to/warexo.git
WAREXO_GIT_REF=master

WAREXO_GIT_PRIVATE_KEY=...
WAREXO_GIT_KNOWN_HOSTS=...
```

`WAREXO_GIT_PRIVATE_KEY` is the private SSH key used **from the Warexo container to the self-hosted repository host**. It is unrelated to developer SSH access.

For non-standard SSH ports, use an SSH URL that contains the port and put the matching `[host]:port` entry into `WAREXO_GIT_KNOWN_HOSTS`.

## Developer SSH / VS Code

Remote access is handled by the central:

```text
aggrosoft/coolify-ssh-bridge
```

The Compose template enables it with:

```env
AGGRO_SSH_ENABLED=true
```

There are no SSHPiper labels, no per-instance `authorized_keys`, and no developer-facing SSH daemon in the Warexo image.

The bridge enters the running Compose service through Docker exec. For this template the service name is:

```text
warexo
```

The working tree is:

```text
/var/www/html
```

VS Code server and Codex state have their own persistent volumes.

## First bootstrap

On first start the runtime:

1. configures SSH access to the self-hosted Git repository
2. clones Warexo into the persistent source volume
3. checks out `WAREXO_GIT_REF`
4. generates `app/config/parameters.yml`
5. runs the committed Composer lock file
6. warms the Symfony production cache
7. starts Apache
8. leaves the fresh database ready for `/install.php`

A failed bootstrap does not stop the container. The instance remains reachable for inspection and the next restart resumes incomplete initialization.

The successful runtime initialization marker is:

```text
/var/lib/warexo/runtime-initialized
```

## Database and application configuration

The Coolify template generates:

```text
database: warexo
user:     warexo
password: SERVICE_PASSWORD_64_DB
secret:   SERVICE_BASE64_64_APP
```

The runtime writes these values to the legacy:

```text
app/config/parameters.yml
```

Mail is redirected to the local `mailpit` service.

## Git checkout

To switch the running working copy:

```bash
/opt/warexo/bin/warexo-checkout feature/foo
```

This fetches the remote, checks out the requested branch/tag/commit, runs Composer install and rebuilds the Symfony cache.

Normal container restarts do not reset or switch the working copy.

## Fresh database setup

Development instances intentionally start with a fresh MariaDB database. No production database dump is imported.

The runtime prepares the application code and configuration, then leaves schema/data initialization to Warexo's own installer:

```text
/install.php
```

This keeps the database schema tied to the checked-out Warexo revision instead of coupling a current Git branch to an unrelated historical live dump.

A clean reset is therefore the disposable-resource workflow:

1. delete the Coolify Compose resource
2. delete its named volumes
3. create a new resource from `compose.coolify.example.yaml`
4. run the Warexo installer again

Normal container restarts keep the existing working tree and development database.

## Safety

Production Warexo cron jobs are intentionally absent. The existing production schedule contains imports, mail delivery, mailbox polling, downloads, subscriptions and webshop exports. They must not start automatically in a cloned development database.

External integrations will be classified and neutralized as part of the snapshot/sanitization work before cron profiles are introduced.
