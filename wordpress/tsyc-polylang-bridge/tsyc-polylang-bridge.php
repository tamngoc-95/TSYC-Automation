<?php
/**
 * Plugin Name: TSYC Polylang Bridge
 * Description: Authenticated read of Polylang's product translation links. Write routes exist only on an explicitly enabled non-production site.
 * Version: 1.1.0
 * Requires PHP: 7.4
 *
 * Install as a must-use plugin. Uses Polylang's public PHP API only
 * (pll_get_post_language, pll_set_post_language, pll_get_post_translations,
 * pll_save_post_translations, pll_get_term); it never writes Polylang's
 * tables or terms directly.
 *
 * Semantics verified against the Polylang 3.8.4 source
 * (docs/POLYLANG_BRIDGE.md):
 *   - pll_save_post_translations UNLINKS any current member left out of the
 *     array and silently DROPS a member whose language differs from its key,
 *     so a link is built from the full current group and always read back;
 *   - pll_set_post_language returns false when the language is already set,
 *     so the language is read back rather than trusting the return value.
 *
 * Read routes are safe on any site. Write routes are not even registered
 * unless every guard in tsyc_bridge_writes_enabled() holds.
 */

if (!defined('ABSPATH')) {
    exit;
}

const TSYC_BRIDGE_VERSION = '1.1.0';
const TSYC_BRIDGE_MECHANISM = 'tsyc/v1 translations endpoint';
const TSYC_BRIDGE_NAMESPACE = 'tsyc/v1';
const TSYC_BRIDGE_PRODUCTION_HOSTS = array('tiemsachyeucon.com');
const TSYC_BRIDGE_LOCK_SECONDS = 120;
const TSYC_BRIDGE_PROFILE_FAHASA_PREORDER = 'FAHASA_PREORDER';

const TSYC_META_OWNER = '_tsyc_internal_product_id';
const TSYC_META_LANGUAGE = '_tsyc_language';
const TSYC_META_REQUEST = '_tsyc_request_id';
const TSYC_META_FINGERPRINT = '_tsyc_request_fingerprint';
const TSYC_META_SOURCE = '_tsyc_source_product_id';
const TSYC_META_AUTHORIZATION = '_tsyc_authorization_id';
const TSYC_META_STATE = '_tsyc_bridge_state';

const TSYC_STATE_CREATED = 'CREATED';
const TSYC_STATE_LANGUAGE_SET = 'LANGUAGE_SET';
const TSYC_STATE_LINKED = 'LINKED';
const TSYC_STATE_SIDE_EFFECT = 'SIDE_EFFECT';

const TSYC_META_SNAPSHOT = '_tsyc_group_snapshot';
// What an operation must leave untouched on every product other than its own draft.
const TSYC_BRIDGE_WATCHED_META = array('_sku', '_regular_price', '_sale_price', '_price', '_stock',
                                       '_manage_stock', '_backorders', '_stock_status');

add_action('rest_api_init', 'tsyc_bridge_register_routes');

function tsyc_bridge_register_routes(): void
{
    register_rest_route(TSYC_BRIDGE_NAMESPACE, '/health', array(
        'methods'             => 'GET',
        'callback'            => 'tsyc_bridge_health',
        'permission_callback' => 'tsyc_bridge_can_read',
    ));
    register_rest_route(TSYC_BRIDGE_NAMESPACE, '/translations/(?P<id>[1-9][0-9]{0,18})', array(
        'methods'             => 'GET',
        'callback'            => 'tsyc_bridge_read_translations',
        'permission_callback' => 'tsyc_bridge_can_read',
    ));
    register_rest_route(TSYC_BRIDGE_NAMESPACE, '/translations/by-request/(?P<request_id>[A-Za-z0-9._:-]{8,128})', array(
        'methods'             => 'GET',
        'callback'            => 'tsyc_bridge_read_by_request',
        'permission_callback' => 'tsyc_bridge_can_read',
    ));

    if (!tsyc_bridge_writes_enabled()) {
        return;
    }
    register_rest_route(TSYC_BRIDGE_NAMESPACE, '/translations', array(
        'methods'             => 'POST',
        'callback'            => 'tsyc_bridge_create_translation',
        'permission_callback' => 'tsyc_bridge_can_write',
    ));
    register_rest_route(TSYC_BRIDGE_NAMESPACE, '/translations/rollback', array(
        'methods'             => 'POST',
        'callback'            => 'tsyc_bridge_rollback_translation',
        'permission_callback' => 'tsyc_bridge_can_write',
    ));
}

/* ---------------------------------------------------------------- guards */

/**
 * Every condition must hold: explicit constant, non-production environment
 * type, not a production host, and a strong write token. The host list is a
 * second lock against a copied wp-config on production.
 */
function tsyc_bridge_writes_enabled(): bool
{
    return tsyc_bridge_writes_allowed(
        defined('TSYC_BRIDGE_ENABLE_WRITES') && TSYC_BRIDGE_ENABLE_WRITES === true,
        wp_get_environment_type(),
        (string) wp_parse_url(home_url(), PHP_URL_HOST),
        defined('TSYC_BRIDGE_WRITE_TOKEN') && is_string(TSYC_BRIDGE_WRITE_TOKEN) ? TSYC_BRIDGE_WRITE_TOKEN : ''
    );
}

function tsyc_bridge_writes_allowed(bool $enabled, string $environment, string $host, string $token): bool
{
    if (!$enabled || !in_array($environment, array('local', 'staging'), true)) {
        return false;
    }
    $host = preg_replace('/^www\./', '', strtolower($host));
    if ($host === '' || in_array($host, TSYC_BRIDGE_PRODUCTION_HOSTS, true)) {
        return false;
    }
    return strlen($token) >= 32;
}

