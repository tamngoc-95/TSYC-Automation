-- ============================================================
-- Tiệm Sách Yêu Con Automation
-- Migration: 001_initial_schema
-- Purpose: Create the initial database foundation
-- ============================================================

begin;

-- ============================================================
-- 1. BATCHES
-- One record represents one processing batch.
-- Example: FB-2026-001
-- ============================================================

create table if not exists public.batches (
    batch_id uuid primary key default gen_random_uuid(),

    batch_code text not null unique,
    batch_name text,

    batch_status text not null default 'CREATED'
        check (
            batch_status in (
                'CREATED',
                'FACEBOOK_COLLECTING',
                'FACEBOOK_COLLECTED',
                'CANDIDATES_EXTRACTED',
                'REFERENCES_COLLECTING',
                'REFERENCES_COLLECTED',
                'AI_ENRICHMENT_RUNNING',
                'MANAGER_REVIEW',
                'DRAFT_GENERATION',
                'COMPLETED',
                'COMPLETED_WITH_WARNINGS',
                'FAILED'
            )
        ),

    description text,

    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    completed_at timestamptz
);


-- ============================================================
-- 2. SOURCE URLS
-- Stores the authorized input URLs for a batch.
-- Python must only process URLs registered here.
-- ============================================================

create table if not exists public.source_urls (
    source_url_id uuid primary key default gen_random_uuid(),

    batch_id uuid not null
        references public.batches(batch_id)
        on delete cascade,

    source_type text not null
        check (
            source_type in (
                'FACEBOOK_POST',
                'PUBLISHER',
                'AUTHORIZED_SUPPLIER',
                'BOOKSTORE',
                'FAHASA',
                'PURCHASE_INVOICE',
                'PURCHASE_ORDER',
                'SUPPLIER_QUOTATION',
                'SUPPLIER_PRICE_LIST',
                'OTHER'
            )
        ),

    source_url text,
    source_name text,

    is_authorized boolean not null default false,

    crawl_status text not null default 'PENDING'
        check (
            crawl_status in (
                'PENDING',
                'IN_PROGRESS',
                'COLLECTED',
                'SKIPPED',
                'FAILED'
            )
        ),

    last_crawled_at timestamptz,
    last_error text,

    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),

    unique (batch_id, source_type, source_url)
);


-- ============================================================
-- 3. RAW PAGES
-- Stores collected raw content before Claude extraction.
-- Raw data must never be overwritten by AI-generated content.
-- ============================================================

create table if not exists public.raw_pages (
    raw_page_id uuid primary key default gen_random_uuid(),

    batch_id uuid not null
        references public.batches(batch_id)
        on delete cascade,

    source_url_id uuid
        references public.source_urls(source_url_id)
        on delete set null,

    page_type text not null
        check (
            page_type in (
                'FACEBOOK_POST',
                'PUBLISHER_PAGE',
                'SUPPLIER_PAGE',
                'BOOKSTORE_PAGE',
                'FAHASA_PAGE',
                'DOCUMENT_TEXT',
                'OTHER'
            )
        ),

    page_url text,
    raw_title text,
    raw_text text,
    raw_html text,

    content_hash text,
    collected_at timestamptz not null default now(),

    collector_name text,
    collector_version text,

    created_at timestamptz not null default now()
);


-- ============================================================
-- 4. PRODUCT CANDIDATES
-- One row represents one distinct book, ISBN, or product.
-- ============================================================

create table if not exists public.product_candidates (
    candidate_id uuid primary key default gen_random_uuid(),

    batch_id uuid not null
        references public.batches(batch_id)
        on delete cascade,

    candidate_code text not null unique,

    candidate_type text not null default 'SINGLE_BOOK'
        check (
            candidate_type in (
                'SINGLE_BOOK',
                'BOOK_COMBO',
                'BOOK_SET',
                'ACTIVITY_PRODUCT',
                'OTHER'
            )
        ),

    combo_group_code text,

    extracted_title text not null,
    possible_isbn text,

    verified_title text,
    verified_isbn text,
    verified_author text,
    verified_publisher text,
    verified_page_count integer,
    verified_weight_grams numeric(10,2),

    verified_length_cm numeric(10,2),
    verified_width_cm numeric(10,2),
    verified_height_cm numeric(10,2),

    identity_status text not null default 'IDENTITY_PENDING'
        check (
            identity_status in (
                'IDENTITY_PENDING',
                'IDENTITY_VERIFIED',
                'ACCEPTED_WITH_LIMITED_METADATA',
                'IDENTITY_CONFLICT',
                'REJECTED'
            )
        ),

    workflow_status text not null default 'EXTRACTED'
        check (
            workflow_status in (
                'EXTRACTED',
                'IDENTITY_PENDING',
                'IDENTITY_VERIFIED',
                'IDENTITY_CONFLICT',
                'CONTENT_PENDING',
                'CONTENT_READY',
                'CONTENT_APPROVED',
                'PRICE_PENDING',
                'PRICE_APPROVED',
                'IMAGE_PENDING',
                'IMAGE_APPROVED',
                'READY_FOR_DRAFT',
                'DRAFT_CREATED',
                'READY_TO_PUBLISH',
                'PUBLISHED',
                'REJECTED'
            )
        ),

    extraction_confidence numeric(5,4)
        check (
            extraction_confidence is null
            or extraction_confidence between 0 and 1
        ),

    identity_confidence numeric(5,4)
        check (
            identity_confidence is null
            or identity_confidence between 0 and 1
        ),

    source_evidence jsonb not null default '{}'::jsonb,
    conflict_fields jsonb not null default '[]'::jsonb,

    review_required boolean not null default false,
    review_reason text,
    decision_reason text,

    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),

    unique (batch_id, candidate_code)
);


