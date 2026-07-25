-- Allow source images to be stored before product candidates are created.
alter table public.product_images
alter column candidate_id drop not null;


-- Link an image to the collected Facebook source records.
alter table public.product_images
add column if not exists raw_page_id uuid
    references public.raw_pages(raw_page_id)
    on delete set null;

alter table public.product_images
add column if not exists source_url_id uuid
    references public.source_urls(source_url_id)
    on delete set null;


-- Preserve the stable Facebook photo page separately
-- from the temporary direct CDN image URL.
alter table public.product_images
add column if not exists source_photo_url text;


-- Record the collector implementation that produced the image.
alter table public.product_images
add column if not exists collector_name text;

alter table public.product_images
add column if not exists collector_version text;


-- Record when the local image was uploaded to storage.
alter table public.product_images
add column if not exists uploaded_at timestamptz;


-- Prevent the same physical image from being inserted twice
-- for the same collected Facebook post.
create unique index if not exists
ux_product_images_raw_page_hash
on public.product_images(raw_page_id, image_hash)
where raw_page_id is not null
  and image_hash is not null;


-- Support fast lookups by collected post.
create index if not exists
ix_product_images_raw_page_id
on public.product_images(raw_page_id);


-- Support fast lookups by authorized source URL.
create index if not exists
ix_product_images_source_url_id
on public.product_images(source_url_id);


-- Support duplicate checks across the image library.
create index if not exists
ix_product_images_image_hash
on public.product_images(image_hash)
where image_hash is not null;