function tsyc_bridge_transport_ok(): bool
{
    return is_ssl() || wp_get_environment_type() === 'local';
}

/** @return true|WP_Error */
function tsyc_bridge_can_read()
{
    if (!tsyc_bridge_transport_ok()) {
        return new WP_Error('tsyc_https_required', 'HTTPS is required.', array('status' => 403));
    }
    if (!is_user_logged_in()) {
        return new WP_Error('tsyc_unauthenticated', 'Authentication is required.', array('status' => 401));
    }
    if (!current_user_can('edit_products')) {
        return new WP_Error('tsyc_forbidden', 'Not allowed.', array('status' => 403));
    }
    return true;
}

/** @return true|WP_Error */
function tsyc_bridge_can_write(WP_REST_Request $request)
{
    $read = tsyc_bridge_can_read();
    if ($read !== true) {
        return $read;
    }
    if (!tsyc_bridge_writes_enabled()) {
        return new WP_Error('tsyc_writes_disabled', 'Writes are disabled on this site.', array('status' => 403));
    }
    $token = (string) $request->get_header('x_tsyc_write_token');
    if ($token === '' || !hash_equals((string) TSYC_BRIDGE_WRITE_TOKEN, $token)) {
        return new WP_Error('tsyc_bad_write_token', 'The write token is missing or wrong.', array('status' => 403));
    }
    return true;
}

/** @return true|WP_Error */
function tsyc_bridge_polylang_ready()
{
    foreach (array('pll_get_post_language', 'pll_set_post_language', 'pll_get_post_translations',
                   'pll_save_post_translations', 'pll_is_translated_post_type', 'pll_languages_list') as $function) {
        if (!function_exists($function)) {
            return new WP_Error('tsyc_polylang_unavailable', 'Polylang is not available.', array('status' => 503));
        }
    }
    if (!pll_is_translated_post_type('product')) {
        return new WP_Error('tsyc_product_not_translated', 'Products are not translated by Polylang here.', array('status' => 503));
    }
    if (!function_exists('wc_get_product')) {
        return new WP_Error('tsyc_woocommerce_unavailable', 'WooCommerce is not available.', array('status' => 503));
    }
    return true;
}

/* ----------------------------------------------------------------- reads */

function tsyc_bridge_health()
{
    $ready = tsyc_bridge_polylang_ready();
    if (is_wp_error($ready)) {
        return $ready;
    }
    return array(
        'bridge_version'   => TSYC_BRIDGE_VERSION,
        'mechanism'        => TSYC_BRIDGE_MECHANISM,
        'polylang_version' => defined('POLYLANG_VERSION') ? POLYLANG_VERSION : null,
        'woocommerce'      => defined('WC_VERSION') ? WC_VERSION : null,
        'languages'        => array_values(pll_languages_list()),
        'default_language' => pll_default_language(),
        'environment'      => wp_get_environment_type(),
        'writes_enabled'   => tsyc_bridge_writes_enabled(),
    );
}

function tsyc_bridge_read_translations(WP_REST_Request $request)
{
    $ready = tsyc_bridge_polylang_ready();
    if (is_wp_error($ready)) {
        return $ready;
    }
    $id = (int) $request['id'];
    $post = get_post($id);
    if (!$post instanceof WP_Post || $post->post_type !== 'product') {
        return new WP_Error('tsyc_not_a_product', 'No such product.', array('status' => 404));
    }
    $lang = pll_get_post_language($id);
    if (empty($lang)) {
        return new WP_Error('tsyc_language_unassigned', 'The product has no Polylang language.', array('status' => 409));
    }
    return tsyc_bridge_group_view($id, (string) $lang);
}

/** The authoritative group of one product plus every inconsistency found. */
function tsyc_bridge_group_view(int $id, string $lang): array
{
    $translations = tsyc_bridge_translations($id, $lang);
    $issues = array();
    $members = array();
    foreach ($translations as $member_lang => $member_id) {
        $member_id = (int) $member_id;
        $post = get_post($member_id);
        if (!$post instanceof WP_Post) {
            $issues[] = array('code' => 'MEMBER_MISSING', 'detail' => "$member_lang #$member_id does not exist.");
            continue;
        }
        if ($post->post_type !== 'product') {
            $issues[] = array('code' => 'MEMBER_NOT_A_PRODUCT', 'detail' => "$member_lang #$member_id is a {$post->post_type}.");
        }
        if ($post->post_status === 'trash') {
            $issues[] = array('code' => 'MEMBER_TRASHED', 'detail' => "$member_lang #$member_id is in the trash.");
        }
        $member_language = pll_get_post_language($member_id);
        if ($member_language !== $member_lang) {
            $issues[] = array('code' => 'MEMBER_LANGUAGE_MISMATCH',
                              'detail' => "#$member_id is linked as $member_lang but is " . var_export($member_language, true) . '.');
        }
        $language_meta = (string) get_post_meta($member_id, TSYC_META_LANGUAGE, true);
        if ($language_meta !== '' && $language_meta !== $member_lang) {
            $issues[] = array('code' => 'MEMBER_LANGUAGE_META_MISMATCH',
                              'detail' => "#$member_id is linked as $member_lang but TSYC recorded $language_meta.");
        }
        $their_group = tsyc_bridge_translations($member_id, (string) $member_language);
        ksort($their_group);
        $our_group = $translations;
        ksort($our_group);
        if (array_map('intval', $their_group) !== array_map('intval', $our_group)) {
            $issues[] = array('code' => 'ASYMMETRIC_GROUP', 'detail' => "#$member_id reports a different translation group.");
        }
        $members[$member_lang] = tsyc_bridge_member_view($post);
    }
    $self = get_post($id);
    return array(
        'bridge_version'   => TSYC_BRIDGE_VERSION,
        'mechanism'        => TSYC_BRIDGE_MECHANISM,
        'polylang_version' => defined('POLYLANG_VERSION') ? POLYLANG_VERSION : null,
        'id'               => $id,
        'lang'             => $lang,
        'sku'              => (string) get_post_meta($id, '_sku', true),
        'status'           => $self instanceof WP_Post ? $self->post_status : null,
        'translations'     => array_map('intval', $translations),
        'members'          => $members,
        'consistent'       => empty($issues),
        'issues'           => $issues,
    );
}

