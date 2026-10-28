#!/usr/bin/env bash
# Run after building WAREXO_RUNTIME_IMAGE (default warexo-runtime:test).
set -euo pipefail
test_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd "$test_root"
test_project="warexo-clone-test-$(date +%s)-$$"
export WAREXO_RUNTIME_IMAGE=${WAREXO_RUNTIME_IMAGE:-warexo-runtime:test}
export WAREXO_TEST_SOURCE_IMAGE="$test_project-source:latest"
export WAREXO_TEST_NETWORK="$test_project-intake"
export WAREXO_TEST_TMP
WAREXO_TEST_TMP=$(mktemp -d /tmp/warexo-clone-test.XXXXXXXX)
source_compose=(docker compose -p "$test_project-source" -f tests/integration/source-compose.yaml)
target_compose=(docker compose -p "$test_project" -f compose.clone.coolify.example.yaml -f tests/integration/target-override.yaml)
failed_compose=(docker compose -p "$test_project-failed" -f compose.clone.coolify.example.yaml -f tests/integration/target-override.yaml)
if [[ ${WAREXO_TEST_PORTING:-0} == 1 ]]; then
    target_compose+=( -f compose.porting.override.yaml -f tests/integration/porting-override.yaml )
    export SERVICE_PASSWORD_64_PORTDB=porting-app-fixture-password
    export SERVICE_BASE64_64_PORTAPP=separate-porting-fixture-secret
fi
cleanup() {
    local status=$?
    trap - EXIT
    if (( status != 0 )); then
        "${source_compose[@]}" logs --tail 80 || true
        "${target_compose[@]}" logs --tail 120 || true
        "${failed_compose[@]}" logs --tail 80 || true
        # These volumes contain only synthetic fixture data. Keep production
        # importer diagnostics protected; expose this test log before cleanup.
        for fixture_project in "$test_project" "$test_project-failed"; do
            if docker volume inspect "${fixture_project}_warexo_state" >/dev/null 2>&1; then
                docker run --rm --entrypoint sh -v "${fixture_project}_warexo_state:/state:ro" "$WAREXO_RUNTIME_IMAGE" \
                    -c 'if [ -f /state/clone-error.log ]; then cat /state/clone-error.log; fi' || true
            fi
        done
    fi
    "${failed_compose[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
    "${target_compose[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
    "${source_compose[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
    docker network rm "$WAREXO_TEST_NETWORK" >/dev/null 2>&1 || true
    docker image rm "$WAREXO_TEST_SOURCE_IMAGE" >/dev/null 2>&1 || true
    rm -rf -- "$WAREXO_TEST_TMP"
    exit "$status"
}
trap cleanup EXIT
export SOURCE_SSH_HOST=source-ssh SOURCE_SSH_PORT=22 SOURCE_SSH_USER=root
export SOURCE_WAREXO_PATH='/srv/warexo instance'
export SERVICE_PASSWORD_64_DBROOT=target-root-fixture-password SERVICE_PASSWORD_64_DB=target-app-fixture-password
export SERVICE_PASSWORD_64_CLONEADMIN=clone-fixture-password SERVICE_BASE64_64_APP=fixture-application-secret
export WAREXO_COOKIE_SECURE=false SOURCE_SSH_PRIVATE_KEY SOURCE_SSH_KNOWN_HOSTS
ssh-keygen -q -t ed25519 -N '' -f "$WAREXO_TEST_TMP/source_key"
SOURCE_SSH_PRIVATE_KEY=$(cat "$WAREXO_TEST_TMP/source_key")
SOURCE_SSH_KNOWN_HOSTS='pending fixture host key'
docker build -q --build-arg "WAREXO_RUNTIME_IMAGE=$WAREXO_RUNTIME_IMAGE" \
    -f tests/integration/Dockerfile.source -t "$WAREXO_TEST_SOURCE_IMAGE" .
docker network create "$WAREXO_TEST_NETWORK" >/dev/null
"${source_compose[@]}" up -d --wait --wait-timeout 180
SOURCE_SSH_KNOWN_HOSTS="source-ssh $("${source_compose[@]}" exec -T source-ssh cat /etc/ssh/ssh_host_ed25519_key.pub)"
source_files() {
    "${source_compose[@]}" exec -T source-ssh bash -c \
        'find "$1" -type f -print0 | sort -z | xargs -0 sha256sum' -- "$SOURCE_WAREXO_PATH"
}
source_database() {
    "${source_compose[@]}" exec -T -e MYSQL_PWD=source-fixture-password source-db \
        mariadb-dump --no-defaults --user=root --single-transaction --skip-lock-tables --skip-comments \
        --routines --events --triggers warexo_source
}
source_files > "$WAREXO_TEST_TMP/files-before"
source_database > "$WAREXO_TEST_TMP/database-before"
"${target_compose[@]}" up -d --wait --wait-timeout 240
"${target_compose[@]}" exec -T warexo python3 - < tests/integration/assert-clone.py
[[ $("${target_compose[@]}" exec -T -e MYSQL_PWD="$SERVICE_PASSWORD_64_DBROOT" db \
    mariadb --no-defaults --user=root --batch --skip-column-names \
    --execute="SELECT COUNT(*) FROM mysql.event WHERE db='warexo'") == 0 ]]
