-- Prevent duplicate raw-page snapshots when the page content has not changed.
-- A new row is still allowed when the same URL returns different content.

create unique index if not exists
ux_raw_pages_source_url_content_hash
on public.raw_pages (
    source_url_id,
    content_hash
)
where source_url_id is not null
  and content_hash is not null;


-- Keep one normalized product reference per candidate and source URL.
-- Repeated collection should update the existing reference instead of
-- inserting another row.

create unique index if not exists
ux_product_references_candidate_source_url
on public.product_references (
    candidate_id,
    source_url_id
)
where source_url_id is not null;


-- Improve crawler queue lookup for selected reference sources.

create index if not exists
ix_source_urls_reference_crawl_queue
on public.source_urls (
    crawl_status,
    source_type,
    created_at
)
where source_type in (
    'PUBLISHER',
    'AUTHORIZED_SUPPLIER',
    'BOOKSTORE',
    'FAHASA'
);


-- Improve discovery queue lookup.

create index if not exists
ix_candidate_reference_sources_crawl_queue
on public.candidate_reference_sources (
    discovery_status,
    is_selected_for_crawl,
    source_url_id
)
where is_selected_for_crawl = true;