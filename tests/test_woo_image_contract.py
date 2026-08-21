"""Regression tests for the WordPress/WooCommerce image identifier contract.

This protects the exact production bug fixed in commit aaeee14 ("Fix Woo
image revalidation key mismatch"): the pre-create revalidation compared
uploaded_media items using the wrong dict key (`image_id`, which uploaded
media records never have) instead of `source_image_id`.

Identifier chain under test:

    local product_images.id
        -> source_image_id (recorded on the WordPress media record)
        -> WordPress media upload/reuse
        -> wordpress_media_id
        -> Woo payload image id

Pure/offline: no live Supabase, no live WordPress/WooCommerce, no network
calls. Every network-touching helper is monkeypatched.
"""

from __future__ import annotations

from typing import Any

import pytest

import create_woocommerce_draft as cwd

from support.fake_supabase import FakeSupabaseRepository


LOCAL_IMAGE_ID = "local-product-image-uuid-1"
WORDPRESS_MEDIA_ID = 555


def make_image(**overrides) -> dict[str, Any]:
    image = {
        "image_id": LOCAL_IMAGE_ID,
        "candidate_id": "candidate-1",
        "storage_bucket": "product-images",
        "storage_path": "candidate-1/cover.jpg",
        "mime_type": "image/jpeg",
        "image_role": "FRONT_COVER",
        "is_selected_main_image": True,
    }
    image.update(overrides)
    return image


def make_sync(**overrides) -> dict[str, Any]:
    sync = {
        "sync_id": "sync-1",
        "response_payload": {"uploaded_media": []},
    }
    sync.update(overrides)
    return sync


# --------------------------------------------------------------------------
# 1. uploaded media record preserves source_image_id
# --------------------------------------------------------------------------


def test_uploaded_media_record_preserves_source_image_id(monkeypatch):
    monkeypatch.setattr(
        cwd,
        "download_source_image_from_supabase",
        lambda **kwargs: (b"fake-image-bytes", "image/jpeg"),
    )
    monkeypatch.setattr(
        cwd,
        "upload_wordpress_media",
        lambda **kwargs: {
            "id": WORDPRESS_MEDIA_ID,
            "source_url": "https://shop.example.com/wp-content/uploads/cover.jpg",
        },
    )
    monkeypatch.setattr(
        cwd,
        "update_wordpress_media_metadata",
        lambda **kwargs: {},
    )

    sync = make_sync()
    repository = FakeSupabaseRepository(
        tables={"woocommerce_product_syncs": [sync]},
    )

    result = cwd.upload_or_reuse_product_images(
        repository=repository,
        sync=sync,
        product={"product_code": "TSYC-FB-2026-001-CAN-0001"},
        product_name="Verified Book Title",
        images=[make_image()],
        store_url="https://shop.example.com",
        username="wp-user",
        application_password="app-pass",
        timeout_seconds=5,
    )

    assert len(result) == 1
    media_record = result[0]

    # The identifier chain must never be confused: source_image_id is the
    # LOCAL product_images.id, wordpress_media_id is the REMOTE WP id.
    assert media_record["source_image_id"] == LOCAL_IMAGE_ID
    assert media_record["wordpress_media_id"] == WORDPRESS_MEDIA_ID
    assert media_record["source_image_id"] != media_record["wordpress_media_id"]


# --------------------------------------------------------------------------
# 2 & 3. stale-image-set revalidation compares source_image_id; unchanged set passes
# --------------------------------------------------------------------------


def test_unchanged_image_set_passes_revalidation():
    current_images = [{"image_id": LOCAL_IMAGE_ID}]
    uploaded_media = [{"source_image_id": LOCAL_IMAGE_ID, "wordpress_media_id": WORDPRESS_MEDIA_ID}]

    # Must not raise.
    cwd.assert_uploaded_image_set_matches_current(
        current_images=current_images,
        uploaded_media=uploaded_media,
    )


