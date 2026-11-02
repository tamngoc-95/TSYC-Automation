<?php
/**
 * Plugin Name: TSYC lab guards (local lab only, never deploy)
 * Description: Refuses to load outside the local lab, disables mail, and exposes the fault-injection points used by the pilot.
 */

if (!defined('ABSPATH')) {
    exit;
}

// A local-only safety net: this file must never run on a real site.
$tsyc_lab_host = (string) wp_parse_url((string) (defined('WP_HOME') ? WP_HOME : ''), PHP_URL_HOST);
if ($tsyc_lab_host !== '127.0.0.1' || !function_exists('wp_get_environment_type') || wp_get_environment_type() !== 'local') {
    return;
}

// No mail leaves the lab.
add_filter('pre_wp_mail', '__return_false');

// Fault injection: the pilot asks for the request to die at one bridge
// checkpoint, the way a lost connection or crashed worker would.
add_action('tsyc_bridge_checkpoint', static function (string $stage): void {
    $wanted = isset($_SERVER['HTTP_X_TSYC_LAB_FAULT']) ? (string) $_SERVER['HTTP_X_TSYC_LAB_FAULT'] : '';
    if ($wanted !== '' && $wanted === $stage) {
        status_header(502);
        exit('lab fault at ' . $stage);
    }
});
