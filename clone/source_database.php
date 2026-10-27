<?php
declare(strict_types=1);

// Executed through SSH stdin. Never loads Warexo, its vendor tree or its configuration as PHP.
// Only this placeholder is replaced locally; connection secrets never enter process arguments.
$configuration = json_decode(base64_decode('__WAREXO_CLONE_CONFIG__', true), true, 512, JSON_THROW_ON_ERROR);

class SourceError extends RuntimeException {}

function runTool(array $command, array $environment, bool $stream = false): string
{
    $descriptors = [0 => ['file', '/dev/null', 'r'], 1 => $stream ? ['file', 'php://stdout', 'w'] : ['pipe', 'w'], 2 => ['file', '/dev/null', 'a']];
    $process = proc_open($command, $descriptors, $pipes, null, $environment);
    if (!is_resource($process)) { throw new SourceError('Could not start the source database client.'); }
    $output = '';
    if (!$stream) { $output = stream_get_contents($pipes[1]); fclose($pipes[1]); }
    if (proc_close($process) !== 0) { throw new SourceError('Source database client failed: check configuration and read privileges, including routine/event definitions.'); }
    return $output;
}

function executable(array $names): string
{
    foreach (array_merge(['/usr/bin', '/usr/local/bin'], explode(PATH_SEPARATOR, getenv('PATH') ?: '')) as $directory) {
        if ($directory === '' || $directory[0] !== '/') { continue; }
        foreach ($names as $name) {
            $file = $directory . '/' . $name;
            if (is_file($file) && is_executable($file)) { return $file; }
        }
    }
    throw new SourceError('Required mysql/mariadb client or dump executable is unavailable on the source.');
}

function inspectFiles(string $root): array
{
    $hashes = [];
    foreach (['composer.json', 'composer.lock', 'app/console', 'app/AppKernel.php', 'vendor/autoload.php', 'vendor/composer/installed.json'] as $relative) {
        if (!is_file($root . '/' . $relative)) { throw new SourceError('Source is missing a required Warexo bootstrap, lock or installed-vendor file.'); }
        $hashes[$relative] = hash_file('sha256', $root . '/' . $relative);
    }
    $pending = [$root];
    while ($pending) {
        $directory = array_pop($pending);
        foreach (new DirectoryIterator($directory) as $entry) {
            if ($entry->isDot()) { continue; }
            $name = $entry->getFilename();
            $path = $entry->getPathname();
            $relative = substr($path, strlen($root) + 1);
            if (in_array($name, ['.git', '.ssh', 'node_modules'], true)
                || in_array($relative, ['backups', 'app/cache', 'app/logs', 'app/temp'], true)
                || preg_match('~^(app/config/parameters\.yml|\.env|web/(install\.php|app_dev\.php|restore))~', $relative)
                || preg_match('~\.(pem|key)$~', $name)) { continue; }
            if ($entry->isLink()) {
                $target = readlink($path);
                $resolved = realpath($path);
                if ($target === false || $target === '' || $target[0] === '/' || $resolved === false
                    || strpos($resolved, $root . '/') !== 0) {
                    throw new SourceError('Source has an external, absolute or broken symlink; prepare a self-contained copy with relative internal links.');
                }
                // Match rsync --safe-links: reject a target that traverses above the
                // transfer root even if a later component happens to re-enter it.
                $parent = dirname($relative);
                $depth = $parent === '.' ? 0 : count(explode('/', $parent));
                foreach (explode('/', $target) as $part) {
                    if ($part === '' || $part === '.') { continue; }
                    $depth += $part === '..' ? -1 : 1;
                    if ($depth < 0) { throw new SourceError('A source symlink traverses above the transfer root; replace it with an internal relative link.'); }
                }
            } elseif ($entry->isDir()) {
                $pending[] = $path;
            } elseif (!$entry->isFile()) {
                throw new SourceError('Source contains an unsupported filesystem entry, such as a device or socket.');
            }
        }
    }
    return $hashes;
}

function batchField(string $value): string
{
    // The client's non-raw batch format escapes control characters and backslashes.
    // strtr replaces once, so a literal backslash followed by 'n' stays literal.
    return strtr($value, ['\\\\' => '\\', '\\n' => "\n", '\\t' => "\t", '\\r' => "\r",
                         '\\0' => "\0", '\\b' => "\x08", '\\Z' => "\x1a"]);
}

