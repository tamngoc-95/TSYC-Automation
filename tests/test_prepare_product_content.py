"""Automated tests for Vietnamese product content protection.

Covers scripts/prepare_product_content.py: APPROVED content must never be
silently selected for overwrite, and drafting/generation must never replace
reviewed wording (CLAUDE.md golden principle #6, mandatory gate "Before
content approval").

Pure/offline: no live Supabase, no network. Repository access is an
in-memory FakeSupabaseRepository.
"""

from __future__ import annotations

from typing import Any

import pytest

import prepare_product_content as ppc

from support.fake_supabase import FakeSupabaseRepository


PRODUCT_ID = "internal-product-1"


def make_product(**overrides) -> dict[str, Any]:
    product = {
        "internal_product_id": PRODUCT_ID,
        "candidate_id": "candidate-1",
        "primary_reference_id": "reference-1",
        "product_code": "TSYC-FB-2026-001-CAN-0001",
        "product_type": "BOOK",
        "title": "Verified Book Title",
        "author": "Author Name",
        "isbn": None,  # deliberately missing -- must not block preparation
        "publisher": "NXB Kim Đồng",
        "language_code": "vi",
        "page_count": 120,
        "weight_grams": 250,
        "length_cm": 20,
        "width_cm": 14,
        "height_cm": 1,
        "cover_price_vnd": 85000,
        "metadata_status": "READY",
        "image_status": "PENDING",
        "content_status": "PENDING",
        "woocommerce_status": "NOT_CREATED",
        "product_metadata": {},
        "is_active": True,
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
        "short_description": "Mô tả ngắn đã được người quản lý duyệt thủ công.",
        "long_description": "Mô tả dài đã được người quản lý duyệt thủ công, "
        "gồm nội dung chủ đề sách thật.",
        "author_summary": "Giới thiệu tác giả đã duyệt.",
        "product_details": "Tác giả: Author Name",
        "seo_title": "Verified Book Title",
        "seo_description": "SEO đã duyệt.",
        "content_status": "APPROVED",
        "review_required": False,
        "generation_method": "HUMAN_ENRICHED",
        "generator_name": ppc.GENERATOR_NAME,
        "generator_version": ppc.GENERATOR_VERSION,
    }
    content.update(overrides)
    return content


# --------------------------------------------------------------------------
# 1. APPROVED product_content cannot be silently selected for overwrite
# --------------------------------------------------------------------------


def test_select_product_for_review_skips_approved_content():
    product = make_product()
    approved = make_approved_content()

    repository = FakeSupabaseRepository(
        tables={
            "internal_products": [product],
            "product_contents": [approved],
        }
    )

    result = ppc.select_product_for_review(
        repository=repository,
        product_code=None,
    )

    assert result is None


def test_select_product_for_review_skips_rejected_content_too():
    product = make_product()
    rejected = make_approved_content(content_status="REJECTED")

    repository = FakeSupabaseRepository(
        tables={
            "internal_products": [product],
            "product_contents": [rejected],
        }
    )

    result = ppc.select_product_for_review(
        repository=repository,
        product_code=None,
    )

    assert result is None


def test_select_product_for_review_returns_drafted_content_for_review():
    product = make_product()
    drafted = make_approved_content(
        content_status="DRAFTED",
        review_required=True,
    )

    repository = FakeSupabaseRepository(
        tables={
            "internal_products": [product],
            "product_contents": [drafted],
        }
    )

    result = ppc.select_product_for_review(
        repository=repository,
        product_code=None,
    )

    assert result is not None
    returned_product, returned_existing = result
    assert returned_product["internal_product_id"] == PRODUCT_ID
    assert returned_existing["content_status"] == "DRAFTED"


# --------------------------------------------------------------------------
# 2 & 3. drafting action cannot replace approved short/long description
# --------------------------------------------------------------------------


def test_merge_never_replaces_existing_short_description():
    generated = ppc.build_safe_draft(make_product())
    existing = make_approved_content()

    merged = ppc.merge_with_existing(generated, existing)

    assert merged["short_description"] == existing["short_description"]
    assert merged["short_description"] != generated["short_description"]


def test_merge_never_replaces_existing_long_description():
    generated = ppc.build_safe_draft(make_product())
    existing = make_approved_content()

    merged = ppc.merge_with_existing(generated, existing)

    assert merged["long_description"] == existing["long_description"]
    assert merged["long_description"] != generated["long_description"]


def test_merge_preserves_all_reviewed_fields_not_only_descriptions():
    generated = ppc.build_safe_draft(make_product())
    existing = make_approved_content()

    merged = ppc.merge_with_existing(generated, existing)

    for field in (
        "product_name",
        "short_description",
        "long_description",
        "author_summary",
        "product_details",
        "seo_title",
        "seo_description",
    ):
        assert merged[field] == existing[field], field


