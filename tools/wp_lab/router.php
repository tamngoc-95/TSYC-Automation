<?php
// php -S router for the TSYC lab: static files as is, everything else through WordPress.
$path = parse_url($_SERVER['REQUEST_URI'], PHP_URL_PATH);
$file = $_SERVER['DOCUMENT_ROOT'] . $path;
if ($path !== '/' && is_file($file) && substr($file, -4) !== '.php') {
    return false;
}
if ($path !== '/' && is_file($file)) {
    $_SERVER['SCRIPT_NAME'] = $path;
    require $file;
    return;
}
$_SERVER['SCRIPT_NAME'] = '/index.php';
require $_SERVER['DOCUMENT_ROOT'] . '/index.php';