gateway_address=$("${target_compose[@]}" port gateway 80)
[[ $(curl --fail --silent --retry 10 --retry-connrefused --retry-delay 1 --max-time 10 "http://$gateway_address/") == 'warexo clone fixture' ]]
[[ $(curl --silent -o /dev/null -w '%{http_code}' "http://$gateway_address/install.php") == 404 ]]
canary_id=$("${source_compose[@]}" ps -q canary)
canary_ip=$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "$canary_id")
docker run --rm --network "$WAREXO_TEST_NETWORK" --entrypoint curl "$WAREXO_RUNTIME_IMAGE" \
    --fail --silent --max-time 5 "http://$canary_ip/" >/dev/null
if "${target_compose[@]}" exec -T warexo curl --noproxy '*' --fail --silent --max-time 3 "http://$canary_ip/"; then
    echo 'FAIL: application reached the external intake canary.' >&2
    exit 1
fi
if [[ ${WAREXO_TEST_PORTING:-0} == 1 ]]; then
    # Sourced deliberately: reuse this isolated fixture's exact Compose project.
    source tests/test-porting-integration.sh
    verify_porting_initial
fi
source_files > "$WAREXO_TEST_TMP/files-after"
source_database > "$WAREXO_TEST_TMP/database-after"
cmp "$WAREXO_TEST_TMP/files-before" "$WAREXO_TEST_TMP/files-after"
cmp "$WAREXO_TEST_TMP/database-before" "$WAREXO_TEST_TMP/database-after"
"${source_compose[@]}" exec -T source-ssh test ! -e "$SOURCE_WAREXO_PATH/app/warmup-ran"

# Wrong host key must block the actual importer and all application startup.
correct_host_key=$SOURCE_SSH_KNOWN_HOSTS
SOURCE_SSH_KNOWN_HOSTS="source-ssh $(cat "$WAREXO_TEST_TMP/source_key.pub")"
"${failed_compose[@]}" up -d --wait --wait-timeout 120 db
if "${failed_compose[@]}" up -d warexo; then
    echo 'FAIL: application startup succeeded despite a wrong source host key.' >&2
    exit 1
fi
[[ -z $("${failed_compose[@]}" ps --status running -q warexo) ]]
docker run --rm --entrypoint sh -v "${test_project}-failed_warexo_state:/state:ro" "$WAREXO_RUNTIME_IMAGE" \
    -c 'test ! -e /state/clone-ready && grep -q "Host key verification failed" /state/clone-error.log'
SOURCE_SSH_KNOWN_HOSTS=$correct_host_key

# Preserve edits across a restart with the source machines completely removed.
"${target_compose[@]}" exec -T warexo python3 - <<'PY'
import os
from pathlib import Path
import pymysql
db = pymysql.connect(host=os.environ['DB_HOST'], user=os.environ['DB_USER'],
                     password=os.environ['DB_PASSWORD'], database=os.environ['DB_NAME'])
with db.cursor() as cursor:
    cursor.execute("UPDATE clone_fixture SET value='retained local edit' WHERE id=1")
db.commit()
Path('/var/www/html/web/local-edit.txt').write_text('retained local file')
PY
"${source_compose[@]}" down
"${target_compose[@]}" run --rm --no-deps clone
if [[ ${WAREXO_TEST_PORTING:-0} == 1 ]]; then
    # Reuse cannot need root credentials or source access after initialization.
    "${target_compose[@]}" run --rm --no-deps -e DB_ADMIN_PASSWORD=deliberately-wrong porting-init
fi
"${target_compose[@]}" restart warexo
"${target_compose[@]}" up -d --wait --wait-timeout 180 warexo gateway
"${target_compose[@]}" exec -T warexo python3 - <<'PY'
import os
from pathlib import Path
import pymysql
db = pymysql.connect(host=os.environ['DB_HOST'], user=os.environ['DB_USER'],
                     password=os.environ['DB_PASSWORD'], database=os.environ['DB_NAME'])
with db.cursor() as cursor:
    cursor.execute('SELECT value FROM clone_fixture WHERE id=1')
    assert cursor.fetchone()[0] == 'retained local edit'
assert Path('/var/www/html/web/local-edit.txt').read_text() == 'retained local file'
assert Path('/var/lib/warexo/clone-app-ready').exists()
PY
if [[ ${WAREXO_TEST_PORTING:-0} == 1 ]]; then
    verify_porting_restart
fi
echo 'PASS: SSH import, sanitization, source preservation, egress isolation, failed startup and source-independent restart.'