/** Polylang's map, with the product itself always present (a product without a group has an empty map). */
function tsyc_bridge_translations(int $id, string $lang): array
{
    $translations = pll_get_post_translations($id);
    $translations = is_array($translations) ? $translations : array();
    if ($lang !== '' && !isset($translations[$lang])) {
        $translations[$lang] = $id;
    }
    return $translations;
}

function tsyc_bridge_member_view(WP_Post $post): array
{
    return array(
        'id'           => (int) $post->ID,
        'status'       => $post->post_status,
        'sku'          => (string) get_post_meta($post->ID, '_sku', true),
        'modified_gmt' => tsyc_bridge_modified_gmt($post),
        'owner_meta'   => (string) get_post_meta($post->ID, TSYC_META_OWNER, true),
        'language_meta' => (string) get_post_meta($post->ID, TSYC_META_LANGUAGE, true),
        'request_id'   => (string) get_post_meta($post->ID, TSYC_META_REQUEST, true),
        'bridge_state' => (string) get_post_meta($post->ID, TSYC_META_STATE, true),
    );
}

/** The same shape wc/v3 prints for date_modified_gmt. */
function tsyc_bridge_modified_gmt(WP_Post $post): string
{
    return str_replace(' ', 'T', (string) $post->post_modified_gmt);
}

function tsyc_bridge_read_by_request(WP_REST_Request $request)
{
    $ready = tsyc_bridge_polylang_ready();
    if (is_wp_error($ready)) {
        return $ready;
    }
    $request_id = (string) $request['request_id'];
    $found = tsyc_bridge_find_by_request($request_id);
    if (count($found) > 1) {
        return new WP_Error('tsyc_request_duplicated', "Several products carry request $request_id.",
                            array('status' => 409, 'product_ids' => $found));
    }
    if (empty($found)) {
        return array('request_id' => $request_id, 'found' => false);
    }
    $id = (int) $found[0];
    $post = get_post($id);
    $lang = (string) pll_get_post_language($id);
    return array(
        'request_id' => $request_id,
        'found'      => true,
        'product_id' => $id,
        'status'     => $post->post_status,
        'lang'       => $lang,
        'state'      => (string) get_post_meta($id, TSYC_META_STATE, true),
        'source_id'  => (int) get_post_meta($id, TSYC_META_SOURCE, true),
        'group'      => tsyc_bridge_group_view($id, $lang),
    );
}

/** Includes the trash and every language: a rolled-back request stays known. */
function tsyc_bridge_find_by_request(string $request_id): array
{
    return array_map('intval', get_posts(array(
        'post_type'        => 'product',
        'post_status'      => array('draft', 'pending', 'private', 'publish', 'future', 'trash', 'auto-draft'),
        'meta_key'         => TSYC_META_REQUEST,
        'meta_value'       => $request_id,
        'fields'           => 'ids',
        'posts_per_page'   => 5,
        'lang'             => '',
        'suppress_filters' => false,
        'no_found_rows'    => true,
    )));
}

/* ---------------------------------------------------------------- writes */

/** @return array|WP_Error Validated create request. */
function tsyc_bridge_validate_create(array $body)
{
    $allowed = array('source_id', 'lang', 'request_id', 'authorization_id', 'internal_product_id', 'product_code',
                     'expected_source_modified_gmt', 'inventory_profile', 'content');
    $unknown = array_diff(array_keys($body), $allowed);
    if (!empty($unknown)) {
        // Price, stock, status, SKU and anything else simply cannot be sent.
        return tsyc_bridge_error('tsyc_forbidden_fields', 'Unknown fields: ' . implode(', ', $unknown), 400);
    }
    foreach ($allowed as $field) {
        if (!isset($body[$field])) {
            return tsyc_bridge_error('tsyc_missing_field', "Missing field: $field", 400);
        }
    }
    $request_id = $body['request_id'];
    if (!is_string($request_id) || !preg_match('/^[A-Za-z0-9._:-]{8,128}$/', $request_id)) {
        return tsyc_bridge_error('tsyc_bad_request_id', 'Invalid request_id.', 400);
    }
    if (!is_int($body['source_id']) || $body['source_id'] <= 0) {
        return tsyc_bridge_error('tsyc_bad_source_id', 'source_id must be a positive integer.', 400);
    }
    $default = pll_default_language();
    if (!is_string($body['lang']) || $body['lang'] === $default || !in_array($body['lang'], pll_languages_list(), true)) {
        return tsyc_bridge_error('tsyc_bad_language', 'lang must be a non-default Polylang language.', 400);
    }
    foreach (array('authorization_id', 'internal_product_id', 'product_code', 'expected_source_modified_gmt') as $field) {
        if (!is_string($body[$field]) || trim($body[$field]) === '' || strlen($body[$field]) > 191) {
            return tsyc_bridge_error('tsyc_bad_field', "Invalid $field.", 400);
        }
    }
    if ($body['inventory_profile'] !== TSYC_BRIDGE_PROFILE_FAHASA_PREORDER) {
        return tsyc_bridge_error('tsyc_bad_inventory_profile', 'Unsupported inventory profile.', 400);
    }
    $content = $body['content'];
    if (!is_array($content) || array_diff(array_keys($content), array('name', 'description', 'short_description'))) {
        return tsyc_bridge_error('tsyc_bad_content', 'content holds name, description and short_description only.', 400);
    }
    foreach (array('name', 'description', 'short_description') as $field) {
        if (!isset($content[$field]) || !is_string($content[$field])) {
            return tsyc_bridge_error('tsyc_bad_content', "content.$field must be a string.", 400);
        }
    }
    if (trim($content['name']) === '') {
        return tsyc_bridge_error('tsyc_bad_content', 'content.name is empty.', 400);
    }
    return $body;
}

