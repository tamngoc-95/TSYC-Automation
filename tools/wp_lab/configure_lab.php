<?php
// wp eval-file: Polylang and WooCommerce settings matching production
// (docs/POLYLANG_EXISTING_ARCHITECTURE.md section 1). Idempotent.

if (wp_get_environment_type() !== 'local' || strpos(home_url(), 'http://127.0.0.1') !== 0) {
    WP_CLI::error('Refusing: not the local lab.');
}

update_option('woocommerce_custom_orders_table_enabled', 'yes');
update_option('woocommerce_coming_soon', 'no');
update_option('woocommerce_currency', 'EUR');
update_option('woocommerce_manage_stock', 'yes');
if (class_exists('WC_Install')) {
    WC_Install::install();
}

if (!function_exists('PLL')) {
    WP_CLI::error('Polylang is not loaded.');
}
// Through Polylang's own options object: it writes its in-memory copy back at
// shutdown, so a plain update_option('polylang') would be overwritten.
$settings = array(
    'force_lang'    => 1,
    'hide_default'  => true,
    'rewrite'       => true,
    'media_support' => false,
    'sync'          => array(),
    'post_types'    => array('product'),
    'taxonomies'    => array('product_cat', 'product_tag', 'product_shipping_class'),
    'browser'       => false,
);
foreach ($settings as $key => $value) {
    $result = PLL()->options->set($key, $value);
    if (is_wp_error($result) && $result->has_errors()) {
        WP_CLI::warning("polylang $key: " . $result->get_error_message());
    }
}
$model = PLL()->model;
$existing = wp_list_pluck($model->get_languages_list(), 'slug');
$wanted = array(
    array('name' => 'Tiếng Việt', 'slug' => 'vi', 'locale' => 'vi', 'flag' => 'vn', 'rtl' => false, 'term_group' => 0),
    array('name' => 'English', 'slug' => 'en', 'locale' => 'en_GB', 'flag' => 'gb', 'rtl' => false, 'term_group' => 1),
    array('name' => 'Deutsch', 'slug' => 'de', 'locale' => 'de_DE', 'flag' => 'de', 'rtl' => false, 'term_group' => 2),
);
foreach ($wanted as $language) {
    if (in_array($language['slug'], $existing, true)) {
        continue;
    }
    $result = $model->add_language($language);
    if (is_wp_error($result)) {
        WP_CLI::error($language['slug'] . ': ' . $result->get_error_message());
    }
    $model->clean_languages_cache();
}
PLL()->options->set('default_lang', 'vi');
flush_rewrite_rules(false);

WP_CLI::log(wp_json_encode(array(
    'wordpress'   => get_bloginfo('version'),
    'woocommerce' => defined('WC_VERSION') ? WC_VERSION : null,
    'polylang'    => defined('POLYLANG_VERSION') ? POLYLANG_VERSION : null,
    'hpos'        => get_option('woocommerce_custom_orders_table_enabled'),
    'languages'   => wp_list_pluck($model->get_languages_list(), 'slug'),
    'default'     => pll_default_language(),
    'post_types'  => PLL()->options['post_types'],
    'sync'        => PLL()->options['sync'],
    'php'         => PHP_VERSION,
)));
