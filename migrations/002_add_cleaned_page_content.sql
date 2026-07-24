alter table public.raw_pages
add column if not exists cleaned_text text,
add column if not exists cleaning_status text
    default 'PENDING'
    check (
        cleaning_status in (
            'PENDING',
            'CLEANED',
            'REVIEW_REQUIRED',
            'FAILED'
        )
    ),
add column if not exists cleaning_method text,
add column if not exists cleaned_at timestamptz;