function tsyc_bridge_error(string $code, string $message, int $status, array $extra = array()): WP_Error
{
    return new WP_Error($code, $message, array_merge(array('status' => $status), $extra));
}

function tsyc_bridge_fingerprint(array $body): string
{
    // authorization_id and the expected timestamp may legitimately differ on a resume.
    return hash('sha256', (string) wp_json_encode(array(
        (int) $body['source_id'], $body['lang'], $body['internal_product_id'], $body['product_code'],
        $body['inventory_profile'], $body['content']['name'], $body['content']['description'],
        $body['content']['short_description'],
    )));
}

/**
 * The source must be TSYC's exact, unchanged VI pre-order draft. Identity
 * needs the SKU, the owner meta and the caller's own baseline together:
 * none of them alone is proof, because hooks can copy meta.
 *
 * @return WC_Product|WP_Error
 */
function tsyc_bridge_validate_source(array $body)
{
    $source_id = (int) $body['source_id'];
    $post = get_post($source_id);
    if (!$post instanceof WP_Post || $post->post_type !== 'product') {
        return tsyc_bridge_error('tsyc_source_missing', "No product #$source_id.", 404);
    }
    if ($post->post_status !== 'draft') {
        return tsyc_bridge_error('tsyc_source_not_draft', "#$source_id is {$post->post_status}: owner territory.", 409);
    }
    if (pll_get_post_language($source_id) !== pll_default_language()) {
        return tsyc_bridge_error('tsyc_source_wrong_language', "#$source_id is not in the default language.", 409);
    }
    $product = wc_get_product($source_id);
    if (!$product instanceof WC_Product || $product->get_type() !== 'simple') {
        return tsyc_bridge_error('tsyc_source_wrong_type', "#$source_id is not a simple product.", 409);
    }
    if ($product->get_sku('edit') !== $body['product_code']) {
        return tsyc_bridge_error('tsyc_source_identity', "#$source_id does not carry SKU {$body['product_code']}.", 409);
    }
    if ((string) get_post_meta($source_id, TSYC_META_OWNER, true) !== $body['internal_product_id']) {
        return tsyc_bridge_error('tsyc_source_identity', "#$source_id is not this internal product.", 409);
    }
    if (tsyc_bridge_modified_gmt($post) !== $body['expected_source_modified_gmt']) {
        return tsyc_bridge_error('tsyc_source_changed', "#$source_id changed after TSYC's baseline.", 409,
                                 array('current_modified_gmt' => tsyc_bridge_modified_gmt($post)));
    }
    $is_preorder = $product->get_manage_stock('edit') === true && (int) $product->get_stock_quantity('edit') === 0
        && $product->get_backorders('edit') === 'yes' && $product->get_regular_price('edit') === ''
        && $product->get_sale_price('edit') === '';
    if (!$is_preorder) {
        return tsyc_bridge_error('tsyc_source_not_preorder', "#$source_id does not have the pre-order inventory shape.", 409);
    }
    return $product;
}

/**
 * One lock per translation group, not per language: pll_save_post_translations
 * rewrites the whole group, so two languages saved from the same earlier read
 * unlink each other while both read back as linked.
 */
function tsyc_bridge_lock_key(int $source_id): string
{
    return 'tsyc_bridge_lock_' . $source_id;
}

/*
 * The lock row is written with SQL on purpose. add_option is an upsert
 * (ON DUPLICATE KEY UPDATE), so two callers can both "win" it: a real lost link
 * on the lab, 2026-11-03. These three functions are the only SQL in this file,
 * they touch only this row in the options table, and a test enforces that.
 */

/** Atomic on the unique option_name: exactly one caller gets a row. */
function tsyc_bridge_lock_insert(string $key, string $token): bool
{
    global $wpdb;
    $wpdb->query($wpdb->prepare("INSERT IGNORE INTO {$wpdb->options} (option_name, option_value, autoload) VALUES (%s, %s, 'off')", $key, $token));
    return (int) $wpdb->rows_affected === 1;
}

/** Deletes only the holder named, so two callers cannot both clear one expired lock. */
function tsyc_bridge_lock_delete(string $key, string $token): bool
{
    global $wpdb;
    return (int) $wpdb->query($wpdb->prepare("DELETE FROM {$wpdb->options} WHERE option_name = %s AND option_value = %s", $key, $token)) === 1;
}

/** @return string|null The holder's token, or null when the group is busy. */
function tsyc_bridge_acquire_lock(string $key): ?string
{
    global $wpdb;
    $token = time() . ':' . wp_generate_password(20, false);
    if (tsyc_bridge_lock_insert($key, $token)) {
        return $token;
    }
    $held = $wpdb->get_var($wpdb->prepare("SELECT option_value FROM {$wpdb->options} WHERE option_name = %s", $key));
    if (is_string($held) && (time() - (int) $held) > TSYC_BRIDGE_LOCK_SECONDS
        && tsyc_bridge_lock_delete($key, $held) && tsyc_bridge_lock_insert($key, $token)) {
        return $token;
    }
    return null;
}

/** Only the holder releases: a worker that outlived its lock must not free the next one's. */
function tsyc_bridge_release_lock(string $key, string $token): void
{
    tsyc_bridge_lock_delete($key, $token);
}

