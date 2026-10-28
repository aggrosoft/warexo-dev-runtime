#!/usr/bin/env bash
# Sourced only by the synthetic clone integration fixture, never a live stack.
set -euo pipefail

verify_porting_initial() {
    # The ordinary image stays small and does not gain a second PHP runtime.
    docker run --rm --entrypoint sh "$WAREXO_RUNTIME_IMAGE" -c '! command -v php84 && test ! -e /opt/php84'
    "${target_compose[@]}" exec -T warexo php74 -r 'exit(PHP_MAJOR_VERSION === 7 && PHP_MINOR_VERSION === 4 ? 0 : 1);'
    "${target_compose[@]}" exec -T warexo php -r 'exit(PHP_MAJOR_VERSION === 7 && PHP_MINOR_VERSION === 4 ? 0 : 1);'
    "${target_compose[@]}" exec -T warexo php84 -r 'exit(PHP_MAJOR_VERSION === 8 && PHP_MINOR_VERSION === 4 ? 0 : 1);'
    "${target_compose[@]}" exec -T -w /workspace/warexo-port warexo composer84 --version
    # Database GRANT patterns must escape '_' rather than grant sibling schemas.
    "${target_compose[@]}" exec -T -e MYSQL_PWD="$SERVICE_PASSWORD_64_DBROOT" db \
        mariadb --no-defaults --user=root --execute='CREATE DATABASE warexoXport; CREATE TABLE warexoXport.guard (id INT);'
    "${target_compose[@]}" exec -T warexo python3 - < tests/integration/assert-porting.py
    local porting_address result
    porting_address=$("${target_compose[@]}" port porting-gateway 80)
    [[ $(curl --fail --silent --max-time 10 "http://$porting_address/__warexo_porting_health") == php84-ready ]]
    [[ $(curl --fail --silent --max-time 10 "http://$porting_address/") == 'warexo clone fixture' ]]
    [[ $(curl --silent -o /dev/null -w '%{http_code}' "http://$porting_address/install.php") == 404 ]]
    result=$(curl --fail --silent --max-time 10 "http://$porting_address/porting-check.php")
    python3 -c 'import json,sys; p=json.loads(sys.argv[1]); assert 80400 <= p["version"] < 80500; assert p["child"].startswith("8.4."); assert p["session"].startswith("/workspace/"); assert p["database"] == "warexo_port"; assert p["has_proxy"] is False' "$result"

    # The dependency proxy must actually work, not merely reject every request.
    "${target_compose[@]}" exec -T warexo warexo-net curl --fail --silent --show-error --max-time 45 \
        https://repo.packagist.org/packages.json >/dev/null
    "${target_compose[@]}" exec -T warexo warexo-net git ls-remote https://github.com/php/php-src.git HEAD >/dev/null
    for denied_url in https://example.com/ https://169.254.169.254/ https://private.fixture.invalid/; do
        result=$("${target_compose[@]}" exec -T warexo warexo-net curl --noproxy '' --silent \
            --max-time 10 --output /dev/null --write-out '%{http_connect}' "$denied_url" || true)
        [[ "$result" == 403 ]]
    done
    if "${target_compose[@]}" exec -T warexo curl --noproxy '*' --fail --silent --max-time 5 https://repo.packagist.org/packages.json >/dev/null; then
        echo 'FAIL: optional workspace gained unrestricted direct outbound connectivity.' >&2
        return 1
    fi
    # --verify-ready is available without root credentials or reference mounts.
    "${target_compose[@]}" exec -T warexo python3 /opt/warexo/porting/workspace.py --verify-ready
    echo 'PASS: dual PHP, HTTP handlers, independent data, scoped grants and dependency-only egress.'
}

verify_porting_restart() {
    "${target_compose[@]}" exec -T warexo python3 - <<'PY'
import os
from pathlib import Path
import pymysql
db = pymysql.connect(host=os.environ['PORTING_DB_HOST'], user=os.environ['PORTING_DB_USER'],
                     password=os.environ['PORTING_DB_PASSWORD'], database=os.environ['PORTING_DB_NAME'])
with db.cursor() as cursor:
    cursor.execute('SELECT value FROM clone_fixture WHERE id=1')
    assert cursor.fetchone()[0] == 'retained porting edit'
db.close()
assert Path('/workspace/warexo-port/web/porting-edit.txt').read_text() == 'retained porting file'
assert not Path('/var/www/html/web/porting-edit.txt').exists()
assert not Path('/workspace/warexo-port/web/local-edit.txt').exists()
PY
    "${target_compose[@]}" exec -T warexo curl --fail --silent --max-time 5 http://127.0.0.1:8084/__warexo_porting_health
    # The opt-in image itself must also delegate unchanged when explicitly off.
    "${target_compose[@]}" run --rm --no-deps -e WAREXO_PORTING_ENABLED=0 warexo \
        bash -c 'test ! -e /etc/apache2/sites-enabled/warexo-porting.conf; php -r "exit(PHP_MAJOR_VERSION === 7 ? 0 : 1);"'
    echo 'PASS: porting edits survive restart/re-init without source/root access; optional startup can be disabled.'
}
