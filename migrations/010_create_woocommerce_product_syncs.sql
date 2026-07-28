-- Track WooCommerce product draft creation and synchronization.
--
-- One internal product can be connected to one WooCommerce product.
-- The table prevents duplicate draft creation and stores API results.

create table if not exists public.woocommerce_product_syncs (
    sync_id uuid
        primary key
        default gen_random_uuid(),

    internal_product_id uuid
        not null
        references public.internal_products(internal_product_id)
        on delete cascade,

    woocommerce_product_id bigint,

    woocommerce_status text
        not null
        default 'PENDING',

    product_sku text
        not null,

    product_name text
        not null,

    product_permalink text,

    request_payload jsonb
        not null
        default '{}'::jsonb,

    response_payload jsonb
        not null
        default '{}'::jsonb,

    error_code text,

    error_message text,

    sync_attempt_count integer
        not null
        default 0,

    last_attempt_at timestamptz,

    synced_at timestamptz,

    created_at timestamptz
        not null
        default now(),

    updated_at timestamptz
        not null
        default now(),

    constraint woocommerce_product_syncs_internal_product_key
        unique (internal_product_id),

    constraint woocommerce_product_syncs_product_sku_key
        unique (product_sku),

    constraint woocommerce_product_syncs_product_id_key
        unique (woocommerce_product_id),

    constraint woocommerce_product_syncs_status_check
        check (
            woocommerce_status in (
                'PENDING',
                'IN_PROGRESS',
                'DRAFT_CREATED',
                'FAILED'
            )
        ),

    constraint woocommerce_product_syncs_attempt_count_check
        check (
            sync_attempt_count >= 0
        )
);


create index if not exists
idx_woocommerce_product_syncs_status
on public.woocommerce_product_syncs(
    woocommerce_status,
    updated_at
);


create index if not exists
idx_woocommerce_product_syncs_product_id
on public.woocommerce_product_syncs(
    woocommerce_product_id
)
where woocommerce_product_id is not null;