/* ------------------------------------------------- side-effect detection */

/** The product's stored values, read past every cache. */
function tsyc_bridge_member_snapshot(int $id): array
{
    clean_post_cache($id);
    wp_cache_delete($id, 'post_meta');
    $post = get_post($id);
    $snapshot = array('status' => $post instanceof WP_Post ? $post->post_status : null,
                      'modified_gmt' => $post instanceof WP_Post ? tsyc_bridge_modified_gmt($post) : null);
    foreach (TSYC_BRIDGE_WATCHED_META as $key) {
        $snapshot[$key] = (string) get_post_meta($id, $key, true);
    }
    return $snapshot;
}

/** @param array $group {lang: product ID} */
function tsyc_bridge_group_snapshot(array $group): array
{
    $members = array();
    foreach ($group as $member_id) {
        $members[(string) (int) $member_id] = tsyc_bridge_member_snapshot((int) $member_id);
    }
    return array('group' => array_map('intval', $group), 'members' => $members);
}

/** Records every write to a watched field during this request, whatever made it. */
function tsyc_bridge_watch_writes(bool $on): void
{
    foreach (array('added_post_meta', 'updated_post_meta', 'deleted_post_meta') as $hook) {
        if ($on) {
            add_action($hook, 'tsyc_bridge_note_write', 10, 3);
        } else {
            remove_action($hook, 'tsyc_bridge_note_write', 10);
        }
    }
    if ($on) {
        add_action('save_post_product', 'tsyc_bridge_note_save', 10, 1);
    } else {
        remove_action('save_post_product', 'tsyc_bridge_note_save', 10);
    }
}

/** A modified time has one-second resolution, so a same-second save shows only here. */
function tsyc_bridge_note_save($post_id): void
{
    $GLOBALS['tsyc_bridge_writes'][] = array('product_id' => (int) $post_id, 'field' => 'post_save');
}

function tsyc_bridge_note_write($meta_id, $post_id, $key): void
{
    if (in_array($key, TSYC_BRIDGE_WATCHED_META, true)) {
        $GLOBALS['tsyc_bridge_writes'][] = array('product_id' => (int) $post_id, 'field' => (string) $key);
    }
}

/**
 * What changed on the products this operation must not touch. Never repaired:
 * undoing it would be another blind write to a product that may be the owner's.
 *
 * @param array $before tsyc_bridge_group_snapshot() from before the operation.
 * @param int   $own    The operation's own draft; 0 for none.
 */
function tsyc_bridge_side_effects(array $before, int $own, bool $include_modified): array
{
    $changes = array();
    foreach ($before['members'] as $member_id => $old) {
        $now = tsyc_bridge_member_snapshot((int) $member_id);
        foreach ($old as $field => $value) {
            if ($now[$field] !== $value && ($include_modified || $field !== 'modified_gmt')) {
                $changes[] = array('code' => 'MEMBER_CHANGED', 'product_id' => (int) $member_id, 'field' => $field,
                                   'before' => $value, 'after' => $now[$field]);
            }
        }
    }
    foreach (tsyc_bridge_other_writes($own) as $write) {
        if ($write['field'] === 'post_save') {
            // The bridge never saves another product, so whatever did is not the bridge.
            $changes[] = array('code' => 'MEMBER_SAVED', 'product_id' => $write['product_id'], 'field' => 'post_save');
        } elseif (!isset($before['members'][(string) $write['product_id']])) {
            $changes[] = array('code' => 'OUTSIDE_GROUP_WRITE', 'product_id' => $write['product_id'], 'field' => $write['field']);
        }
    }
    return $changes;
}

function tsyc_bridge_other_writes(int $own): array
{
    $writes = array();
    foreach ((array) ($GLOBALS['tsyc_bridge_writes'] ?? array()) as $write) {
        if ($write['product_id'] !== $own) {
            $writes[$write['product_id'] . ':' . $write['field']] = $write;
        }
    }
    return array_values($writes);
}

/** A hook must not have given the new draft a price, other inventory or another book's SKU. */
function tsyc_bridge_draft_shape_problems(int $id, string $product_code): array
{
    $now = tsyc_bridge_member_snapshot($id);
    $wanted = array('status' => 'draft', '_regular_price' => '', '_sale_price' => '', '_price' => '',
                    '_manage_stock' => 'yes', '_backorders' => 'yes');
    $problems = array();
    foreach ($wanted as $field => $value) {
        if ($now[$field] !== $value) {
            $problems[] = array('code' => 'DRAFT_SHAPE', 'product_id' => $id, 'field' => $field, 'before' => $value, 'after' => $now[$field]);
        }
    }
    if ((int) $now['_stock'] !== 0 || $now['_stock'] === '') {
        $problems[] = array('code' => 'DRAFT_SHAPE', 'product_id' => $id, 'field' => '_stock', 'before' => '0', 'after' => $now['_stock']);
    }
    if (!in_array($now['_sku'], array('', $product_code), true)) {
        $problems[] = array('code' => 'DRAFT_SHAPE', 'product_id' => $id, 'field' => '_sku', 'before' => '', 'after' => $now['_sku']);
    }
    return $problems;
}

/** Stops this request and marks the draft so a replay cannot carry on past it. */
function tsyc_bridge_side_effect_error(int $id, array $changes, bool $linked): WP_Error
{
    update_post_meta($id, TSYC_META_STATE, TSYC_STATE_SIDE_EFFECT);
    update_post_meta($id, '_tsyc_side_effects', $changes);
    $post = get_post($id);
    return tsyc_bridge_error('tsyc_sibling_side_effect', "Creating #$id changed protected values. Stopped; nothing was repaired.", 409,
                             array('product_id' => $id, 'linked' => $linked, 'changes' => $changes,
                                   'modified_gmt' => $post instanceof WP_Post ? tsyc_bridge_modified_gmt($post) : null));
}

