"""Automated tests for WooCommerce draft creation safety.

Covers scripts/create_woocommerce_draft.py: the draft-only guarantee, the
no-price guarantee, the exactly-one-main-image gate, the pre-create
revalidation gates, the local/remote duplicate-retry guards, and the
single-create-call guarantee (CLAUDE.md golden principles #1 and mandatory
gates "Before WooCommerce draft").

Pure/offline: no live WooCommerce, no live Supabase, no network calls.
`requests` is monkeypatched wherever the production code would otherwise
make an HTTP call.
"""

from __future__ import annotations

import ast
import inspect
from typing import Any

import pytest

import create_woocommerce_draft as cwd

from support.fake_supabase import FakeSupabaseRepository


PRODUCT_ID = "internal-product-1"
CANDIDATE_ID = "candidate-1"


def make_ready_product(**overrides) -> dict[str, Any]:
    product = {
        "internal_product_id": PRODUCT_ID,
        "candidate_id": CANDIDATE_ID,
        "product_code": "TSYC-FB-2026-001-CAN-0001",
        "product_type": "BOOK",
        "title": "Verified Book Title",
        "author": "Author Name",
        "isbn": "9786041234567",
        "publisher": "NXB Kim Đồng",
        "page_count": 120,
        "weight_grams": 250,
        "length_cm": 20,
        "width_cm": 14,
        "height_cm": 1,
        "is_active": True,
        "content_status": "APPROVED",
        "image_status": "APPROVED",
        "pricing_status": "PENDING",
        "woocommerce_status": "READY_FOR_DRAFT",
        "review_required": False,
        "product_metadata": {},
        "created_at": "2026-08-01T00:00:00+00:00",
    }
    product.update(overrides)
    return product


def make_approved_content(**overrides) -> dict[str, Any]:
    content = {
        "product_content_id": "content-1",
        "internal_product_id": PRODUCT_ID,
        "content_language": "vi",
        "product_name": "Verified Book Title",
        "short_description": "Mô tả ngắn đã duyệt.",
        "long_description": "Mô tả dài đã duyệt.",
        "author_summary": "Giới thiệu tác giả.",
        "product_details": "Tác giả: Author Name",
        "seo_title": "Verified Book Title",
        "seo_description": "SEO description.",
        "content_status": "APPROVED",
        "review_required": False,
    }
    content.update(overrides)
    return content


def make_image(**overrides) -> dict[str, Any]:
    image = {
        "image_id": "image-1",
        "candidate_id": CANDIDATE_ID,
        "storage_bucket": "product-images",
        "storage_path": "candidate-1/image-1.jpg",
        "original_file_name": "cover.jpg",
        "mime_type": "image/jpeg",
        "width_pixels": 1200,
        "height_pixels": 1600,
        "file_size_bytes": 200_000,
        "image_hash": "abc123",
        "image_role": "FRONT_COVER",
        "usage_rights_status": "STORE_OWNED",
        "is_selected_main_image": True,
        "is_publish_eligible": True,
        "image_status": "VALIDATED",
    }
    image.update(overrides)
    return image


# --------------------------------------------------------------------------
# 1 & 2. Woo payload always draft-status, never contains a price field
# --------------------------------------------------------------------------


def test_payload_status_is_always_draft():
    product = make_ready_product()
    content = make_approved_content()
    uploaded_media = [{"wordpress_media_id": 555}]

    payload = cwd.build_woocommerce_payload(
        product=product,
        content=content,
        uploaded_media=uploaded_media,
    )

    assert payload["status"] == "draft"


def test_payload_never_contains_price_fields():
    product = make_ready_product()
    content = make_approved_content()

    payload = cwd.build_woocommerce_payload(
        product=product,
        content=content,
        uploaded_media=[],
    )

    for forbidden_field in ("regular_price", "sale_price", "price"):
        assert forbidden_field not in payload


def test_payload_status_cannot_be_overridden_by_caller_input():
    """
    build_woocommerce_payload's status field is a literal, not derived from
    product/content -- caller-supplied dicts (even malicious ones) must
    never be able to change it.
    """
    product = make_ready_product(status="publish", woocommerce_status="publish")
    content = make_approved_content(status="publish")

    payload = cwd.build_woocommerce_payload(
        product=product,
        content=content,
        uploaded_media=[],
    )

    assert payload["status"] == "draft"


def test_payload_ignores_extraneous_price_keys_on_input_dicts():
    """Extra caller-supplied 'price'-shaped keys must never leak into the payload."""
    product = make_ready_product(regular_price="199000", price="199000")
    content = make_approved_content(sale_price="149000")

    payload = cwd.build_woocommerce_payload(
        product=product,
        content=content,
        uploaded_media=[],
    )

    for forbidden_field in ("regular_price", "sale_price", "price"):
        assert forbidden_field not in payload