function inspectEvents(callable $query): array
{
    $events = [];
    $names = $query('SELECT HEX(EVENT_NAME) FROM information_schema.EVENTS WHERE EVENT_SCHEMA=DATABASE() ORDER BY EVENT_NAME');
    foreach ($names === '' ? [] : explode("\n", $names) as $encodedName) {
        if (!preg_match('/\A(?:[0-9A-Fa-f]{2})+\z/', $encodedName)) {
            throw new SourceError('Cannot decode source event metadata.');
        }
        $name = hex2bin($encodedName);
        $row = $query('SHOW CREATE EVENT `' . str_replace('`', '``', $name) . '`', false);
        $columns = explode("\t", $row);
        if (count($columns) < 4 || strpos($row, "\n") !== false || batchField($columns[0]) !== $name
            || $columns[3] === '' || $columns[3] === 'NULL') {
            throw new SourceError('Cannot read a source event definition; check event privileges.');
        }
        $events[] = ['name' => $name, 'create_statement' => batchField($columns[3])];
    }
    return $events;
}

try {
    if (PHP_VERSION_ID < 70400) { throw new SourceError('Source PHP CLI must be version 7.4 or newer; select it with SOURCE_PHP_BIN.'); }
    $root = realpath($configuration['root']);
    if ($root === false || $root === '/' || !is_dir($root)) { throw new SourceError('SOURCE_WAREXO_PATH is not an accessible application directory.'); }
    $database = $configuration['database'];
    $client = executable(['mariadb', 'mysql']);
    $dump = executable(['mariadb-dump', 'mysqldump']);
    $environment = array_replace(getenv(), ['MYSQL_PWD' => $database['password'], 'MYSQL_TEST_LOGIN_FILE' => '/dev/null']);
    $options = ['--no-defaults', '--user=' . $database['user']];
    if ($database['socket'] !== null) {
        $options[] = '--protocol=SOCKET'; $options[] = '--socket=' . $database['socket'];
    } else {
        $options[] = '--host=' . $database['host']; $options[] = '--port=' . $database['port'];
    }
    $query = static function (string $sql, bool $raw = true) use ($client, $options, $environment, $database): string {
        $arguments = ['--batch', '--skip-column-names', '--default-character-set=utf8mb4', '--database=' . $database['name'], '--execute=' . $sql];
        if ($raw) { $arguments[] = '--raw'; }
        return rtrim(runTool(array_merge([$client], $options, $arguments), $environment), "\r\n");
    };
    if ($query("SELECT COUNT(*) FROM information_schema.tables WHERE table_schema=DATABASE() AND TABLE_TYPE='BASE TABLE' AND COALESCE(ENGINE,'') <> 'InnoDB'") !== '0') {
        throw new SourceError('Non-InnoDB source tables require a separately prepared consistent snapshot; no locking dump was attempted.');
    }
    if ($configuration['operation'] === 'inspect') {
        $server = explode("\t", $query('SELECT VERSION(), @@sql_mode, @@lower_case_table_names, @@time_zone'));
        $tables = (int) $query("SELECT COUNT(*) FROM information_schema.tables WHERE table_schema=DATABASE() AND TABLE_TYPE='BASE TABLE'");
        if ($tables === 0) { throw new SourceError('Source database has no visible base tables; verify the database and read privileges.'); }
        echo json_encode([
            'files' => inspectFiles($root),
            'database_version' => $server[0] ?? '', 'sql_mode' => $server[1] ?? '',
            'lower_case_table_names' => $server[2] ?? '', 'time_zone' => $server[3] ?? '',
            'base_tables' => $tables,
            // Reference only: a server with event_scheduler=DISABLED rejects event DDL.
            'events' => inspectEvents($query),
        ], JSON_THROW_ON_ERROR);
    } elseif ($configuration['operation'] === 'dump') {
        $version = runTool([$dump, '--no-defaults', '--version'], $environment);
        $flags = ['--single-transaction', '--quick', '--skip-lock-tables', '--no-tablespaces', '--hex-blob', '--skip-comments',
                  '--default-character-set=utf8mb4', '--routines', '--skip-events', '--triggers'];
        if (stripos($version, 'MariaDB') === false) {
            $flags[] = '--set-gtid-purged=OFF';
            if (preg_match('/\bVer\s+8\./', $version)) { $flags[] = '--column-statistics=0'; }
        }
        runTool(array_merge([$dump], $options, $flags, ['--', $database['name']]), $environment, true);
    } else {
        throw new SourceError('Unknown read-only source operation.');
    }
} catch (Throwable $error) {
    // No raw driver errors, database values, credentials or source paths in the log.
    $message = $error instanceof SourceError ? $error->getMessage() : 'Unexpected source inspection failure; check PHP version, filesystem permissions and configuration.';
    file_put_contents('php://stderr', 'Source snapshot failed: ' . $message . "\n");
    exit(1);
}
