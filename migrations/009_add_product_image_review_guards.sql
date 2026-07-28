-- Protect product image review and publishing decisions.

create unique index if not exists
ux_product_images_one_selected_main_per_candidate
on public.product_images(candidate_id)
where candidate_id is not null
  and is_selected_main_image = true;


alter table public.product_images
drop constraint if exists
product_images_publish_eligibility_check;


alter table public.product_images
add constraint product_images_publish_eligibility_check
check (
    is_publish_eligible = false
    or (
        image_status = 'VALIDATED'
        and usage_rights_status in (
            'STORE_OWNED',
            'PUBLISHER_APPROVED',
            'SUPPLIER_APPROVED'
        )
    )
);


alter table public.product_images
drop constraint if exists
product_images_selected_main_check;


alter table public.product_images
add constraint product_images_selected_main_check
check (
    is_selected_main_image = false
    or (
        is_main_image_candidate = true
        and is_publish_eligible = true
        and image_status = 'VALIDATED'
    )
);


create index if not exists
idx_product_images_review_queue
on public.product_images (
    image_status,
    usage_rights_status,
    created_at
)
where image_status in (
    'PENDING',
    'DOWNLOADED'
);


create index if not exists
idx_product_images_publish_eligible
on public.product_images (
    candidate_id,
    is_selected_main_image
)
where is_publish_eligible = true;