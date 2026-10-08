#!/usr/bin/env bash
set -u

mkdir -p /var/lib/warexo

# This SSH configuration is only for cloning/pushing the Warexo source repository.
# Developer access is handled centrally by coolify-ssh-bridge through docker exec.
if ! /opt/warexo/bin/setup-git-ssh; then
    echo "[warexo-dev] WARNING: Git SSH setup failed; continuing so the container stays reachable." >&2
fi

bootstrap() {
    if [ ! -d /var/www/html/.git ]; then
        /opt/warexo/bin/clone-warexo
    fi

    /opt/warexo/bin/create-parameters

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
    echo "[warexo-dev] Container will stay up for inspection through Coolify/coolify-ssh-bridge." >&2
fi

exec "$@"
