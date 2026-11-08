<?php
/**
 * SYNTHETIC fixture for tests/test_php_hook_inventory.py. Written by TSYC to
 * exercise the parser. It is not the developer's code and says nothing about
 * the store. Its "secrets" are fake.
 *
 * add_action('in_a_docblock', 'must_not_count');
 */

// add_filter('in_a_line_comment', 'must_not_count');
# add_filter('in_a_hash_comment', 'must_not_count');

$not_a_hook = "add_action('in_a_string', 'must_not_count');";
$heredoc = <<<TXT
add_action('in_a_heredoc', 'must_not_count');
TXT;

define('FAKE_API_KEY', 'fake-value-for-the-redaction-test');
$fake_password = 'hunter2hunter2';
$settings = array('consumer_secret' => 'cs_0123456789012345678901234567890123456789');

// Named function: VI pushes stock to the siblings, guarded.
add_action('woocommerce_update_product', 'tsyc_fixture_push_stock', 20, 1);
function tsyc_fixture_push_stock($product_id)
{
    static $running = false;
    if ($running || defined('DOING_AUTOSAVE') || wp_is_post_revision($product_id)) {
        return;
    }
    if (pll_get_post_language($product_id) !== pll_default_language()) {
        return;
    }
    $running = true;
    $stock = get_post_meta($product_id, '_stock', true);
    foreach (pll_get_post_translations($product_id) as $sibling) {
        update_post_meta($sibling, '_stock', $stock);
        update_post_meta($sibling, '_backorders', get_post_meta($product_id, '_backorders', true));
    }
    $running = false;
}

// Closure, any language, on the link event: the case the bridge's guard exists for.
add_action('set_object_terms', function ($object_id, $terms, $tt_ids, $taxonomy) {
    if ($taxonomy !== 'post_translations') {
        return;
    }
    foreach (pll_get_post_translations($object_id) as $sibling) {
        tsyc_fixture_copy_price($object_id, $sibling);
    }
}, 10, 4);

// Reached only through another function.
function tsyc_fixture_copy_price($from, $to)
{
    update_post_meta($to, '_regular_price', get_post_meta($from, '_regular_price', true));
    update_post_meta($to, '_price', get_post_meta($from, '_price', true));
}

// Unguarded recursion: the save fires the very hook it is on.
add_action('save_post_product', 'tsyc_fixture_resave');
function tsyc_fixture_resave($post_id)
{
    $product = wc_get_product($post_id);
    $product->set_sku('X');
    $product->save();
}

// A cycle through two hooks and a custom action, guarded on one side only.
add_action('tsyc_fixture_custom', 'tsyc_fixture_cycle_a');
add_action('woocommerce_product_set_stock', 'tsyc_fixture_cycle_b');
function tsyc_fixture_cycle_a($id)
{
    wc_update_product_stock($id, 1);
}
function tsyc_fixture_cycle_b($product)
{
    do_action('tsyc_fixture_custom', $product->get_id());
}

// Dynamic meta key copied in a loop, and raw SQL.
add_filter('pll_copy_post_metas', 'tsyc_fixture_copy_metas', 99, 2);
function tsyc_fixture_copy_metas($keys, $sync)
{
    global $wpdb;
    foreach (array('_sku', '_stock') as $key) {
        update_post_meta(1, $key, 'x');
    }
    $wpdb->query("UPDATE wp_postmeta SET meta_value = 1");
    return $keys;
}

// Same field, same hook: which one wins depends on priority.
add_action('woocommerce_update_product', 'tsyc_fixture_set_stock_late', 99);
function tsyc_fixture_set_stock_late($id)
{
    update_post_meta($id, '_stock', 5);
}

// Forms the resolver must understand or admit it cannot.
add_action('init', 'tsyc_fixture_missing_function');
add_action('init', $unknown_callback);
add_action('woocommerce_' . $status . '_product', 'tsyc_fixture_set_stock_late', 5);
add_action("woocommerce_{$status}_thing", 'tsyc_fixture_set_stock_late');
add_filter('the_title', fn($title) => strtoupper($title));
add_action('admin_init', 'Tsyc\\Fixture\\namespaced_function');
remove_action('woocommerce_update_product', 'someone_elses_callback');
?>
<div>add_action('in_inline_html', 'must_not_count');</div>
<?php
add_action('after_html', 'tsyc_fixture_after_html', -5);
function tsyc_fixture_after_html()
{
}