function tsyc_bridge_create_translation(WP_REST_Request $request)
{
    $ready = tsyc_bridge_polylang_ready();
    if (is_wp_error($ready)) {
        return $ready;
    }
    $body = $request->get_json_params();
    $body = tsyc_bridge_validate_create(is_array($body) ? $body : array());
    if (is_wp_error($body)) {
        return $body;
    }
    $key = tsyc_bridge_lock_key((int) $body['source_id']);
    $token = tsyc_bridge_acquire_lock($key);
    if ($token === null) {
        return tsyc_bridge_error('tsyc_busy', 'Another request is working on this translation group.', 409);
    }
    // Also released when the worker dies mid-request; a hard crash expires by age.
    register_shutdown_function('tsyc_bridge_release_lock', $key, $token);
    $GLOBALS['tsyc_bridge_writes'] = array();
    tsyc_bridge_watch_writes(true);
    $result = tsyc_bridge_create_locked($body);
    tsyc_bridge_watch_writes(false);
    return $result;
}

function tsyc_bridge_create_locked(array $body)
{
    $source_id = (int) $body['source_id'];
    $lang = (string) $body['lang'];
    $fingerprint = tsyc_bridge_fingerprint($body);
    $found = tsyc_bridge_find_by_request((string) $body['request_id']);
    if (count($found) > 1) {
        return tsyc_bridge_error('tsyc_request_duplicated', 'Several products carry this request.', 409);
    }

    $replayed = !empty($found);
    if ($replayed) {
        $id = (int) $found[0];
        if ((string) get_post_meta($id, TSYC_META_FINGERPRINT, true) !== $fingerprint) {
            return tsyc_bridge_error('tsyc_request_conflict', 'This request_id was used for a different request.', 409);
        }
        if (get_post_status($id) === 'trash') {
            return tsyc_bridge_error('tsyc_request_rolled_back', 'This request was rolled back; a new request is required.', 409);
        }
        // A finished request only reports: re-linking here could undo a change
        // the owner made after it finished.
        if ((string) get_post_meta($id, TSYC_META_STATE, true) === TSYC_STATE_LINKED) {
            return tsyc_bridge_report_finished($id, $source_id, $lang, (string) $body['request_id']);
        }
        if ((string) get_post_meta($id, TSYC_META_STATE, true) === TSYC_STATE_SIDE_EFFECT) {
            return tsyc_bridge_error('tsyc_sibling_side_effect', "#$id stopped on a side effect; only a person may continue.", 409,
                                     array('product_id' => $id, 'changes' => get_post_meta($id, '_tsyc_side_effects', true)));
        }
        // Compared on every attempt, so a crash cannot hide what the first attempt did.
        $before = json_decode((string) get_post_meta($id, TSYC_META_SNAPSHOT, true), true);
        if (!is_array($before) || !isset($before['members'], $before['group'])) {
            return tsyc_bridge_error('tsyc_snapshot_missing', "#$id has no group snapshot; only a person may continue.", 409,
                                     array('product_id' => $id));
        }
    } else {
        $source = tsyc_bridge_validate_source($body);
        if (is_wp_error($source)) {
            return $source;
        }
        $existing = tsyc_bridge_translations($source_id, pll_default_language());
        if (isset($existing[$lang])) {
            return tsyc_bridge_error('tsyc_translation_exists', "$lang is already linked to #{$existing[$lang]}.", 409,
                                     array('product_id' => (int) $existing[$lang]));
        }
        $orphans = tsyc_bridge_find_unreconciled((string) $body['internal_product_id'], $lang);
        if (!empty($orphans)) {
            return tsyc_bridge_error('tsyc_unreconciled_copy', 'A product already claims this book and language.', 409,
                                     array('product_ids' => $orphans));
        }
        // Taken before the insert and stored with it, so a resume compares with the true start.
        $before = tsyc_bridge_group_snapshot($existing);
        $id = tsyc_bridge_insert_draft($body, $source, $fingerprint, $before);
        if (is_wp_error($id)) {
            return $id;
        }
    }
    do_action('tsyc_bridge_checkpoint', 'after_create');

    $current = pll_get_post_language($id);
    if ($current !== $lang) {
        pll_set_post_language($id, $lang);
        if (pll_get_post_language($id) !== $lang) {
            return tsyc_bridge_error('tsyc_language_unverified', "#$id did not read back as $lang.", 500, array('product_id' => $id));
        }
    }
    update_post_meta($id, TSYC_META_STATE, TSYC_STATE_LANGUAGE_SET);
    do_action('tsyc_bridge_checkpoint', 'after_language');

    // Nothing protected may have moved, and the source must still be TSYC's
    // unchanged draft, at the last moment before the link is written.
    $changes = array_merge(tsyc_bridge_side_effects($before, $id, false),
                           tsyc_bridge_draft_shape_problems($id, (string) $body['product_code']));
    if (!empty($changes)) {
        return tsyc_bridge_side_effect_error($id, $changes, false);
    }
    $source = tsyc_bridge_validate_source($body);
    if (is_wp_error($source)) {
        $source->add_data(array_merge((array) $source->get_error_data(), array('product_id' => $id)));
        return $source;
    }

    // The full current group plus the new member: leaving a member out would unlink it.
    $group = tsyc_bridge_translations($source_id, pll_default_language());
    do_action('tsyc_bridge_checkpoint', 'before_link');
    if (isset($group[$lang]) && (int) $group[$lang] !== $id) {
        return tsyc_bridge_error('tsyc_translation_exists', "$lang is already linked to #{$group[$lang]}.", 409,
                                 array('product_id' => (int) $group[$lang]));
    }
    $group[$lang] = $id;
    pll_save_post_translations($group);
    do_action('tsyc_bridge_checkpoint', 'after_link');

    $view = tsyc_bridge_group_view($id, $lang);
    $source_view = tsyc_bridge_translations($source_id, pll_default_language());
    if (empty($view['consistent']) || (int) ($view['translations'][pll_default_language()] ?? 0) !== $source_id
        || (int) ($source_view[$lang] ?? 0) !== $id) {
        return tsyc_bridge_error('tsyc_link_unverified', "#$id did not read back linked to #$source_id.", 500,
                                 array('product_id' => $id, 'group' => $view));
    }
    // The group must be exactly what it was plus this member, and every member untouched.
    $expected = $before['group'];
    $expected[$lang] = $id;
    $actual = array_map('intval', $view['translations']);
    ksort($expected);
    ksort($actual);
    $changes = array_merge(tsyc_bridge_side_effects($before, $id, true),
                           tsyc_bridge_draft_shape_problems($id, (string) $body['product_code']));
    if ($expected !== $actual) {
        $changes[] = array('code' => 'GROUP_CHANGED', 'product_id' => $id, 'field' => 'translations',
                           'before' => $expected, 'after' => $actual);
    }
    if (!empty($changes)) {
        return tsyc_bridge_side_effect_error($id, $changes, true);
    }
    update_post_meta($id, TSYC_META_STATE, TSYC_STATE_LINKED);
    do_action('tsyc_bridge_checkpoint', 'before_response');

    return new WP_REST_Response(tsyc_bridge_operation_result($id, $source_id, $lang, (string) $body['request_id'], $replayed, $view),
                                $replayed ? 200 : 201);
}

