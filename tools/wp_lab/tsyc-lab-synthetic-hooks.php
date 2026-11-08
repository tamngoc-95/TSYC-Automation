<?php
/**
 * Plugin Name: TSYC lab SYNTHETIC sync hooks (local lab only, never deploy)
 * Description: Assumed sync behaviours, each behind a switch. NOT the store's code and not evidence about it.
 *
 * Each mode is a guess at what the developer's unread theme code might do. A pass
 * with a mode on proves one thing only: TSYC's guards react correctly IF the
 * store behaves that way. Only the real source can say whether it does.
 */

if (!defined('ABSPATH')) {
    exit;
}

$tsyc_lab_host = (string) wp_parse_url((string) (defined('WP_HOME') ? WP_HOME : ''), PHP_URL_HOST);
if ($tsyc_lab_host !== '127.0.0.1' || !function_exists('wp_get_environment_type') || wp_get_environment_type() !== 'local') {
    return;
}

function tsyc_lab_mode(string $mode): bool
{
    wp_cache_delete('tsyc_lab_hook_modes', 'options');
    return in_array($mode, (array) get_option('tsyc_lab_hook_modes', array()), true);
}

function tsyc_lab_siblings(int $id): array
{
    $group = function_exists('pll_get_post_translations') ? (array) pll_get_post_translations($id) : array();
    return array_values(array_filter(array_map('intval', $group), static function ($other) use ($id) {
        return $other !== $id;
    }));
}

function tsyc_lab_copy_meta(int $from, int $to, array $keys): void
{
    foreach ($keys as $key) {
        update_post_meta($to, $key, get_post_meta($from, $key, true));
    }
}

const TSYC_LAB_INVENTORY = array('_stock', '_manage_stock', '_backorders', '_stock_status');
const TSYC_LAB_PRICE = array('_regular_price', '_sale_price', '_price');

/** Fired by Polylang for each member when a translation group is saved. */
add_action('set_object_terms', static function ($object_id, $terms, $tt_ids, $taxonomy): void {
    static $busy = false;
    if ($busy || $taxonomy !== 'post_translations' || get_post_type($object_id) !== 'product') {
        return;
    }
    $busy = true;
    $id = (int) $object_id;
    $siblings = tsyc_lab_siblings($id);
    $is_default = pll_get_post_language($id) === pll_default_language();
    if (tsyc_lab_mode('inventory_push_on_link') && !$is_default) {
        foreach ($siblings as $sibling) {
            tsyc_lab_copy_meta($id, $sibling, TSYC_LAB_INVENTORY);
        }
    }
    if (tsyc_lab_mode('price_pull_on_link') && !$is_default) {
        foreach ($siblings as $sibling) {
            if ((string) get_post_meta($sibling, '_regular_price', true) !== '') {
                tsyc_lab_copy_meta($sibling, $id, TSYC_LAB_PRICE);
                break;
            }
        }
    }
    if (tsyc_lab_mode('sku_copy_on_link') && !$is_default) {
        $source = (int) pll_get_post($id, pll_default_language());
        if ($source > 0) {
            update_post_meta($id, '_sku', get_post_meta($source, '_sku', true));
        }
    }
    if (tsyc_lab_mode('touch_siblings_on_link') && !$is_default) {
        foreach ($siblings as $sibling) {
            wp_update_post(array('ID' => $sibling));
        }
    }
    if (tsyc_lab_mode('relink_on_link') && !$is_default) {
        $target = (int) get_option('tsyc_lab_relink_target');
        $group = (array) pll_get_post_translations($id);
        if ($target > 0 && !in_array($target, array_map('intval', $group), true)) {
            $group[(string) pll_get_post_language($target)] = $target;
            pll_save_post_translations($group);
        }
    }
    if (tsyc_lab_mode('outside_write_on_link') && !$is_default) {
        $target = (int) get_option('tsyc_lab_outside_target');
        if ($target > 0) {
            update_post_meta($target, '_stock', '99');
        }
    }
    $busy = false;
}, 10, 4);

/** A save in one language pushes values onto the group. */
add_action('woocommerce_update_product', static function ($id): void {
    static $busy = false;
    if ($busy) {
        return;
    }
    $busy = true;
    $id = (int) $id;
    $is_default = pll_get_post_language($id) === pll_default_language();
    foreach (tsyc_lab_siblings($id) as $sibling) {
        if (tsyc_lab_mode('price_push_vi_on_save') && $is_default) {
            tsyc_lab_copy_meta($id, $sibling, TSYC_LAB_PRICE);
        }
        if (tsyc_lab_mode('price_push_any_on_save')) {
            tsyc_lab_copy_meta($id, $sibling, TSYC_LAB_PRICE);
        }
        if (tsyc_lab_mode('inventory_push_any_on_save')) {
            tsyc_lab_copy_meta($id, $sibling, TSYC_LAB_INVENTORY);
        }
    }
    $busy = false;
}, 10, 1);