# --------------------------------------------------------------------------
# 3. draft creation is refused under each unsafe precondition
# --------------------------------------------------------------------------


def test_get_ready_product_refuses_when_product_code_not_ready_for_draft():
    repository = FakeSupabaseRepository(
        tables={
            "internal_products": [
                make_ready_product(woocommerce_status="NOT_CREATED"),
            ],
        }
    )

    with pytest.raises(RuntimeError, match="No READY_FOR_DRAFT"):
        cwd.get_ready_product(
            repository=repository,
            product_code="TSYC-FB-2026-001-CAN-0001",
        )


def test_get_ready_product_excludes_review_required_products():
    repository = FakeSupabaseRepository(
        tables={
            "internal_products": [make_ready_product(review_required=True)],
        }
    )

    result = cwd.get_ready_product(repository=repository, product_code=None)

    assert result is None


def test_revalidate_refuses_when_content_status_not_approved():
    repository = FakeSupabaseRepository(
        tables={
            "internal_products": [
                make_ready_product(content_status="DRAFTED"),
            ],
        }
    )

    with pytest.raises(RuntimeError, match="content is no longer APPROVED"):
        cwd.revalidate_pre_create_state(
            repository=repository,
            product_code="TSYC-FB-2026-001-CAN-0001",
        )


def test_revalidate_refuses_when_image_status_not_approved():
    repository = FakeSupabaseRepository(
        tables={
            "internal_products": [
                make_ready_product(image_status="PENDING"),
            ],
        }
    )

    with pytest.raises(RuntimeError, match="image status is no longer APPROVED"):
        cwd.revalidate_pre_create_state(
            repository=repository,
            product_code="TSYC-FB-2026-001-CAN-0001",
        )


def test_revalidate_refuses_when_zero_main_images():
    repository = FakeSupabaseRepository(
        tables={
            "internal_products": [make_ready_product()],
            "product_contents": [make_approved_content(internal_product_id=PRODUCT_ID)],
            "product_images": [],
        }
    )

    with pytest.raises(RuntimeError, match="Exactly one validated"):
        cwd.revalidate_pre_create_state(
            repository=repository,
            product_code="TSYC-FB-2026-001-CAN-0001",
        )


def test_revalidate_refuses_when_more_than_one_selected_main_image():
    repository = FakeSupabaseRepository(
        tables={
            "internal_products": [make_ready_product()],
            "product_contents": [make_approved_content(internal_product_id=PRODUCT_ID)],
            "product_images": [
                make_image(image_id="image-1"),
                make_image(image_id="image-2"),
            ],
        }
    )

    with pytest.raises(RuntimeError, match="Exactly one validated"):
        cwd.revalidate_pre_create_state(
            repository=repository,
            product_code="TSYC-FB-2026-001-CAN-0001",
        )


def test_revalidate_refuses_when_selected_main_image_not_publish_eligible():
    repository = FakeSupabaseRepository(
        tables={
            "internal_products": [make_ready_product()],
            "product_contents": [make_approved_content(internal_product_id=PRODUCT_ID)],
            "product_images": [
                make_image(is_publish_eligible=False),
            ],
        }
    )

    with pytest.raises(RuntimeError, match="Exactly one validated"):
        cwd.revalidate_pre_create_state(
            repository=repository,
            product_code="TSYC-FB-2026-001-CAN-0001",
        )


@pytest.mark.parametrize(
    "rights_status",
    ["RIGHTS_UNKNOWN", "REFERENCE_ONLY", None, "PENDING_REVIEW"],
)
def test_get_publishable_images_excludes_non_publishable_rights_status(rights_status):
    repository = FakeSupabaseRepository(
        tables={
            "product_images": [make_image(usage_rights_status=rights_status)],
        }
    )

    images = cwd.get_publishable_images(
        repository=repository,
        candidate_id=CANDIDATE_ID,
    )

    assert images == []


def test_get_publishable_images_excludes_non_validated_status():
    repository = FakeSupabaseRepository(
        tables={
            "product_images": [make_image(image_status="PENDING")],
        }
    )

    images = cwd.get_publishable_images(
        repository=repository,
        candidate_id=CANDIDATE_ID,
    )

    assert images == []


def test_revalidate_succeeds_when_all_gates_satisfied():
    repository = FakeSupabaseRepository(
        tables={
            "internal_products": [make_ready_product()],
            "product_contents": [make_approved_content(internal_product_id=PRODUCT_ID)],
            "product_images": [make_image()],
        }
    )

    product, content, images = cwd.revalidate_pre_create_state(
        repository=repository,
        product_code="TSYC-FB-2026-001-CAN-0001",
    )

    assert product["internal_product_id"] == PRODUCT_ID
    assert content["content_status"] == "APPROVED"
    assert len(images) == 1


