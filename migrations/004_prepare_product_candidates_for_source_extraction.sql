-- Link each product candidate to its collected source page.
alter table public.product_candidates
add column if not exists raw_page_id uuid
    references public.raw_pages(raw_page_id)
    on delete set null;


-- Preserve the authorized source URL relationship.
alter table public.product_candidates
add column if not exists source_url_id uuid
    references public.source_urls(source_url_id)
    on delete set null;


-- Store the author extracted from the source before identity verification.
alter table public.product_candidates
add column if not exists extracted_author text;


-- Record how the initial candidate was extracted.
alter table public.product_candidates
add column if not exists extraction_method text
    not null
    default 'MANUAL';


-- Record the extractor implementation and version.
alter table public.product_candidates
add column if not exists extractor_name text;

alter table public.product_candidates
add column if not exists extractor_version text;


-- Allow only controlled extraction methods.
alter table public.product_candidates
drop constraint if exists
product_candidates_extraction_method_check;

alter table public.product_candidates
add constraint product_candidates_extraction_method_check
check (
    extraction_method in (
        'MANUAL',
        'RULE_BASED',
        'AI_ASSISTED',
        'HYBRID'
    )
);


-- Prevent the same candidate code from being inserted twice.
create unique index if not exists
ux_product_candidates_candidate_code
on public.product_candidates(candidate_code);


-- Support fast lookup by collected Facebook post.
create index if not exists
ix_product_candidates_raw_page_id
on public.product_candidates(raw_page_id);


-- Support fast lookup by authorized source URL.
create index if not exists
ix_product_candidates_source_url_id
on public.product_candidates(source_url_id);


-- Prevent the same extracted title from being created twice
-- from the same collected page.
create unique index if not exists
ux_product_candidates_raw_page_title
on public.product_candidates(
    raw_page_id,
    lower(trim(extracted_title))
)
where raw_page_id is not null;