<?php
/**
 * Plugin Name: TSYC lab event recorder (local lab only, never deploy)
 * Description: Records which product hooks really fire during one traced lab scenario.
 */

if (!defined('ABSPATH')) {
    exit;
}

$tsyc_lab_host = (string) wp_parse_url((string) (defined('WP_HOME') ? WP_HOME : ''), PHP_URL_HOST);
if ($tsyc_lab_host !== '127.0.0.1' || !function_exists('wp_get_environment_type') || wp_get_environment_type() !== 'local') {
    return;
}

$tsyc_lab_trace = (string) get_option('tsyc_lab_trace_id', '');
if (!preg_match('/^[a-z0-9_-]{1,64}$/', $tsyc_lab_trace)) {
    return;
}

add_action('all', static function ($hook, ...$args) use ($tsyc_lab_trace): void {
    static $busy = false;
    $pattern = '/^(save_post(_product)?|wp_insert_post|edit_post|post_updated|pre_post_update|transition_post_status'
        . '|(draft|publish|trash|pending)_product|wp_trash_post|trashed_post|untrashed_post|before_delete_post'
        . '|added_post_meta|updated_post_meta|deleted_post_meta|set_object_terms'
        . '|woocommerce_(new_product|update_product|before_product_object_save|after_product_object_save'
        . '|product_object_updated_props|product_set_stock|product_set_stock_status|updated_product_stock'
        . '|admin_process_product_object|process_product_meta(_simple)?|rest_insert_product_object'
        . '|rest_pre_insert_product_object|reduce_order_stock|reduce_order_item_stock|order_status_processing)'
        . '|pll_save_post|pll_post_synchronized|pll_copy_post_metas|pll_created_sync_post)$/';
    if ($busy || !is_string($hook) || !preg_match($pattern, $hook)) {
        return;
    }
    if (in_array($hook, array('added_post_meta', 'updated_post_meta', 'deleted_post_meta'), true)) {
        $id = is_numeric($args[1] ?? null) ? (int) $args[1] : 0;
        $key = (string) ($args[2] ?? '');
        if (strpos($key, '_edit_') === 0) {
            return;
        }
    } elseif ($hook === 'set_object_terms') {
        $id = (int) ($args[0] ?? 0);
        $key = (string) ($args[3] ?? '');
    } elseif ($hook === 'transition_post_status') {
        $post = $args[2] ?? null;
        $id = $post instanceof WP_Post ? (int) $post->ID : 0;
        $key = (string) ($args[1] ?? '') . '->' . (string) ($args[0] ?? '');
    } else {
        $first = $args[0] ?? null;
        $id = is_numeric($first) ? (int) $first : ($first instanceof WC_Data ? (int) $first->get_id() : ($first instanceof WP_Post ? (int) $first->ID : 0));
        $key = '';
    }
    $busy = true;
    $lang = ($id > 0 && function_exists('pll_get_post_language')) ? (string) pll_get_post_language($id) : '';
    $busy = false;
    $dir = WP_CONTENT_DIR . '/tsyc-lab-trace';
    if (!is_dir($dir)) {
        mkdir($dir, 0777, true);
    }
    file_put_contents("$dir/$tsyc_lab_trace.jsonl",
                      json_encode(array('hook' => $hook, 'id' => $id, 'key' => $key, 'lang' => $lang)) . "\n", FILE_APPEND | LOCK_EX);
}, 10, 6);
