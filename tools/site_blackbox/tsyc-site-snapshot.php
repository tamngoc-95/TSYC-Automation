<?php
/**
 * TSYC read-only catalog snapshot (black-box test package).
 *
 * READ-ONLY: this file only SELECTs and reads. It contains no insert, update,
 * delete, save or option write, sends nothing over the network and reads no
 * theme or plugin source code.
 *
 * Run (either form), then keep the JSON file:
 *   wp eval-file tsyc-site-snapshot.php 00_baseline > 00_baseline.json
 *   php tsyc-site-snapshot.php --wp-load=/path/to/wp-load.php 00_baseline > 00_baseline.json
 *
 * Output: one JSON line starting with {"tsyc_snapshot": ...}. Per product:
 * status, prices, stock, backorders, SKU, images, language, translation
 * links, related product IDs, and SHA-256 hashes of the text (never the
 * text itself). No customer, order, user or key data is read.
 */

if (!defined('ABSPATH')) {
    // Plain PHP CLI: load WordPress from the path given.
    if (PHP_SAPI !== 'cli') {
        exit(1);
    }
    $tsyc_load = null;
    $tsyc_rest = array();
    foreach (array_slice($argv, 1) as $tsyc_argument) {
        if (strpos($tsyc_argument, '--wp-load=') === 0) {
            $tsyc_load = substr($tsyc_argument, 10);
        } else {
            $tsyc_rest[] = $tsyc_argument;
        }
    }
    if ($tsyc_load === null || !is_file($tsyc_load)) {
        fwrite(STDERR, "Usage: wp eval-file tsyc-site-snapshot.php LABEL\n   or: php tsyc-site-snapshot.php --wp-load=/path/to/wp-load.php LABEL\n");
        exit(1);
    }
    require $tsyc_load;
    $args = $tsyc_rest;
}

if (!function_exists('tsyc_snapshot_build')) {
    function tsyc_snapshot_meta(int $id, string $key): string
    {
        $value = get_post_meta($id, $key, true);
        return is_scalar($value) ? (string) $value : (string) wp_json_encode($value);
    }

    function tsyc_snapshot_ids($value): array
    {
        if (is_string($value)) {
            $value = $value === '' ? array() : explode(',', $value);
        }
        $ids = array();
        foreach ((array) $value as $item) {
            if ((int) $item > 0) {
                $ids[] = (int) $item;
            }
        }
        return $ids;
    }

    function tsyc_snapshot_build(string $label): array
    {
        global $wpdb;
        $has_polylang = function_exists('pll_get_post_language') && function_exists('pll_get_post_translations');
        // Straight from the posts table: no language filter, drafts and trash included.
        $rows = $wpdb->get_results(
            "SELECT ID, post_status, post_title, post_name, post_content, post_excerpt, post_modified_gmt, post_parent
             FROM {$wpdb->posts} WHERE post_type = 'product' AND post_status <> 'auto-draft' ORDER BY ID ASC"
        );
        $products = array();
        foreach ($rows as $row) {
            $id = (int) $row->ID;
            $translations = null;
            $language = null;
            if ($has_polylang) {
                $language = pll_get_post_language($id);
                $language = $language ? (string) $language : '';
                $translations = array();
                foreach ((array) pll_get_post_translations($id) as $lang => $member) {
                    $translations[(string) $lang] = (int) $member;
                }
                ksort($translations);
            }
            $types = wp_get_object_terms($id, 'product_type', array('fields' => 'slugs'));
            $categories = wp_get_object_terms($id, 'product_cat', array('fields' => 'ids'));
            $products[(string) $id] = array(
                'id' => $id,
                'status' => (string) $row->post_status,
                'type' => is_array($types) && $types ? (string) $types[0] : '',
                'title' => (string) $row->post_title,
                'slug' => (string) $row->post_name,
                'modified_gmt' => (string) $row->post_modified_gmt,
                'content_sha256' => hash('sha256', (string) $row->post_content),
                'excerpt_sha256' => hash('sha256', (string) $row->post_excerpt),
                'sku' => tsyc_snapshot_meta($id, '_sku'),
                'regular_price' => tsyc_snapshot_meta($id, '_regular_price'),
                'sale_price' => tsyc_snapshot_meta($id, '_sale_price'),
                'price' => tsyc_snapshot_meta($id, '_price'),
                'manage_stock' => tsyc_snapshot_meta($id, '_manage_stock'),
                'stock_quantity' => tsyc_snapshot_meta($id, '_stock'),
                'backorders' => tsyc_snapshot_meta($id, '_backorders'),
                'stock_status' => tsyc_snapshot_meta($id, '_stock_status'),
                'total_sales' => tsyc_snapshot_meta($id, 'total_sales'),
                'image_id' => (int) get_post_meta($id, '_thumbnail_id', true),
                'gallery_image_ids' => tsyc_snapshot_ids(get_post_meta($id, '_product_image_gallery', true)),
                'upsell_ids' => tsyc_snapshot_ids(get_post_meta($id, '_upsell_ids', true)),
                'cross_sell_ids' => tsyc_snapshot_ids(get_post_meta($id, '_crosssell_ids', true)),
                'category_ids' => is_array($categories) ? array_map('intval', $categories) : array(),
                'attributes_sha256' => hash('sha256', (string) wp_json_encode(get_post_meta($id, '_product_attributes', true))),
                'weight' => tsyc_snapshot_meta($id, '_weight'),
                'tsyc_internal_product_id' => tsyc_snapshot_meta($id, '_tsyc_internal_product_id'),
                'tsyc_language' => tsyc_snapshot_meta($id, '_tsyc_language'),
                'language' => $language,
                'translations' => $translations,
            );
        }
        $plugins = array();
        if (!function_exists('get_plugins')) {
            require_once ABSPATH . 'wp-admin/includes/plugin.php';
        }
        $installed = get_plugins();
        foreach ((array) get_option('active_plugins', array()) as $file) {
            $plugins[] = array('file' => (string) $file, 'version' => isset($installed[$file]['Version']) ? (string) $installed[$file]['Version'] : '');
        }
        $theme = wp_get_theme();
        return array(
            'tsyc_snapshot' => 1,
            'label' => $label,
            'taken_at_gmt' => gmdate('Y-m-d\TH:i:s\Z'),
            'site' => array(
                'host' => (string) wp_parse_url(home_url(), PHP_URL_HOST),
                'environment_type' => function_exists('wp_get_environment_type') ? wp_get_environment_type() : '',
                'wordpress' => get_bloginfo('version'),
                'woocommerce' => defined('WC_VERSION') ? WC_VERSION : '',
                'polylang' => defined('POLYLANG_VERSION') ? POLYLANG_VERSION : '',
                'theme' => (string) $theme->get_stylesheet(),
                'parent_theme' => (string) $theme->get_template(),
                'active_plugins' => $plugins,
                'manage_stock_option' => (string) get_option('woocommerce_manage_stock'),
                'no_stock_amount' => (string) get_option('woocommerce_notify_no_stock_amount'),
                'hold_stock_minutes' => (string) get_option('woocommerce_hold_stock_minutes'),
            ),
            'product_count' => count($products),
            'products' => $products,
        );
    }
}

if (!function_exists('wc_get_product')) {
    fwrite(STDERR, "WooCommerce is not active: no snapshot.\n");
    exit(2);
}
$tsyc_label = isset($args[0]) ? preg_replace('/[^A-Za-z0-9_.-]/', '_', (string) $args[0]) : 'snapshot';
echo wp_json_encode(tsyc_snapshot_build($tsyc_label)) . "\n";
