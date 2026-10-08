#!/usr/bin/env bash
set -u

mkdir -p /var/lib/warexo

# Configure SSH first. A failed application bootstrap must not make the
# development container inaccessible for debugging.
if ! /opt/warexo/bin/setup-ssh; then
    echo "[warexo-dev] WARNING: SSH setup failed; continuing so the container stays reachable." >&2
fi

# Start sshd before bootstrapping Warexo. Coolify terminal access only needs
# the container to stay alive; the SSH bridge can use sshd as soon as it is configured.
/usr/sbin/sshd || echo "[warexo-dev] WARNING: sshd failed to start." >&2

bootstrap() {
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
}

echo "[warexo-dev] Starting Warexo bootstrap..."

if bootstrap; then
    rm -f /var/lib/warexo/bootstrap-failed
    touch /var/lib/warexo/bootstrap-ok
    echo "[warexo-dev] Bootstrap completed."
else
    status=$?
    rm -f /var/lib/warexo/bootstrap-ok
    printf '%s\n' "$status" > /var/lib/warexo/bootstrap-failed
    echo "[warexo-dev] ERROR: Bootstrap failed with exit code $status." >&2
    echo "[warexo-dev] Container will stay up for debugging via Coolify terminal/SSH." >&2
fi

exec "$@"
