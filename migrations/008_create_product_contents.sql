-- Store product content prepared for WooCommerce.
--
-- Content is generated from verified internal product data and
-- reviewed before a WooCommerce draft is created.

create table if not exists public.product_contents (
    product_content_id uuid
        primary key
        default gen_random_uuid(),

    internal_product_id uuid
        not null
        references public.internal_products(internal_product_id)
        on delete cascade,

    content_language text
        not null
        default 'vi',

    product_name text
        not null,

    short_description text,

    long_description text,

    author_summary text,

    product_details text,

    seo_title text,

    seo_description text,

    content_status text
        not null
        default 'DRAFTED',

    generation_method text
        not null
        default 'RULE_BASED',

    generator_name text,

    generator_version text,

    review_required boolean
        not null
        default true,

    review_notes text,

    approved_at timestamptz,

    created_at timestamptz
        not null
        default now(),

    updated_at timestamptz
        not null
        default now(),

    constraint product_contents_internal_product_language_key
        unique (
            internal_product_id,
            content_language
        ),

    constraint product_contents_language_check
        check (
            content_language in (
                'vi',
                'de',
                'en'
            )
        ),

    constraint product_contents_status_check
        check (
            content_status in (
                'DRAFTED',
                'REVIEW_REQUIRED',
                'APPROVED',
                'REJECTED'
            )
        ),

    constraint product_contents_generation_method_check
        check (
            generation_method in (
                'MANUAL',
                'RULE_BASED',
                'AI_ASSISTED',
                'HYBRID'
            )
        )
);


create index if not exists
idx_product_contents_internal_product
on public.product_contents(internal_product_id);


create index if not exists
idx_product_contents_review_queue
on public.product_contents(
    content_status,
    review_required,
    updated_at
);


create index if not exists
idx_product_contents_language
on public.product_contents(content_language);