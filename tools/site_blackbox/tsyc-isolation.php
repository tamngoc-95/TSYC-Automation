<?php
/**
 * TSYC black-box package: is this WordPress an isolated test copy?
 *
 * READ-ONLY. It reads constants, two rows of the options table and the
 * bridge file's hash. It writes nothing and sends nothing.
 *
 * Fail closed: the copy counts as isolated only when EVERY check below
 * passes. Anything missing or unreadable is a failed check, never a pass.
 *
 *   I1 HOST_NOT_PRODUCTION   home and site URL (as WordPress uses them AND as
 *                            stored in the database) name no production host
 *   I2 ENVIRONMENT_TYPE      WP_ENVIRONMENT_TYPE is "local" or "staging"
 *   I3 OUTBOUND_HTTP_BLOCKED WP_HTTP_BLOCK_EXTERNAL is true and
 *                            WP_ACCESSIBLE_HOSTS opens no wildcard and no
 *                            production host, so webhooks, payment and sync
 *                            calls through WordPress cannot leave the copy
 *   I4 OPERATOR_ATTESTATION  TSYC_BLACKBOX_ISOLATED_COPY equals this copy's
 *                            own host: the developer states in wp-config that
 *                            this exact host is an isolated copy with mail,
 *                            payments and outbound sync switched off. A
 *                            wp-config carried to another host stops matching
 *
 * Run on its own (prints one JSON line, exit 0 isolated, 5 not isolated):
 *   wp eval-file tsyc-isolation.php
 * Other package files load it with require_once and call tsyc_bbx_isolation().
 */

if (!defined('ABSPATH')) {
    fwrite(STDERR, "Run through WP-CLI: wp eval-file tsyc-isolation.php\n");
    exit(1);
}

if (!function_exists('tsyc_bbx_isolation')) {
    function tsyc_bbx_production_hosts(): array
    {
        return array('tiemsachyeucon.com', 'www.tiemsachyeucon.com');
    }

    function tsyc_bbx_host_of(string $url): string
    {
        return strtolower((string) wp_parse_url($url, PHP_URL_HOST));
    }

    function tsyc_bbx_is_production_host(string $host): bool
    {
        return in_array(strtolower($host), tsyc_bbx_production_hosts(), true);
    }

    /** SHA-256 of the bridge file actually loaded, line endings normalized. '' when absent. */
    function tsyc_bbx_bridge_sha256(): string
    {
        if (!function_exists('tsyc_bridge_writes_enabled')) {
            return '';
        }
        $file = (new ReflectionFunction('tsyc_bridge_writes_enabled'))->getFileName();
        $text = is_string($file) && is_readable($file) ? file_get_contents($file) : false;
        return is_string($text) ? hash('sha256', str_replace("\r\n", "\n", $text)) : '';
    }

    function tsyc_bbx_isolation(): array
    {
        global $wpdb;
        $checks = array();
        $add = function (string $id, string $name, bool $ok, string $detail) use (&$checks) {
            $checks[] = array('id' => $id, 'name' => $name, 'ok' => $ok, 'detail' => $detail);
        };

        // I1: every way WordPress knows its own address.
        $hosts = array('home_url' => tsyc_bbx_host_of(home_url()), 'site_url' => tsyc_bbx_host_of(site_url()));
        $stored = $wpdb->get_results("SELECT option_name, option_value FROM {$wpdb->options} WHERE option_name IN ('home', 'siteurl')");
        foreach ((array) $stored as $row) {
            $hosts['stored_' . $row->option_name] = tsyc_bbx_host_of((string) $row->option_value);
        }
        $bad = array();
        foreach ($hosts as $where => $host) {
            if ($host === '' || tsyc_bbx_is_production_host($host)) {
                $bad[] = $where . '=' . ($host === '' ? '(none)' : $host);
            }
        }
        if (count($hosts) < 4) {
            $bad[] = 'home/siteurl rows not readable from the options table';
        }
        $add('I1', 'HOST_NOT_PRODUCTION', !$bad,
            $bad ? 'Refused: ' . implode(', ', $bad) : 'Hosts: ' . implode(', ', array_unique(array_values($hosts))));

        // I2
        $environment = function_exists('wp_get_environment_type') ? wp_get_environment_type() : '';
        $add('I2', 'ENVIRONMENT_TYPE', in_array($environment, array('local', 'staging'), true),
            "WP_ENVIRONMENT_TYPE is '$environment' (needs local or staging).");

        // I3
        $blocked = defined('WP_HTTP_BLOCK_EXTERNAL') && WP_HTTP_BLOCK_EXTERNAL === true;
        $open = array();
        if (defined('WP_ACCESSIBLE_HOSTS')) {
            foreach (preg_split('/[\s,]+/', (string) WP_ACCESSIBLE_HOSTS, -1, PREG_SPLIT_NO_EMPTY) as $allowed) {
                $allowed = strtolower($allowed);
                $matches_production = false;
                foreach (tsyc_bbx_production_hosts() as $production) {
                    if (fnmatch($allowed, $production)) {
                        $matches_production = true;
                    }
                }
                if ($allowed === '*' || $matches_production) {
                    $open[] = $allowed;
                }
            }
        }
        $add('I3', 'OUTBOUND_HTTP_BLOCKED', $blocked && !$open,
            !$blocked ? "WP_HTTP_BLOCK_EXTERNAL is not true: the copy could call real services (webhooks, payments, sync)."
                      : ($open ? 'WP_ACCESSIBLE_HOSTS opens: ' . implode(', ', $open) : 'Outbound HTTP through WordPress is blocked.'));

        // I4
        $attested = defined('TSYC_BLACKBOX_ISOLATED_COPY') ? strtolower(trim((string) TSYC_BLACKBOX_ISOLATED_COPY)) : '';
        $ok = $attested !== '' && $attested === $hosts['home_url'] && !tsyc_bbx_is_production_host($attested);
        $add('I4', 'OPERATOR_ATTESTATION', $ok,
            $attested === '' ? 'TSYC_BLACKBOX_ISOLATED_COPY is not set in wp-config.php.'
                             : ($ok ? "Attested for $attested." : "TSYC_BLACKBOX_ISOLATED_COPY is '$attested', this copy is '{$hosts['home_url']}'."));

        $isolated = true;
        foreach ($checks as $check) {
            $isolated = $isolated && $check['ok'];
        }
        return array(
            'isolated' => $isolated,
            'checks' => $checks,
            'bridge_version' => defined('TSYC_BRIDGE_VERSION') ? (string) TSYC_BRIDGE_VERSION : '',
            'bridge_sha256' => tsyc_bbx_bridge_sha256(),
            'wp_cron_disabled' => defined('DISABLE_WP_CRON') && DISABLE_WP_CRON,
        );
    }
}

if (!defined('TSYC_BBX_ISOLATION_LIBRARY')) {
    $tsyc_bbx_result = tsyc_bbx_isolation();
    echo wp_json_encode(array('tsyc_isolation' => 1) + $tsyc_bbx_result) . "\n";
    exit($tsyc_bbx_result['isolated'] ? 0 : 5);
}