/** @return WP_REST_Response|WP_Error The stored result, or a refusal if the link no longer holds. */
function tsyc_bridge_report_finished(int $id, int $source_id, string $lang, string $request_id)
{
    $view = tsyc_bridge_group_view($id, (string) pll_get_post_language($id));
    $default = pll_default_language();
    if (empty($view['consistent']) || (int) ($view['translations'][$lang] ?? 0) !== $id
        || (int) ($view['translations'][$default] ?? 0) !== $source_id) {
        return tsyc_bridge_error('tsyc_link_changed', "#$id is no longer linked to #$source_id; nothing was changed.", 409,
                                 array('product_id' => $id, 'group' => $view));
    }
    return new WP_REST_Response(tsyc_bridge_operation_result($id, $source_id, $lang, $request_id, true, $view), 200);
}

/** Products claiming this book and language that this request did not create. */
function tsyc_bridge_find_unreconciled(string $internal_product_id, string $lang): array
{
    return array_map('intval', get_posts(array(
        'post_type'        => 'product',
        'post_status'      => array('draft', 'pending', 'private', 'publish', 'future'),
        'meta_query'       => array(
            array('key' => TSYC_META_OWNER, 'value' => $internal_product_id),
            array('key' => TSYC_META_LANGUAGE, 'value' => $lang),
        ),
        'fields'           => 'ids',
        'posts_per_page'   => 5,
        'lang'             => '',
        'suppress_filters' => false,
        'no_found_rows'    => true,
    )));
}

/** @return int|WP_Error A new draft: no price and no SKU, ever. */
function tsyc_bridge_insert_draft(array $body, WC_Product $source, string $fingerprint, array $before)
{
    $product = new WC_Product_Simple();
    $product->set_name($body['content']['name']);
    $product->set_description($body['content']['description']);
    $product->set_short_description($body['content']['short_description']);
    $product->set_status('draft');
    $product->set_manage_stock(true);
    $product->set_stock_quantity(0);
    $product->set_backorders('yes');
    // The same physical book: its images and physical facts come from the
    // source on the server, never from the request.
    $product->set_image_id($source->get_image_id('edit'));
    $product->set_gallery_image_ids($source->get_gallery_image_ids('edit'));
    $product->set_weight($source->get_weight('edit'));
    $product->set_length($source->get_length('edit'));
    $product->set_width($source->get_width('edit'));
    $product->set_height($source->get_height('edit'));
    $product->set_attributes($source->get_attributes('edit'));
    $warnings = array();
    if (!empty($source->get_attributes('edit'))) {
        $warnings[] = 'ATTRIBUTE_LABELS_NOT_LOCALIZED';
    }
    $product->set_category_ids(tsyc_bridge_translated_terms($source->get_category_ids('edit'), (string) $body['lang'], 'CATEGORY', $warnings));
    $product->set_tag_ids(tsyc_bridge_translated_terms($source->get_tag_ids('edit'), (string) $body['lang'], 'TAG', $warnings));
    $product->update_meta_data('_tsyc_bridge_warnings', array_values(array_unique($warnings)));
    $product->update_meta_data(TSYC_META_REQUEST, $body['request_id']);
    $product->update_meta_data(TSYC_META_FINGERPRINT, $fingerprint);
    $product->update_meta_data(TSYC_META_OWNER, $body['internal_product_id']);
    $product->update_meta_data(TSYC_META_LANGUAGE, $body['lang']);
    $product->update_meta_data(TSYC_META_SOURCE, (string) (int) $body['source_id']);
    $product->update_meta_data(TSYC_META_AUTHORIZATION, $body['authorization_id']);
    $product->update_meta_data(TSYC_META_STATE, TSYC_STATE_CREATED);
    $product->update_meta_data(TSYC_META_SNAPSHOT, (string) wp_json_encode($before));
    try {
        $id = (int) $product->save();
    } catch (Exception $error) {
        return tsyc_bridge_error('tsyc_create_failed', $error->getMessage(), 500);
    }
    if ($id <= 0) {
        return tsyc_bridge_error('tsyc_create_failed', 'WooCommerce did not return a product ID.', 500);
    }
    return $id;
}

