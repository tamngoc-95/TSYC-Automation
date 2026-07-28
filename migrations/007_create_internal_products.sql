-- Create the internal product catalog table.
--
-- One verified product candidate can create one internal product.
-- This table will later be used for metadata completion, pricing,
-- image approval, and WooCommerce draft creation.

create table if not exists public.internal_products (
    internal_product_id uuid
        primary key
        default gen_random_uuid(),

    candidate_id uuid
        not null
        references public.product_candidates(candidate_id)
        on delete restrict,

    primary_reference_id uuid
        references public.product_references(reference_id)
        on delete set null,

    product_code text
        not null,

    product_type text
        not null
        default 'BOOK',

    title text
        not null,

    author text,

    isbn text,

    publisher text,

    language_code text,

    page_count integer,

    weight_grams numeric,

    length_cm numeric,

    width_cm numeric,

    height_cm numeric,

    cover_price_vnd numeric,

    purchase_price_vnd numeric,

    purchase_currency text
        default 'VND',

    suggested_price_eur numeric,

    metadata_status text
        not null
        default 'INCOMPLETE',

    pricing_status text
        not null
        default 'PENDING',

    image_status text
        not null
        default 'PENDING',

    content_status text
        not null
        default 'PENDING',

    woocommerce_status text
        not null
        default 'NOT_CREATED',

    review_required boolean
        not null
        default false,

    review_reason text,

    is_active boolean
        not null
        default true,

    product_metadata jsonb
        not null
        default '{}'::jsonb,

    created_at timestamptz
        not null
        default now(),

    updated_at timestamptz
        not null
        default now(),

    constraint internal_products_candidate_id_key
        unique (candidate_id),

    constraint internal_products_product_code_key
        unique (product_code),

    constraint internal_products_product_type_check
        check (
            product_type in (
                'BOOK',
                'BOOK_COMBO',
                'BOOK_SET',
                'ACTIVITY_PRODUCT',
                'OTHER'
            )
        ),

    constraint internal_products_page_count_check
        check (
            page_count is null
            or page_count > 0
        ),

    constraint internal_products_weight_check
        check (
            weight_grams is null
            or weight_grams > 0
        ),

    constraint internal_products_dimensions_check
        check (
            (
                length_cm is null
                or length_cm > 0
            )
            and (
                width_cm is null
                or width_cm > 0
            )
            and (
                height_cm is null
                or height_cm > 0
            )
        ),

    constraint internal_products_cover_price_check
        check (
            cover_price_vnd is null
            or cover_price_vnd >= 0
        ),

    constraint internal_products_purchase_price_check
        check (
            purchase_price_vnd is null
            or purchase_price_vnd >= 0
        ),

    constraint internal_products_suggested_price_check
        check (
            suggested_price_eur is null
            or suggested_price_eur >= 0
        ),

    constraint internal_products_metadata_status_check
        check (
            metadata_status in (
                'INCOMPLETE',
                'READY',
                'REVIEW_REQUIRED',
                'APPROVED'
            )
        ),

    constraint internal_products_pricing_status_check
        check (
            pricing_status in (
                'PENDING',
                'CALCULATED',
                'REVIEW_REQUIRED',
                'APPROVED'
            )
        ),

    constraint internal_products_image_status_check
        check (
            image_status in (
                'PENDING',
                'AVAILABLE',
                'REVIEW_REQUIRED',
                'APPROVED'
            )
        ),

    constraint internal_products_content_status_check
        check (
            content_status in (
                'PENDING',
                'DRAFTED',
                'REVIEW_REQUIRED',
                'APPROVED'
            )
        ),

    constraint internal_products_woocommerce_status_check
        check (
            woocommerce_status in (
                'NOT_CREATED',
                'READY_FOR_DRAFT',
                'DRAFT_CREATED',
                'READY_TO_PUBLISH',
                'PUBLISHED',
                'FAILED'
            )
        )
);


create index if not exists
idx_internal_products_candidate_id
on public.internal_products(candidate_id);


create index if not exists
idx_internal_products_primary_reference_id
on public.internal_products(primary_reference_id);


create index if not exists
idx_internal_products_isbn
on public.internal_products(isbn)
where isbn is not null;


create index if not exists
idx_internal_products_workflow
on public.internal_products(
    metadata_status,
    pricing_status,
    image_status,
    content_status,
    woocommerce_status
);


create index if not exists
idx_internal_products_review_required
on public.internal_products(
    review_required,
    updated_at
)
where review_required = true;