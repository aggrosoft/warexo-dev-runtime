#!/usr/bin/env bash
set -euo pipefail
fixture_root='/srv/warexo instance'
mkdir -p /run/sshd /root/.ssh "$fixture_root"/{app/config,vendor/composer,web/uploads}
install -m 0600 /fixture/source_key.pub /root/.ssh/authorized_keys
chmod 0700 /root/.ssh
passwd -d root >/dev/null
ssh-keygen -A
printf '%s\n' 'PermitRootLogin prohibit-password' 'PasswordAuthentication no' 'UsePAM no' > /etc/ssh/sshd_config.d/fixture.conf
printf '%s\n' '{"name":"fixture/warexo"}' > "$fixture_root/composer.json"
printf '%s\n' '{"packages":[]}' > "$fixture_root/composer.lock"
printf '%s\n' '<?php class AppKernel {}' > "$fixture_root/app/AppKernel.php"
printf '%s\n' '<?php return true;' > "$fixture_root/vendor/autoload.php"
printf '%s\n' '[]' > "$fixture_root/vendor/composer/installed.json"
printf '%s\n' 'deployed vendor patch must survive' > "$fixture_root/vendor/custom-patch.txt"
printf '%s\n' 'synthetic uploaded document' > "$fixture_root/web/uploads/customer.txt"
printf '%s\n' '<?php echo "installer must not survive";' > "$fixture_root/web/install.php"
printf '%s\n' '<?php echo "warexo clone fixture";' > "$fixture_root/web/app.php"
cat > "$fixture_root/web/.htaccess" <<'HTACCESS'
RewriteEngine On
RewriteCond %{REQUEST_FILENAME} !-f
RewriteRule ^ app.php [QSA,L]
HTACCESS
cat > "$fixture_root/app/console" <<'PHP'
<?php
if (array_slice($argv, 1) !== ['cache:warmup', '--env=prod']) { exit(91); }
if (file_put_contents(__DIR__ . '/warmup-ran', "local warmup\n") === false) { exit(92); }
PHP
cat > "$fixture_root/app/config/parameters.yml" <<'YAML'
parameters:
  database_driver: pdo_mysql
  database_host: source-db
  database_port: 3306
  database_name: warexo_source
  database_user: root
  database_password: source-fixture-password
YAML
cat > "$fixture_root/app/config/security.yml" <<'YAML'
security:
  encoders:
    'FOS\UserBundle\Model\UserInterface': sha512
YAML
/usr/sbin/sshd -t
touch /run/source-ready
exec /usr/sbin/sshd -D -e
