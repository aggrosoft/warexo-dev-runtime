#!/usr/bin/env bash
set -euo pipefail

mkdir -p /var/lib/warexo

/opt/warexo/bin/setup-ssh

if [ ! -d /var/www/html/.git ]; then
    /opt/warexo/bin/clone-warexo
    /opt/warexo/bin/create-parameters

    if [ -n "${WAREXO_SNAPSHOT_URL:-}" ]; then
        /opt/warexo/bin/restore-snapshot
    fi

    /opt/warexo/bin/install-app
    touch /var/lib/warexo/initialized
else
    /opt/warexo/bin/create-parameters
fi

/usr/sbin/sshd

exec "$@"