def test_find_product_by_sku_detects_duplicate_remote_sku(monkeypatch):
    calls = []

    def fake_get(url, auth, params, headers, timeout):
        calls.append(params)

        class FakeResponse:
            status_code = 200

            def json(self):
                return [{"id": 999, "sku": params["sku"], "status": "draft"}]

        return FakeResponse()

    monkeypatch.setattr(cwd.requests, "get", fake_get)

    result = cwd.find_product_by_sku(
        store_url="https://example.com",
        api_version="wc/v3",
        consumer_key="ck_test",
        consumer_secret="cs_test",
        timeout_seconds=5,
        sku="TSYC-FB-2026-001-CAN-0001",
    )

    assert result is not None
    assert result["id"] == 999
    assert len(calls) == 1


def test_find_product_by_sku_returns_none_when_no_duplicate(monkeypatch):
    def fake_get(url, auth, params, headers, timeout):
        class FakeResponse:
            status_code = 200

            def json(self):
                return []

        return FakeResponse()

    monkeypatch.setattr(cwd.requests, "get", fake_get)

    result = cwd.find_product_by_sku(
        store_url="https://example.com",
        api_version="wc/v3",
        consumer_key="ck_test",
        consumer_secret="cs_test",
        timeout_seconds=5,
        sku="TSYC-FB-2026-001-CAN-0001",
    )

    assert result is None


@pytest.mark.parametrize(
    "existing_sync",
    [
        {"woocommerce_product_id": 123},
        {
            "woocommerce_product_id": 123,
            "response_payload": {"recovery_required": True},
        },
    ],
)
def test_has_existing_remote_draft_refuses_retry(existing_sync):
    assert cwd.has_existing_remote_draft(existing_sync) is True


@pytest.mark.parametrize(
    "existing_sync",
    [
        None,
        {},
        {"woocommerce_product_id": None},
        {"woocommerce_status": "FAILED"},
    ],
)
def test_has_existing_remote_draft_allows_creation(existing_sync):
    assert cwd.has_existing_remote_draft(existing_sync) is False


# --------------------------------------------------------------------------
# 4 & 5. exactly-one-create-call, no automatic retry on an uncertain result
# --------------------------------------------------------------------------


def test_create_woocommerce_product_makes_exactly_one_call_on_success(monkeypatch):
    calls = []

    def fake_post(url, auth, json, headers, timeout):
        calls.append(json)

        class FakeResponse:
            status_code = 201

            def json(self):
                return {"id": 42, "status": "draft", "permalink": "https://x/y"}

        return FakeResponse()

    monkeypatch.setattr(cwd.requests, "post", fake_post)

    result = cwd.create_woocommerce_product(
        store_url="https://example.com",
        api_version="wc/v3",
        consumer_key="ck_test",
        consumer_secret="cs_test",
        timeout_seconds=5,
        payload={"status": "draft"},
    )

    assert result["id"] == 42
    assert len(calls) == 1


def test_create_woocommerce_product_does_not_retry_on_timeout(monkeypatch):
    """
    An uncertain remote result (timeout: the product may or may not have
    been created) must propagate as an exception, with no internal retry.
    """
    import requests as real_requests

    calls = {"count": 0}

    def fake_post(url, auth, json, headers, timeout):
        calls["count"] += 1
        raise real_requests.Timeout("simulated timeout")

    monkeypatch.setattr(cwd.requests, "post", fake_post)

    with pytest.raises(real_requests.Timeout):
        cwd.create_woocommerce_product(
            store_url="https://example.com",
            api_version="wc/v3",
            consumer_key="ck_test",
            consumer_secret="cs_test",
            timeout_seconds=5,
            payload={"status": "draft"},
        )

    assert calls["count"] == 1


def test_create_woocommerce_product_raises_on_unexpected_status(monkeypatch):
    """A created-but-not-draft product must be rejected, not silently accepted."""

    def fake_post(url, auth, json, headers, timeout):
        class FakeResponse:
            status_code = 201

            def json(self):
                return {"id": 42, "status": "publish"}

        return FakeResponse()

    monkeypatch.setattr(cwd.requests, "post", fake_post)

    with pytest.raises(cwd.WooCommerceCreateError, match="unexpected status"):
        cwd.create_woocommerce_product(
            store_url="https://example.com",
            api_version="wc/v3",
            consumer_key="ck_test",
            consumer_secret="cs_test",
            timeout_seconds=5,
            payload={"status": "draft"},
        )


def test_main_source_calls_create_woocommerce_product_exactly_once():
    """
    Regression guard: main() must contain exactly one call-site for
    create_woocommerce_product. A future retry loop that calls it twice
    would defeat the no-blind-retry guarantee and must fail this test.
    """
    source = inspect.getsource(cwd.main)
    tree = ast.parse(source)

    call_count = sum(
        1
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "create_woocommerce_product"
    )

    assert call_count == 1