-- ============================================================
-- 5. PRODUCT REFERENCES
-- Stores one row per candidate and external reference source.
-- Reference values do not automatically overwrite master data.
-- ============================================================

create table if not exists public.product_references (
    reference_id uuid primary key default gen_random_uuid(),

    candidate_id uuid not null
        references public.product_candidates(candidate_id)
        on delete cascade,

    source_url_id uuid
        references public.source_urls(source_url_id)
        on delete set null,

    source_type text not null
        check (
            source_type in (
                'PUBLISHER',
                'AUTHORIZED_SUPPLIER',
                'BOOKSTORE',
                'FAHASA',
                'FACEBOOK',
                'OTHER'
            )
        ),

    source_name text,
    source_url text,

    reference_title text,
    reference_isbn text,
    reference_author text,
    reference_publisher text,

    reference_page_count integer,
    reference_weight_grams numeric(10,2),

    reference_length_cm numeric(10,2),
    reference_width_cm numeric(10,2),
    reference_height_cm numeric(10,2),

    reference_cover_price_vnd numeric(14,2),
    reference_description text,
    reference_image_url text,

    match_decision text
        check (
            match_decision is null
            or match_decision in (
                'MATCH',
                'POSSIBLE_MATCH',
                'DIFFERENT_EDITION',
                'NO_MATCH',
                'MANUAL_REVIEW'
            )
        ),

    match_confidence numeric(5,4)
        check (
            match_confidence is null
            or match_confidence between 0 and 1
        ),

    source_priority integer,
    raw_metadata jsonb not null default '{}'::jsonb,

    collected_at timestamptz,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);


-- ============================================================
-- 6. PRODUCT IMAGES
-- Stores image metadata. The database stores paths and metadata,
-- while binary image files are stored in Supabase Storage.
-- ============================================================

create table if not exists public.product_images (
    image_id uuid primary key default gen_random_uuid(),

    candidate_id uuid not null
        references public.product_candidates(candidate_id)
        on delete cascade,

    reference_id uuid
        references public.product_references(reference_id)
        on delete set null,

    source_type text not null
        check (
            source_type in (
                'STORE_OWNED',
                'PUBLISHER',
                'AUTHORIZED_SUPPLIER',
                'FACEBOOK',
                'FAHASA',
                'BOOKSTORE',
                'OTHER'
            )
        ),

    source_url text,
    storage_bucket text,
    storage_path text,

    original_file_name text,
    mime_type text,

    width_pixels integer,
    height_pixels integer,
    file_size_bytes bigint,

    image_hash text,

    image_role text
        check (
            image_role is null
            or image_role in (
                'FRONT_COVER',
                'BACK_COVER',
                'INSIDE_PAGE',
                'COMBO_IMAGE',
                'LIFESTYLE',
                'OTHER'
            )
        ),

    usage_rights_status text not null default 'RIGHTS_UNKNOWN'
        check (
            usage_rights_status in (
                'STORE_OWNED',
                'PUBLISHER_APPROVED',
                'SUPPLIER_APPROVED',
                'REFERENCE_ONLY',
                'RIGHTS_UNKNOWN'
            )
        ),

    is_main_image_candidate boolean not null default false,
    is_selected_main_image boolean not null default false,
    is_publish_eligible boolean not null default false,

    image_status text not null default 'PENDING'
        check (
            image_status in (
                'PENDING',
                'DOWNLOADED',
                'VALIDATED',
                'REJECTED',
                'FAILED'
            )
        ),

    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);


-- ============================================================
-- 7. REVIEW ISSUES
-- Stores every issue requiring manager or technical review.
-- ============================================================