/** Polylang's translation of each term. A missing one is reported, never created or guessed. */
function tsyc_bridge_translated_terms(array $term_ids, string $lang, string $kind, array &$warnings): array
{
    $translated = array();
    foreach ($term_ids as $term_id) {
        $target = function_exists('pll_get_term') ? (int) pll_get_term((int) $term_id, $lang) : 0;
        if ($target > 0) {
            $translated[] = $target;
        } else {
            $warnings[] = $kind . '_NOT_TRANSLATED:' . (int) $term_id;
        }
    }
    return $translated;
}

function tsyc_bridge_operation_result(int $id, int $source_id, string $lang, string $request_id, bool $replayed, array $view): array
{
    clean_post_cache($id);
    $product = wc_get_product($id);
    $post = get_post($id);
    return array(
        'operation_id' => $request_id,
        'state'        => (string) get_post_meta($id, TSYC_META_STATE, true),
        'replayed'     => $replayed,
        'product_id'   => $id,
        'source_id'    => $source_id,
        'lang'         => $lang,
        'status'       => $post->post_status,
        'modified_gmt' => tsyc_bridge_modified_gmt($post),
        'text'         => array(
            'name'              => $product->get_name('edit'),
            'description'       => $product->get_description('edit'),
            'short_description' => $product->get_short_description('edit'),
        ),
        'translations' => $view['translations'],
        'warnings'     => array_values((array) get_post_meta($id, '_tsyc_bridge_warnings', true)),
        'read_back'    => array('verified' => true, 'bridge_version' => TSYC_BRIDGE_VERSION),
    );
}

/**
 * Undo one bridge create: unlink through Polylang's public API, then trash.
 * Refused once the draft changed after the caller's baseline (owner edit).
 */
function tsyc_bridge_rollback_translation(WP_REST_Request $request)
{
    $ready = tsyc_bridge_polylang_ready();
    if (is_wp_error($ready)) {
        return $ready;
    }
    $body = $request->get_json_params();
    $request_id = is_array($body) ? ($body['request_id'] ?? null) : null;
    $expected = is_array($body) ? ($body['expected_modified_gmt'] ?? null) : null;
    if (!is_string($request_id) || !is_string($expected) || !preg_match('/^[A-Za-z0-9._:-]{8,128}$/', $request_id)) {
        return tsyc_bridge_error('tsyc_bad_rollback', 'request_id and expected_modified_gmt are required.', 400);
    }
    $found = tsyc_bridge_find_by_request($request_id);
    if (count($found) !== 1) {
        return tsyc_bridge_error('tsyc_rollback_target', 'The request does not name exactly one product.', 404);
    }
    $id = (int) $found[0];
    $post = get_post($id);
    if ($post->post_status === 'trash') {
        return array('operation_id' => $request_id, 'rolled_back' => true, 'already' => true, 'product_id' => $id);
    }
    if ($post->post_status !== 'draft') {
        return tsyc_bridge_error('tsyc_rollback_refused', "#$id is {$post->post_status}.", 409);
    }
    if (tsyc_bridge_modified_gmt($post) !== $expected) {
        return tsyc_bridge_error('tsyc_rollback_refused', "#$id changed after the baseline: never undone automatically.", 409);
    }
    $source_id = (int) get_post_meta($id, TSYC_META_SOURCE, true);
    if ($source_id <= 0) {
        return tsyc_bridge_error('tsyc_rollback_target', "#$id does not name its source.", 404);
    }
    // The same lock as a create: this also rewrites the whole group.
    $key = tsyc_bridge_lock_key($source_id);
    $token = tsyc_bridge_acquire_lock($key);
    if ($token === null) {
        return tsyc_bridge_error('tsyc_busy', 'Another request is working on this translation group.', 409);
    }
    register_shutdown_function('tsyc_bridge_release_lock', $key, $token);
    $GLOBALS['tsyc_bridge_writes'] = array();
    tsyc_bridge_watch_writes(true);
    $result = tsyc_bridge_rollback_locked($id, $request_id);
    tsyc_bridge_watch_writes(false);
    return $result;
}

function tsyc_bridge_rollback_locked(int $id, string $request_id)
{
    $lang = (string) pll_get_post_language($id);
    $group = tsyc_bridge_translations($id, $lang);
    unset($group[$lang]);
    $before = tsyc_bridge_group_snapshot($group);
    if (!empty($group)) {
        // Saving the group without this member is what Polylang's API uses to unlink it.
        pll_save_post_translations($group);
    }
    $remaining = tsyc_bridge_translations($id, $lang);
    if (count($remaining) !== 1) {
        return tsyc_bridge_error('tsyc_rollback_unverified', "#$id is still linked.", 500);
    }
    if (!wp_trash_post($id)) {
        return tsyc_bridge_error('tsyc_rollback_failed', "#$id could not be trashed.", 500);
    }
    $changes = tsyc_bridge_side_effects($before, $id, true);
    foreach ($group as $member_lang => $member_id) {
        $kept = tsyc_bridge_translations((int) $member_id, (string) $member_lang);
        ksort($kept);
        $wanted = $group;
        ksort($wanted);
        if (array_map('intval', $kept) !== array_map('intval', $wanted)) {
            $changes[] = array('code' => 'GROUP_CHANGED', 'product_id' => (int) $member_id, 'field' => 'translations',
                               'before' => $wanted, 'after' => $kept);
        }
    }
    if (!empty($changes)) {
        return tsyc_bridge_error('tsyc_sibling_side_effect', "#$id was rolled back, but protected values changed.", 409,
                                 array('product_id' => $id, 'rolled_back' => true, 'changes' => $changes));
    }
    return array('operation_id' => $request_id, 'rolled_back' => true, 'already' => false, 'product_id' => $id);
}
