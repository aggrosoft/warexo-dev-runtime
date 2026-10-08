#!/usr/bin/env bash
set -u

mkdir -p /var/lib/warexo

if ! /opt/warexo/bin/setup-ssh; then
    echo "[warexo-dev] WARNING: Git SSH setup failed; continuing so the container stays reachable." >&2
fi

/usr/sbin/sshd || echo "[warexo-dev] WARNING: sshd failed to start." >&2

bootstrap() {
    # Clone only when there is no working copy yet.
    if [ ! -d /var/www/html/.git ]; then
        /opt/warexo/bin/clone-warexo
    fi

    # Always regenerate environment-specific configuration.
    /opt/warexo/bin/create-parameters

    # Complete the first bootstrap only after all required steps succeeded.
    # This intentionally does NOT use the presence of .git as the marker,
    # because a failed bootstrap may already have cloned the repository.
    if [ ! -f /var/lib/warexo/initialized ]; then
        if [ -n "${WAREXO_SNAPSHOT_URL:-}" ]; then
            /opt/warexo/bin/restore-snapshot
        fi

        /opt/warexo/bin/install-app
        touch /var/lib/warexo/initialized
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
    echo "[warexo-dev] Container will stay up for debugging." >&2
fi

exec "$@"
