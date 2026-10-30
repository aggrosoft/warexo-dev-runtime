#!/usr/bin/env bash
set -euo pipefail

case "${WAREXO_PORTING_ENABLED:-0}" in
    0|false)
        # Also disable our vhost after an explicit on -> off container restart.
        rm -f /etc/apache2/sites-enabled/warexo-porting.conf
        exec /usr/local/bin/warexo-entrypoint "$@"
        ;;
    1|true) ;;
    *) echo '[warexo-porting] WAREXO_PORTING_ENABLED must be 0/1 or false/true.' >&2; exit 1 ;;
esac

if [[ "${WAREXO_BOOTSTRAP_MODE:-fresh}" != clone ]]; then
    echo '[warexo-porting] The optional porting workspace requires clone mode.' >&2
    exit 1
fi
if [[ ! -f /workspace/.warexo-porting-ready.json ]]; then
    echo '[warexo-porting] No prepared workspace. Run the separate porting-init service first.' >&2
    exit 1
fi
python3 /opt/warexo/porting/workspace.py --verify-ready

mkdir -p /workspace/.runtime/php84/{sessions,tmp,cache,logs}
chown developer:developer /workspace/.runtime/php84 /workspace/.runtime/php84/{sessions,tmp,cache,logs}
a2enmod proxy proxy_fcgi
# Only the PHP 8.4 vhost changes. Keep PHP 7.4 and its deployed files on port 80.
case "${WAREXO_PORTING_TARGET:-next}" in
    next) porting_vhost=/opt/warexo/porting/apache84.conf ;;
    aggrowawi) porting_vhost=/opt/warexo/porting/apache84-legacy.conf ;;
    *) echo '[warexo-porting] WAREXO_PORTING_TARGET must be next or aggrowawi.' >&2; exit 1 ;;
esac
cp "$porting_vhost" /etc/apache2/sites-available/warexo-porting.conf
ln -sf /etc/apache2/sites-available/warexo-porting.conf /etc/apache2/sites-enabled/warexo-porting.conf

# Separate process groups allow shutdown during Legacy warmup as well as while
# Apache/FPM are serving. Nothing invokes the imported application under PHP 8.4.
fpm_pid=""
apache_pid=""
shutdown() {
    trap '' TERM INT HUP QUIT
    [[ -z "$fpm_pid" ]] || kill -TERM -- "-$fpm_pid" 2>/dev/null || true
    [[ -z "$apache_pid" ]] || kill -TERM -- "-$apache_pid" 2>/dev/null || true
    [[ -z "$fpm_pid" ]] || wait "$fpm_pid" 2>/dev/null || true
    [[ -z "$apache_pid" ]] || wait "$apache_pid" 2>/dev/null || true
}
trap 'shutdown; exit 143' TERM
trap 'shutdown; exit 130' INT
trap 'shutdown; exit 129' HUP
trap 'shutdown; exit 131' QUIT

setsid env -u PHPRC -u PHP_INI_SCAN_DIR \
    PHP_INI_SCAN_DIR=/opt/php84/etc/conf.d \
    /opt/php84/sbin/php-fpm -c /opt/php84/etc/php.ini \
    --fpm-config /opt/php84/etc/php-fpm.conf --nodaemonize &
fpm_pid=$!
setsid /usr/local/bin/warexo-entrypoint "$@" &
apache_pid=$!
set +e
wait -n "$fpm_pid" "$apache_pid"
status=$?
shutdown
exit "$status"
