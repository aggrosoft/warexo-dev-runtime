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

    # Environment-specific config is regenerated on every start.
    /opt/warexo/bin/create-parameters

    # Composer/application preparation only needs to complete once per disposable
    # workspace. Database schema/data are intentionally NOT cloned here.
    if [ ! -f /var/lib/warexo/runtime-initialized ]; then
        /opt/warexo/bin/install-app
        touch /var/lib/warexo/runtime-initialized
    fi

    /opt/warexo/bin/fix-runtime-permissions
}

echo "[warexo-dev] Starting Warexo runtime bootstrap..."

if bootstrap; then
    rm -f /var/lib/warexo/bootstrap-failed
    touch /var/lib/warexo/bootstrap-ok
    echo "[warexo-dev] Runtime bootstrap completed."
    echo "[warexo-dev] Fresh database is ready for the Warexo installer at /install.php."
else
    status=$?
    rm -f /var/lib/warexo/bootstrap-ok
    printf '%s\n' "$status" > /var/lib/warexo/bootstrap-failed
    echo "[warexo-dev] ERROR: Runtime bootstrap failed with exit code $status." >&2
    echo "[warexo-dev] Container will stay up for inspection through Coolify/coolify-ssh-bridge." >&2
fi

exec "$@"