create table if not exists public.review_issues (
    review_issue_id uuid primary key default gen_random_uuid(),

    batch_id uuid not null
        references public.batches(batch_id)
        on delete cascade,

    candidate_id uuid
        references public.product_candidates(candidate_id)
        on delete cascade,

    issue_type text not null
        check (
            issue_type in (
                'IDENTITY_CONFLICT',
                'ISBN_INVALID',
                'DUPLICATE_CANDIDATE',
                'DUPLICATE_ISBN',
                'METADATA_CONFLICT',
                'IMAGE_MAPPING_AMBIGUOUS',
                'IMAGE_RIGHTS_UNKNOWN',
                'PRICING_DATA_MISSING',
                'CONTENT_VALIDATION_FAILED',
                'CRAWL_FAILED',
                'AI_OUTPUT_INVALID',
                'OTHER'
            )
        ),

    issue_severity text not null default 'WARNING'
        check (
            issue_severity in (
                'INFO',
                'WARNING',
                'ERROR',
                'BLOCKING'
            )
        ),

    issue_status text not null default 'OPEN'
        check (
            issue_status in (
                'OPEN',
                'IN_REVIEW',
                'RESOLVED',
                'IGNORED'
            )
        ),

    issue_title text not null,
    issue_description text,

    detected_by text,
    resolution_notes text,
    resolved_at timestamptz,

    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);


-- ============================================================
-- 8. PROCESS LOGS
-- Technical and business process audit trail.
-- ============================================================

create table if not exists public.process_logs (
    process_log_id bigint generated always as identity primary key,

    batch_id uuid
        references public.batches(batch_id)
        on delete cascade,

    candidate_id uuid
        references public.product_candidates(candidate_id)
        on delete cascade,

    process_name text not null,
    process_step text,

    log_level text not null default 'INFO'
        check (
            log_level in (
                'DEBUG',
                'INFO',
                'WARNING',
                'ERROR',
                'CRITICAL'
            )
        ),

    status text,
    message text not null,

    error_code text,
    error_details jsonb,

    run_id uuid,
    created_at timestamptz not null default now()
);


-- ============================================================
-- INDEXES
-- ============================================================

create index if not exists idx_source_urls_batch_id
    on public.source_urls(batch_id);

create index if not exists idx_source_urls_crawl_status
    on public.source_urls(crawl_status);

create index if not exists idx_raw_pages_batch_id
    on public.raw_pages(batch_id);

create index if not exists idx_raw_pages_source_url_id
    on public.raw_pages(source_url_id);

create index if not exists idx_candidates_batch_id
    on public.product_candidates(batch_id);

create index if not exists idx_candidates_workflow_status
    on public.product_candidates(workflow_status);

create index if not exists idx_candidates_identity_status
    on public.product_candidates(identity_status);

create index if not exists idx_candidates_verified_isbn
    on public.product_candidates(verified_isbn);

create index if not exists idx_references_candidate_id
    on public.product_references(candidate_id);

create index if not exists idx_references_reference_isbn
    on public.product_references(reference_isbn);

create index if not exists idx_images_candidate_id
    on public.product_images(candidate_id);

create index if not exists idx_images_hash
    on public.product_images(image_hash);

create index if not exists idx_review_issues_candidate_id
    on public.review_issues(candidate_id);

create index if not exists idx_review_issues_status
    on public.review_issues(issue_status);

create index if not exists idx_process_logs_batch_id
    on public.process_logs(batch_id);

create index if not exists idx_process_logs_candidate_id
    on public.process_logs(candidate_id);

create index if not exists idx_process_logs_created_at
    on public.process_logs(created_at);


-- ============================================================
-- UPDATED_AT TRIGGER
-- Automatically refreshes updated_at after each update.
-- ============================================================

create or replace function public.set_updated_at()
returns trigger
language plpgsql
as $$
begin
    new.updated_at = now();
    return new;
end;
$$;

drop trigger if exists trg_batches_updated_at
    on public.batches;

create trigger trg_batches_updated_at
before update on public.batches
for each row
execute function public.set_updated_at();


drop trigger if exists trg_source_urls_updated_at
    on public.source_urls;

create trigger trg_source_urls_updated_at
before update on public.source_urls
for each row
execute function public.set_updated_at();


drop trigger if exists trg_candidates_updated_at
    on public.product_candidates;

create trigger trg_candidates_updated_at
before update on public.product_candidates
for each row
execute function public.set_updated_at();


drop trigger if exists trg_references_updated_at
    on public.product_references;

create trigger trg_references_updated_at
before update on public.product_references
for each row
execute function public.set_updated_at();


drop trigger if exists trg_images_updated_at
    on public.product_images;

create trigger trg_images_updated_at
before update on public.product_images
for each row
execute function public.set_updated_at();


drop trigger if exists trg_review_issues_updated_at
    on public.review_issues;

create trigger trg_review_issues_updated_at
before update on public.review_issues
for each row
execute function public.set_updated_at();


-- ============================================================
-- ROW LEVEL SECURITY
-- Enable RLS now. No public policies are created at this stage.
-- Python will use a protected backend secret key.
-- ============================================================

alter table public.batches enable row level security;
alter table public.source_urls enable row level security;
alter table public.raw_pages enable row level security;
alter table public.product_candidates enable row level security;
alter table public.product_references enable row level security;
alter table public.product_images enable row level security;
alter table public.review_issues enable row level security;
alter table public.process_logs enable row level security;

commit;
