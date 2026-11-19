<?php
/**
 * TSYC black-box driver: one synthetic pre-order trio through the TSYC Polylang bridge.
 *
 * THIS FILE WRITES TEST PRODUCTS. It refuses to run unless ALL of these hold:
 *   - every isolation check in tsyc-isolation.php passes (fail closed): no
 *     production host anywhere in the site's address, WP_ENVIRONMENT_TYPE
 *     "local" or "staging", outbound HTTP blocked, and the developer's
 *     TSYC_BLACKBOX_ISOLATED_COPY attestation naming this exact host;
 *   - the site host is not tiemsachyeucon.com (checked again here);
 *   - the TSYC bridge is installed with its write routes enabled, which the
 *     bridge itself only allows off production;
 *   - it runs through WP-CLI as a user who may edit products.
 *
 * It needs no network, no TSYC repository and no source code of the site.
 *
 *   wp eval-file tsyc-bridge-trio-driver.php create --user=ADMIN_LOGIN
 *       One synthetic VI draft (stock 0, backorders allowed, no price), then
 *       an EN and a DE draft through the bridge's own create route.
 *   wp eval-file tsyc-bridge-trio-driver.php create-vi --user=ADMIN_LOGIN
 *   wp eval-file tsyc-bridge-trio-driver.php translate VI_ID --user=ADMIN_LOGIN
 *       The same in two steps, so a snapshot can be taken in between.
 *   wp eval-file tsyc-bridge-trio-driver.php cleanup VI_ID --user=ADMIN_LOGIN
 *       Moves that synthetic trio to the trash. Only products this driver made.
 *
 * It never sets a price, never publishes, and touches no existing product.
 */

if (!defined('ABSPATH') || !defined('WP_CLI') || !WP_CLI) {
    fwrite(STDERR, "Run through WP-CLI: wp eval-file tsyc-bridge-trio-driver.php create --user=ADMIN_LOGIN\n");
    exit(1);
}

const TSYC_BBX_SKU_PREFIX = 'TSYC-BBX-';
const TSYC_BBX_PRODUCTION_HOSTS = array('tiemsachyeucon.com');

function tsyc_bbx_stop(string $code, string $message): void
{
    echo wp_json_encode(array('tsyc_driver' => 1, 'ok' => false, 'code' => $code, 'message' => $message)) . "\n";
    exit(3);
}

function tsyc_bbx_guard(): void
{
    $library = __DIR__ . '/tsyc-isolation.php';
    if (!is_readable($library)) {
        tsyc_bbx_stop('NOT_ISOLATED', 'tsyc-isolation.php is missing next to this file, so isolation cannot be established.');
    }
    if (!defined('TSYC_BBX_ISOLATION_LIBRARY')) {
        define('TSYC_BBX_ISOLATION_LIBRARY', true);
    }
    require_once $library;
    $isolation = tsyc_bbx_isolation();
    if ($isolation['isolated'] !== true) {
        $failed = array();
        foreach ($isolation['checks'] as $check) {
            if (!$check['ok']) {
                $failed[] = $check['id'] . ' ' . $check['name'] . ': ' . $check['detail'];
            }
        }
        tsyc_bbx_stop('NOT_ISOLATED', 'Not an established isolated copy. Nothing was written. ' . implode(' | ', $failed));
    }
    $host = preg_replace('/^www\./', '', strtolower((string) wp_parse_url(home_url(), PHP_URL_HOST)));
    if ($host === '' || in_array($host, TSYC_BBX_PRODUCTION_HOSTS, true)) {
        tsyc_bbx_stop('PRODUCTION_HOST_REFUSED', 'This driver never runs on the production host.');
    }
    if (!in_array(wp_get_environment_type(), array('local', 'staging'), true)) {
        tsyc_bbx_stop('ENVIRONMENT_REFUSED', 'WP_ENVIRONMENT_TYPE must be local or staging (it is ' . wp_get_environment_type() . ').');
    }
    if (!function_exists('wc_get_product') || !function_exists('pll_get_post_translations')) {
        tsyc_bbx_stop('DEPENDENCY_MISSING', 'WooCommerce and Polylang must both be active.');
    }
    if (!function_exists('tsyc_bridge_writes_enabled') || !tsyc_bridge_writes_enabled()) {
        tsyc_bbx_stop('BRIDGE_WRITES_OFF', 'The TSYC bridge is not installed or its write routes are not enabled on this copy.');
    }
    if (!current_user_can('edit_products')) {
        tsyc_bbx_stop('NO_USER', 'Pass --user=ADMIN_LOGIN (a user who may edit products).');
    }
}

function tsyc_bbx_bridge_post(string $route, array $body): array
{
    // In-process call: there is no network leg, so the bridge's HTTPS rule has nothing to protect.
    $_SERVER['HTTPS'] = 'on';
    $request = new WP_REST_Request('POST', '/tsyc/v1/' . $route);
    $request->set_header('Content-Type', 'application/json');
    $request->set_header('X-TSYC-Write-Token', (string) TSYC_BRIDGE_WRITE_TOKEN);
    $request->set_body(wp_json_encode($body));
    $response = rest_do_request($request);
    // The bridge frees its group lock when the PHP process ends, which for a web
    // request is right after the answer. This process goes on to the next
    // language, so the finished request's lock is freed here the same way: with
    // the bridge's own function and the holder's own token.
    if (isset($body['source_id']) && function_exists('tsyc_bridge_lock_key')) {
        global $wpdb;
        $key = tsyc_bridge_lock_key((int) $body['source_id']);
        $held = $wpdb->get_var($wpdb->prepare("SELECT option_value FROM {$wpdb->options} WHERE option_name = %s", $key));
        if (is_string($held) && $held !== '') {
            tsyc_bridge_release_lock($key, $held);
        }
    }
    return array('status' => $response->get_status(), 'data' => $response->get_data());
}

