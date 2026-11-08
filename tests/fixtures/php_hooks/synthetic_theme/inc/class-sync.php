<?php
/** SYNTHETIC fixture: class-based registrations. Not the developer's code. */

namespace Tsyc\Fixture;

class Sync
{
    public function __construct($loader)
    {
        add_action('woocommerce_product_set_stock', array($this, 'on_stock'), 15);
        add_action('save_post', [$this, 'on_save']);
        add_action('save_post', [__CLASS__, 'static_save']);
        add_action('save_post', [self::class, 'static_save']);
        add_action('save_post', array('Tsyc\\Fixture\\Other', 'named_class_method'));
        add_action('save_post', 'Tsyc\\Fixture\\Other::named_class_method');
        add_action('init', $this->on_init(...));
        add_action('woocommerce_new_product', [$other, 'by_name_only']);
        $loader->add_action('woocommerce_loaded', $this, 'not_a_core_call');
    }

    public function on_stock($product)
    {
        $translations = pll_get_post_translations($product->get_id());
        foreach ($translations as $id) {
            $sibling = wc_get_product($id);
            $sibling->set_stock_quantity($product->get_stock_quantity());
            $sibling->set_regular_price($product->get_regular_price());
            $sibling->save();
        }
    }

    public function on_save($post_id)
    {
        $this->helper($post_id);
    }

    private function helper($post_id)
    {
        remove_action('save_post', [$this, 'on_save']);
        wp_update_post(array('ID' => $post_id));
        add_action('save_post', [$this, 'on_save']);
    }

    public static function static_save($post_id)
    {
        wp_update_post(array('ID' => $post_id, 'post_title' => $_POST['title'] ?? ''));
    }

    public function on_init()
    {
    }
}

class Other
{
    public static function named_class_method($id)
    {
        pll_set_post_language($id, 'vi');
    }

    public function by_name_only($id)
    {
        update_post_meta($id, '_sku', '');
    }
}