def test_merge_only_fills_missing_fields_from_generated():
    generated = ppc.build_safe_draft(make_product())
    existing = make_approved_content(seo_description=None)  # left blank by reviewer

    merged = ppc.merge_with_existing(generated, existing)

    assert merged["seo_description"] == generated["seo_description"]
    assert merged["short_description"] == existing["short_description"]


# --------------------------------------------------------------------------
# 4. approval bookkeeping may change status/audit fields, not content text
# --------------------------------------------------------------------------


def test_save_content_approve_changes_only_bookkeeping_fields():
    product = make_product()
    existing = make_approved_content(
        content_status="DRAFTED",
        review_required=True,
        approved_at=None,
    )

    repository = FakeSupabaseRepository(
        tables={
            "internal_products": [product],
            "product_contents": [existing],
        }
    )

    content_text_fields = {
        "product_name": existing["product_name"],
        "short_description": existing["short_description"],
        "long_description": existing["long_description"],
        "author_summary": existing["author_summary"],
        "product_details": existing["product_details"],
        "seo_title": existing["seo_title"],
        "seo_description": existing["seo_description"],
    }

    result = ppc.save_content(
        repository=repository,
        product=product,
        existing=existing,
        content=content_text_fields,
        approve=True,
    )

    # Bookkeeping fields changed.
    assert result["content_status"] == "APPROVED"
    assert result["review_required"] is False
    assert result["approved_at"] is not None

    # Reviewed content text is untouched.
    for field, value in content_text_fields.items():
        assert result[field] == value

    stored_product = repository.client.tables["internal_products"][0]
    assert stored_product["content_status"] == "APPROVED"


def test_validate_approval_content_refuses_untouched_generic_draft():
    product = make_product()
    generated = ppc.build_safe_draft(product)

    with pytest.raises(RuntimeError, match="generic metadata-only safe draft"):
        ppc.validate_approval_content(
            existing={"content_status": "DRAFTED", **generated},
            content=generated,
            generated=generated,
        )


def test_validate_approval_content_refuses_first_generation_with_no_existing():
    generated = ppc.build_safe_draft(make_product())

    with pytest.raises(RuntimeError, match="cannot be approved on its first"):
        ppc.validate_approval_content(
            existing=None,
            content=generated,
            generated=generated,
        )


def test_validate_approval_content_allows_enriched_content():
    product = make_product()
    generated = ppc.build_safe_draft(product)
    enriched = dict(generated)
    enriched["long_description"] = (
        "Nội dung sách kể về một hành trình khám phá thế giới xung quanh, "
        "được biên tập lại từ bài đăng Facebook đã được phép sử dụng."
    )

    # Must not raise.
    ppc.validate_approval_content(
        existing={"content_status": "DRAFTED", **enriched},
        content=enriched,
        generated=generated,
    )


# --------------------------------------------------------------------------
# 5. DRAFTED content remains editable only through supported review/update
# --------------------------------------------------------------------------


def test_save_content_draft_sets_drafted_status_and_review_required():
    product = make_product()
    generated = ppc.build_safe_draft(product)

    repository = FakeSupabaseRepository(
        tables={"internal_products": [product]},
    )

    result = ppc.save_content(
        repository=repository,
        product=product,
        existing=None,
        content=generated,
        approve=False,
    )

    assert result["content_status"] == "DRAFTED"
    assert result["review_required"] is True
    assert result["approved_at"] is None


def test_save_content_draft_updates_in_place_when_existing_row_present():
    product = make_product()
    existing = make_approved_content(
        content_status="DRAFTED",
        review_required=True,
        short_description="Bản nháp cũ cần chỉnh sửa thêm.",
    )

    repository = FakeSupabaseRepository(
        tables={
            "internal_products": [product],
            "product_contents": [existing],
        }
    )

    revised_content = dict(existing)
    revised_content["short_description"] = "Bản nháp đã được chỉnh sửa bởi người quản lý."

    result = ppc.save_content(
        repository=repository,
        product=product,
        existing=existing,
        content=revised_content,
        approve=False,
    )

    assert result["content_status"] == "DRAFTED"
    assert result["short_description"] == "Bản nháp đã được chỉnh sửa bởi người quản lý."

    stored_rows = repository.client.tables["product_contents"]
    assert len(stored_rows) == 1  # updated in place, not duplicated
    assert stored_rows[0]["short_description"] == result["short_description"]


# --------------------------------------------------------------------------
# 6. missing ISBN does not block content preparation
# --------------------------------------------------------------------------


def test_build_safe_draft_does_not_require_isbn():
    product = make_product(isbn=None)

    # Must not raise.
    content = ppc.build_safe_draft(product)

    assert content["product_name"] == "Verified Book Title"
    assert "ISBN" not in content["product_details"]


def test_select_product_for_review_does_not_skip_products_missing_isbn():
    product = make_product(isbn=None)

    repository = FakeSupabaseRepository(
        tables={"internal_products": [product]},
    )

    result = ppc.select_product_for_review(
        repository=repository,
        product_code=None,
    )

    assert result is not None
    returned_product, existing = result
    assert returned_product["isbn"] is None
    assert existing is None