def test_revalidation_keys_by_source_image_id_not_by_wordpress_media_id():
    """
    Regression for commit aaeee14: even if a WordPress media id happened to
    collide (as a string) with a local image id, the comparison must use
    source_image_id, not any WordPress-side field.
    """
    current_images = [{"image_id": LOCAL_IMAGE_ID}]

    # This uploaded_media item has no usable source_image_id (None), which
    # is exactly what the pre-fix code produced when it mistakenly read
    # item.get("image_id") -- a key uploaded_media records never carry.
    uploaded_media = [{"wordpress_media_id": LOCAL_IMAGE_ID}]

    with pytest.raises(RuntimeError, match="stale image set"):
        cwd.assert_uploaded_image_set_matches_current(
            current_images=current_images,
            uploaded_media=uploaded_media,
        )


# --------------------------------------------------------------------------
# 4. changed image set fails safely before Woo product creation
# --------------------------------------------------------------------------


def test_changed_image_set_fails_before_create_call(monkeypatch):
    calls = {"count": 0}

    def fake_create_woocommerce_product(*args, **kwargs):
        calls["count"] += 1
        return {"id": 1, "status": "draft"}

    monkeypatch.setattr(cwd, "create_woocommerce_product", fake_create_woocommerce_product)

    current_images = [{"image_id": "image-A"}, {"image_id": "image-B"}]
    uploaded_media = [{"source_image_id": "image-A"}]  # image-B is missing

    with pytest.raises(RuntimeError, match="stale image set"):
        cwd.assert_uploaded_image_set_matches_current(
            current_images=current_images,
            uploaded_media=uploaded_media,
        )
        # This mirrors main()'s sequential call order: unreachable if the
        # revalidation above correctly raised first.
        cwd.create_woocommerce_product(
            store_url="https://shop.example.com",
            api_version="wc/v3",
            consumer_key="ck_test",
            consumer_secret="cs_test",
            timeout_seconds=5,
            payload={"status": "draft"},
        )

    assert calls["count"] == 0


def test_extra_uploaded_media_beyond_current_set_also_fails():
    """A stale set can also mean uploaded_media has a leftover image the
    current publishable set no longer contains -- must fail either direction."""
    current_images = [{"image_id": "image-A"}]
    uploaded_media = [
        {"source_image_id": "image-A"},
        {"source_image_id": "image-B"},
    ]

    with pytest.raises(RuntimeError, match="stale image set"):
        cwd.assert_uploaded_image_set_matches_current(
            current_images=current_images,
            uploaded_media=uploaded_media,
        )


# --------------------------------------------------------------------------
# 5. reused WordPress media follows the same identifier contract
# --------------------------------------------------------------------------


def test_reused_wordpress_media_is_not_re_uploaded(monkeypatch):
    def must_not_be_called(**kwargs):
        raise AssertionError(
            "download/upload must not run when existing media is reusable"
        )

    monkeypatch.setattr(cwd, "download_source_image_from_supabase", must_not_be_called)
    monkeypatch.setattr(cwd, "upload_wordpress_media", must_not_be_called)
    monkeypatch.setattr(cwd, "update_wordpress_media_metadata", must_not_be_called)
    monkeypatch.setattr(cwd, "verify_wordpress_media", lambda **kwargs: True)

    previously_uploaded = {
        "source_image_id": LOCAL_IMAGE_ID,
        "wordpress_media_id": WORDPRESS_MEDIA_ID,
        "wordpress_source_url": "https://shop.example.com/wp-content/uploads/cover.jpg",
        "is_selected_main_image": True,
    }

    sync = make_sync(
        response_payload={"uploaded_media": [previously_uploaded]},
    )
    repository = FakeSupabaseRepository(
        tables={"woocommerce_product_syncs": [sync]},
    )

    result = cwd.upload_or_reuse_product_images(
        repository=repository,
        sync=sync,
        product={"product_code": "TSYC-FB-2026-001-CAN-0001"},
        product_name="Verified Book Title",
        images=[make_image()],
        store_url="https://shop.example.com",
        username="wp-user",
        application_password="app-pass",
        timeout_seconds=5,
    )

    assert len(result) == 1
    assert result[0]["source_image_id"] == LOCAL_IMAGE_ID
    assert result[0]["wordpress_media_id"] == WORDPRESS_MEDIA_ID


