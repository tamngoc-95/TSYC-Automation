-- Map one internal product (one physical book) to its WooCommerce
-- product per storefront language (vi / en / de).
--
-- Each language copy is a separate WooCommerce product ID (Polylang,
-- docs/POLYLANG_EXISTING_ARCHITECTURE.md). woocommerce_product_syncs keeps
-- one row per internal product (the VI draft TSYC created); this table is
-- the per-language record the multilingual adapter needs:
--   * TSYC_CREATED rows are products TSYC itself created (proof of
--     ownership is this record, never copied `_tsyc_*` meta);
--   * POLYLANG_LINKED rows are existing copies proven by a staging-proven
--     Polylang link reader; they are never TSYC-owned and never written.
--
-- language_code is the storefront language, not the book's own language
-- (internal_products.language_code). Additive only: no existing table or
-- row changes. Rows are written by approved TSYC scripts only.

create table if not exists public.woocommerce_language_products (
    language_product_id uuid
        primary key
        default gen_random_uuid(),

    internal_product_id uuid
        not null
        references public.internal_products(internal_product_id)
        on delete restrict,

    language_code text
        not null,

    woocommerce_product_id bigint,

    origin text
        not null,

    mapping_status text
        not null
        default 'PENDING_CREATE',

    -- The VI row points at the existing sync row it was derived from.
    sync_id uuid
        references public.woocommerce_product_syncs(sync_id)
        on delete restrict,

    product_content_id uuid
        references public.product_contents(product_content_id)
        on delete restrict,

    -- Idempotency key of a TSYC create request (EN/DE).
    request_id text,

    -- Remote state right after TSYC's last confirmed write.
    baseline_source text,
    baseline_modified_gmt text,
    baseline_text jsonb,

    link_mechanism text,
    link_verified_at timestamptz,
    link_evidence jsonb
        not null
        default '{}'::jsonb,

    created_at timestamptz
        not null
        default now(),

    updated_at timestamptz
        not null
        default now(),

    constraint woocommerce_language_products_language_key
        unique (internal_product_id, language_code),

    constraint woocommerce_language_products_language_check
        check (language_code in ('vi', 'en', 'de')),

    constraint woocommerce_language_products_origin_check
        check (origin in ('TSYC_CREATED', 'POLYLANG_LINKED')),

    constraint woocommerce_language_products_status_check
        check (
            mapping_status in (
                'PENDING_CREATE',
                'RECORDED',
                'REMOTE_REMOVED',
                'CONFLICT'
            )
        ),

    constraint woocommerce_language_products_baseline_source_check
        check (
            baseline_source is null
            or baseline_source in ('CREATE_RESPONSE', 'TSYC_WRITE_RESPONSE')
        ),

    -- A recorded mapping always names its Woo product.
    constraint woocommerce_language_products_recorded_id_check
        check (
            mapping_status = 'PENDING_CREATE'
            or woocommerce_product_id is not null
        ),

    -- Only TSYC's own VI row derives from the sync row.
    constraint woocommerce_language_products_sync_check
        check (
            sync_id is null
            or (language_code = 'vi' and origin = 'TSYC_CREATED')
        ),

    -- A TSYC translation is created with an idempotency key.
    constraint woocommerce_language_products_request_check
        check (
            origin <> 'TSYC_CREATED'
            or language_code = 'vi'
            or request_id is not null
        ),

    -- A linked copy is recorded only with its proven link.
    constraint woocommerce_language_products_link_check
        check (
            origin <> 'POLYLANG_LINKED'
            or (link_mechanism is not null and link_verified_at is not null)
        )
);


create unique index if not exists
idx_woocommerce_language_products_product_id
on public.woocommerce_language_products(
    woocommerce_product_id
)
where woocommerce_product_id is not null;


create unique index if not exists
idx_woocommerce_language_products_request_id
on public.woocommerce_language_products(
    request_id
)
where request_id is not null;


create index if not exists
idx_woocommerce_language_products_status
on public.woocommerce_language_products(
    mapping_status,
    updated_at
);


alter table public.woocommerce_language_products enable row level security;
