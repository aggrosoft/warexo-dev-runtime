# Warexo Dev Runtime

Reproducible development runtime for legacy Warexo installations.

## Scope

This repository provides the runtime only. The Warexo application itself is cloned from its existing Git remote into a persistent volume and can then be edited directly over SSH / VS Code Remote SSH.

Current baseline:

- PHP 7.4
- Apache
- MariaDB 10.11
- Composer 1
- Symfony prod environment
- Mailpit for outgoing mail
- no Warexo cron jobs enabled by default

## Required variables

For a normal development instance only the external access settings need to be supplied:

```env
WAREXO_GIT_URL=git@git.example.com:warexo/warexo.git
WAREXO_GIT_REF=master

WAREXO_GIT_PRIVATE_KEY=...
WAREXO_GIT_KNOWN_HOSTS=...

SSH_AUTHORIZED_KEYS=...
```

Database credentials and the Symfony secret are generated automatically and persisted in the `warexo-secrets` volume.

Optional snapshot restore during first bootstrap:

```env
WAREXO_SNAPSHOT_URL=https://example.invalid/warexo-dev.sql.zst
WAREXO_SNAPSHOT_TOKEN=
```

## First bootstrap

On the first start the application container:

1. configures SSH
2. clones the Warexo repository
3. checks out `WAREXO_GIT_REF`
4. generates `app/config/parameters.yml`
5. optionally restores a database snapshot
6. runs `composer install`
7. warms the Symfony prod cache
8. starts Apache and SSH

Normal container restarts do not reset the source checkout.

## Remote development

The working tree is directly available at:

```text
/var/www/html
```

Use the `developer` user over SSH.

To switch the running application to another branch, tag, or commit:

```bash
/opt/warexo/bin/warexo-checkout feature/my-change
```

## Important safety default

Warexo cron jobs are intentionally not installed or enabled. The production cron set contains commands that import orders, send mail, fetch external mail, download data, export products and run subscriptions. These must be classified before any automatic dev cron profile is introduced.

## Next steps

- make snapshot restore production-ready
- add deterministic reset command
- sanitize copied production data
- classify safe/unsafe cron jobs
- verify legacy extension requirements against a real instance