def test_reuse_falls_back_to_re_upload_when_wordpress_media_was_deleted(monkeypatch):
    """
    If the saved WordPress attachment no longer exists remotely, reuse must
    not be trusted blindly -- a fresh upload must happen instead.
    """
    monkeypatch.setattr(cwd, "verify_wordpress_media", lambda **kwargs: False)
    monkeypatch.setattr(
        cwd,
        "download_source_image_from_supabase",
        lambda **kwargs: (b"fake-image-bytes", "image/jpeg"),
    )
    monkeypatch.setattr(
        cwd,
        "upload_wordpress_media",
        lambda **kwargs: {"id": 777, "source_url": "https://shop.example.com/new.jpg"},
    )
    monkeypatch.setattr(cwd, "update_wordpress_media_metadata", lambda **kwargs: {})

    previously_uploaded = {
        "source_image_id": LOCAL_IMAGE_ID,
        "wordpress_media_id": WORDPRESS_MEDIA_ID,
    }

    sync = make_sync(
        response_payload={"uploaded_media": [previously_uploaded]},
    )
    repository = FakeSupabaseRepository(
        tables={"woocommerce_product_syncs": [sync]},
    )

    result = cwd.upload_or_reuse_product_images(
        repository=repository,
        sync=sync,
        product={"product_code": "TSYC-FB-2026-001-CAN-0001"},
        product_name="Verified Book Title",
        images=[make_image()],
        store_url="https://shop.example.com",
        username="wp-user",
        application_password="app-pass",
        timeout_seconds=5,
    )

    assert result[0]["wordpress_media_id"] == 777
    assert result[0]["source_image_id"] == LOCAL_IMAGE_ID


# --------------------------------------------------------------------------
# 6. source_image_id and wordpress_media_id cannot be confused
# --------------------------------------------------------------------------


def test_find_saved_media_matches_only_on_source_image_id():
    """
    find_saved_media_for_image must key strictly on source_image_id, even
    when a wordpress_media_id happens to look like a local image id.
    """
    uploaded_media = [
        {
            "source_image_id": "different-local-image",
            "wordpress_media_id": LOCAL_IMAGE_ID,  # coincidental string match
        },
    ]

    result = cwd.find_saved_media_for_image(
        uploaded_media=uploaded_media,
        image_id=LOCAL_IMAGE_ID,
    )

    # Must NOT match on wordpress_media_id, even though it equals image_id.
    assert result is None


def test_find_saved_media_matches_correct_source_image_id():
    target = {"source_image_id": LOCAL_IMAGE_ID, "wordpress_media_id": WORDPRESS_MEDIA_ID}
    other = {"source_image_id": "some-other-image", "wordpress_media_id": 1}

    result = cwd.find_saved_media_for_image(
        uploaded_media=[other, target],
        image_id=LOCAL_IMAGE_ID,
    )

    assert result is target


def test_woo_payload_images_use_wordpress_media_id_not_source_image_id():
    """
    The final Woo payload's image list must reference WordPress media ids
    (what WooCommerce understands), never the local source_image_id.
    """
    uploaded_media = [
        {
            "source_image_id": LOCAL_IMAGE_ID,
            "wordpress_media_id": WORDPRESS_MEDIA_ID,
        },
    ]

    product = {
        "internal_product_id": "internal-product-1",
        "candidate_id": "candidate-1",
        "product_code": "TSYC-FB-2026-001-CAN-0001",
    }
    content = {
        "product_name": "Verified Book Title",
        "product_content_id": "content-1",
        "short_description": "",
        "long_description": "",
        "author_summary": "",
        "product_details": "",
    }

    payload = cwd.build_woocommerce_payload(
        product=product,
        content=content,
        uploaded_media=uploaded_media,
    )

    assert payload["images"] == [{"id": WORDPRESS_MEDIA_ID}]
    assert LOCAL_IMAGE_ID not in [img["id"] for img in payload["images"]]