tsyc_bbx_guard();
$tsyc_bbx_command = isset($args[0]) ? (string) $args[0] : '';

/** EN then DE through the bridge. A refusal or a side-effect stop ends it: nothing is retried or repaired. */
function tsyc_bbx_translate(int $vi, string $code, string $internal, string $run, array $out): array
{
    foreach (array('en', 'de') as $lang) {
        clean_post_cache($vi);
        $modified = str_replace(' ', 'T', get_post($vi)->post_modified_gmt);
        $answer = tsyc_bbx_bridge_post('translations', array(
            'source_id' => $vi,
            'lang' => $lang,
            'request_id' => 'tsyc-bbx-' . $lang . '-' . $run,
            'authorization_id' => 'blackbox-site-copy-test',
            'internal_product_id' => $internal,
            'product_code' => $code,
            'expected_source_modified_gmt' => $modified,
            'inventory_profile' => 'FAHASA_PREORDER',
            'content' => array(
                'name' => 'TSYC blackbox test book ' . $run . ' (' . $lang . ')',
                'description' => '<p>Synthetic test text (' . $lang . ').</p>',
                'short_description' => 'Synthetic test (' . $lang . ').',
            ),
        ));
        $data = is_array($answer['data']) ? $answer['data'] : array();
        $out['answers'][$lang] = array('http_status' => $answer['status'],
                                       'code' => isset($data['code']) ? $data['code'] : null,
                                       'message' => isset($data['message']) ? $data['message'] : null,
                                       'changes' => isset($data['data']['changes']) ? $data['data']['changes'] : null);
        if ($answer['status'] === 201 && isset($data['product_id'])) {
            $out['trio'][$lang] = (int) $data['product_id'];
        } else {
            // A refusal or side-effect stop is the finding: nothing is retried or repaired.
            $out['ok'] = false;
            break;
        }
    }
    return $out;
}

if ($tsyc_bbx_command === 'translate') {
    $vi = isset($args[1]) ? (int) $args[1] : 0;
    $source = $vi > 0 ? wc_get_product($vi) : null;
    $code = $source ? (string) $source->get_sku('edit') : '';
    if (!$source || strpos($code, TSYC_BBX_SKU_PREFIX) !== 0) {
        tsyc_bbx_stop('NOT_A_DRIVER_PRODUCT', "#$vi was not made by this driver (SKU prefix " . TSYC_BBX_SKU_PREFIX . ').');
    }
    $out = tsyc_bbx_translate($vi, $code, (string) get_post_meta($vi, '_tsyc_internal_product_id', true),
        substr($code, strlen(TSYC_BBX_SKU_PREFIX)),
        array('tsyc_driver' => 1, 'ok' => true, 'product_code' => $code, 'trio' => array('vi' => $vi), 'answers' => array()));
    echo wp_json_encode($out) . "\n";
    exit($out['ok'] ? 0 : 4);
}

if ($tsyc_bbx_command === 'create' || $tsyc_bbx_command === 'create-vi') {
    $run = gmdate('Ymd-His');
    $code = TSYC_BBX_SKU_PREFIX . $run;
    $internal = wp_generate_uuid4();
    $product = new WC_Product_Simple();
    $product->set_name('TSYC blackbox test book ' . $run);
    $product->set_status('draft');
    $product->set_sku($code);
    $product->set_description('<p>Synthetic test text (vi).</p>');
    $product->set_short_description('Synthetic test (vi).');
    $product->set_manage_stock(true);
    $product->set_stock_quantity(0);
    $product->set_backorders('yes');
    $product->update_meta_data('_tsyc_internal_product_id', $internal);
    $product->update_meta_data('_tsyc_product_code', $code);
    $vi = $product->save();
    if (!pll_get_post_language($vi)) {
        pll_set_post_language($vi, pll_default_language());
    }
    $out = array('tsyc_driver' => 1, 'ok' => true, 'product_code' => $code, 'internal_product_id' => $internal,
                 'trio' => array('vi' => $vi), 'answers' => array());
    if ($tsyc_bbx_command === 'create') {
        $out = tsyc_bbx_translate($vi, $code, $internal, $run, $out);
    }
    echo wp_json_encode($out) . "\n";
    exit($out['ok'] ? 0 : 4);
}

if ($tsyc_bbx_command === 'cleanup') {
    $vi = isset($args[1]) ? (int) $args[1] : 0;
    $source = $vi > 0 ? wc_get_product($vi) : null;
    if (!$source || strpos((string) $source->get_sku('edit'), TSYC_BBX_SKU_PREFIX) !== 0) {
        tsyc_bbx_stop('NOT_A_DRIVER_PRODUCT', "#$vi was not made by this driver (SKU prefix " . TSYC_BBX_SKU_PREFIX . ').');
    }
    $internal = (string) get_post_meta($vi, '_tsyc_internal_product_id', true);
    $trashed = array();
    $skipped = array();
    foreach ((array) pll_get_post_translations($vi) as $lang => $member) {
        $member = (int) $member;
        if ($internal !== '' && (string) get_post_meta($member, '_tsyc_internal_product_id', true) === $internal) {
            wp_trash_post($member);
            $trashed[$lang] = $member;
        } else {
            $skipped[$lang] = $member;
        }
    }
    echo wp_json_encode(array('tsyc_driver' => 1, 'ok' => true, 'trashed' => $trashed, 'left_alone' => $skipped)) . "\n";
    exit(0);
}

tsyc_bbx_stop('USAGE', 'Commands: create | create-vi | translate VI_ID | cleanup VI_ID');
