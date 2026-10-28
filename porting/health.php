<?php
// Runtime health only: never bootstrap Warexo or expose configuration.
header('Content-Type: text/plain');
if (PHP_VERSION_ID < 80400 || PHP_VERSION_ID >= 80500) {
    http_response_code(503);
    exit('wrong-runtime');
}
echo "php84-ready\n";
