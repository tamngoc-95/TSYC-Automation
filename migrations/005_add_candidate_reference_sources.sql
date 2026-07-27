-- Link product candidates to reference URLs discovered
-- from publishers, suppliers, bookstores, Fahasa, or other sources.
create table if not exists public.candidate_reference_sources (
    discovery_id uuid
        primary key
        default gen_random_uuid(),

    candidate_id uuid
        not null
        references public.product_candidates(candidate_id)
        on delete cascade,

    source_url_id uuid
        not null
        references public.source_urls(source_url_id)
        on delete cascade,

    discovery_method text
        not null
        default 'MANUAL',

    discovery_query text,

    discovery_rank integer,

    discovery_confidence numeric,

    discovery_status text
        not null
        default 'DISCOVERED',

    selection_reason text,

    is_selected_for_crawl boolean
        not null
        default false,

    discovered_at timestamptz
        not null
        default now(),

    reviewed_at timestamptz,

    created_at timestamptz
        not null
        default now(),

    updated_at timestamptz
        not null
        default now(),

    constraint candidate_reference_sources_candidate_url_key
        unique (
            candidate_id,
            source_url_id
        ),

    constraint candidate_reference_sources_discovery_method_check
        check (
            discovery_method in (
                'MANUAL',
                'SEARCH_ENGINE',
                'SITE_SEARCH',
                'ISBN_LOOKUP',
                'AI_ASSISTED',
                'OTHER'
            )
        ),

    constraint candidate_reference_sources_discovery_status_check
        check (
            discovery_status in (
                'DISCOVERED',
                'SELECTED',
                'CRAWLED',
                'MATCHED',
                'REJECTED',
                'FAILED'
            )
        ),

    constraint candidate_reference_sources_confidence_check
        check (
            discovery_confidence is null
            or (
                discovery_confidence >= 0
                and discovery_confidence <= 1
            )
        ),

    constraint candidate_reference_sources_rank_check
        check (
            discovery_rank is null
            or discovery_rank > 0
        )
);


create index if not exists
ix_candidate_reference_sources_candidate_id
on public.candidate_reference_sources(candidate_id);


create index if not exists
ix_candidate_reference_sources_source_url_id
on public.candidate_reference_sources(source_url_id);


create index if not exists
ix_candidate_reference_sources_status
on public.candidate_reference_sources(
    discovery_status,
    is_selected_for_crawl
);