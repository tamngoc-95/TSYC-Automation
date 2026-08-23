"""Automated tests for the --action REVISE reviewer-edit workflow.

Covers scripts/prepare_product_content.py's REVISE action: the permanent,
supported way to apply human-reviewed wording to an existing DRAFTED
product_contents row without creating a duplicate row, without touching
APPROVED/REJECTED content, and without weakening the APPROVE gate.

Pure/offline: no live Supabase, no network. Repository access is an
in-memory FakeSupabaseRepository. Content files are written to pytest's
tmp_path, never to the repository's own data/ or scratch directories.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import prepare_product_content as ppc

from support.fake_supabase import FakeSupabaseRepository


PRODUCT_ID = "internal-product-1"
CANDIDATE_ID = "candidate-1"
PRODUCT_CODE = "TSYC-FB-2026-001-CAN-0001"


def make_product(**overrides: Any) -> dict[str, Any]:
    product = {
        "internal_product_id": PRODUCT_ID,
        "candidate_id": CANDIDATE_ID,
        "primary_reference_id": "reference-1",
        "product_code": PRODUCT_CODE,
        "product_type": "BOOK",
        "title": "Verified Book Title",
        "author": "Author Name",
        "isbn": None,
        "publisher": "NXB Kim Đồng",
        "language_code": "vi",
        "page_count": None,
        "weight_grams": None,
        "length_cm": None,
        "width_cm": None,
        "height_cm": None,
        "cover_price_vnd": None,
        "metadata_status": "READY",
        "image_status": "APPROVED",
        "content_status": "DRAFTED",
        "woocommerce_status": "NOT_CREATED",
        "product_metadata": {},
        "is_active": True,
        "created_at": "2026-08-01T00:00:00+00:00",
    }
    product.update(overrides)
    return product


def make_drafted_content(**overrides: Any) -> dict[str, Any]:
    content = {
        "product_content_id": "content-1",
        "internal_product_id": PRODUCT_ID,
        "content_language": "vi",
        "product_name": "Verified Book Title",
        "short_description": "Mô tả ngắn bản nháp ban đầu.",
        "long_description": "Mô tả dài bản nháp ban đầu, chưa được biên tập.",
        "author_summary": "Tác giả của ấn phẩm là Author Name.",
        "product_details": "Tác giả: Author Name",
        "seo_title": "Verified Book Title",
        "seo_description": "Thông tin sách tại Tiệm Sách Yêu Con.",
        "content_status": "DRAFTED",
        "review_required": True,
        "generation_method": "RULE_BASED",
        "generator_name": ppc.GENERATOR_NAME,
        "generator_version": ppc.GENERATOR_VERSION,
        "review_notes": "Draft saved.",
        "approved_at": None,
    }
    content.update(overrides)
    return content


def write_content_file(tmp_path: Path, payload: dict[str, Any], name: str = "content.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def make_repository(
    product: dict[str, Any] | None = None,
    content: dict[str, Any] | None = None,
) -> FakeSupabaseRepository:
    tables: dict[str, list[dict[str, Any]]] = {
        "internal_products": [product] if product else [],
    }

    if content is not None:
        tables["product_contents"] = [content]

    return FakeSupabaseRepository(tables=tables)


# --------------------------------------------------------------------------
# 1. Valid REVISE of a DRAFTED row
# --------------------------------------------------------------------------


def test_revise_valid_drafted_row_applies_changes(tmp_path):
    product = make_product()
    existing = make_drafted_content()
    repository = make_repository(product, existing)

    content_file = write_content_file(
        tmp_path,
        {
            "short_description": "Mô tả ngắn đã được biên tập bởi người quản lý.",
            "long_description": "Mô tả dài đã được biên tập, có nội dung thật.",
        },
    )

    result = ppc.run_revise_action(
        repository=repository,
        product_code=PRODUCT_CODE,
        content_file=str(content_file),
        non_interactive=True,
        confirm_revise=True,
    )

    assert result is not None
    assert result["short_description"] == "Mô tả ngắn đã được biên tập bởi người quản lý."
    assert result["long_description"] == "Mô tả dài đã được biên tập, có nội dung thật."


# --------------------------------------------------------------------------
# 2. Omitted fields are preserved
# --------------------------------------------------------------------------


def test_revise_omitted_fields_preserved(tmp_path):
    product = make_product()
    existing = make_drafted_content()
    repository = make_repository(product, existing)

    content_file = write_content_file(
        tmp_path,
        {"short_description": "Chỉ sửa mô tả ngắn."},
    )

    result = ppc.run_revise_action(
        repository=repository,
        product_code=PRODUCT_CODE,
        content_file=str(content_file),
        non_interactive=True,
        confirm_revise=True,
    )

    assert result["short_description"] == "Chỉ sửa mô tả ngắn."
    assert result["long_description"] == existing["long_description"]
    assert result["seo_title"] == existing["seo_title"]
    assert result["seo_description"] == existing["seo_description"]
    assert result["product_details"] == existing["product_details"]
    # Never reviewer-editable -- always carried over unchanged.
    assert result["product_name"] == existing["product_name"]
    assert result["author_summary"] == existing["author_summary"]


# --------------------------------------------------------------------------
# 3 & 4. APPROVED / REJECTED content is refused
# --------------------------------------------------------------------------


def test_revise_refuses_approved_content(tmp_path):
    product = make_product()
    existing = make_drafted_content(content_status="APPROVED", review_required=False)
    repository = make_repository(product, existing)

    content_file = write_content_file(tmp_path, {"short_description": "x"})

    with pytest.raises(RuntimeError, match="APPROVED"):
        ppc.run_revise_action(
            repository=repository,
            product_code=PRODUCT_CODE,
            content_file=str(content_file),
            non_interactive=True,
            confirm_revise=True,
        )

    # No write occurred.
    assert repository.client.tables["product_contents"][0]["short_description"] == existing["short_description"]


def test_revise_refuses_rejected_content(tmp_path):
    product = make_product()
    existing = make_drafted_content(content_status="REJECTED", review_required=False)
    repository = make_repository(product, existing)

    content_file = write_content_file(tmp_path, {"short_description": "x"})

    with pytest.raises(RuntimeError, match="REJECTED"):
        ppc.run_revise_action(
            repository=repository,
            product_code=PRODUCT_CODE,
            content_file=str(content_file),
            non_interactive=True,
            confirm_revise=True,
        )


# --------------------------------------------------------------------------
# 5. Missing product_contents row is refused, not silently created
# --------------------------------------------------------------------------


def test_revise_refuses_missing_content_row(tmp_path):
    product = make_product()
    repository = make_repository(product, content=None)

    content_file = write_content_file(tmp_path, {"short_description": "x"})

    with pytest.raises(RuntimeError, match="existing product_contents row"):
        ppc.run_revise_action(
            repository=repository,
            product_code=PRODUCT_CODE,
            content_file=str(content_file),
            non_interactive=True,
            confirm_revise=True,
        )

    assert repository.client.tables.get("product_contents", []) == []


# --------------------------------------------------------------------------
# 6 & 7. Content file schema validation
# --------------------------------------------------------------------------


def test_revise_rejects_unknown_json_field(tmp_path):
    product = make_product()
    existing = make_drafted_content()
    repository = make_repository(product, existing)

    content_file = write_content_file(tmp_path, {"isbn": "9780000000000"})

    with pytest.raises(RuntimeError, match="unsupported field"):
        ppc.run_revise_action(
            repository=repository,
            product_code=PRODUCT_CODE,
            content_file=str(content_file),
            non_interactive=True,
            confirm_revise=True,
        )


def test_revise_rejects_non_string_field(tmp_path):
    product = make_product()
    existing = make_drafted_content()
    repository = make_repository(product, existing)

    content_file = write_content_file(tmp_path, {"short_description": 12345})

    with pytest.raises(RuntimeError, match="must be a string"):
        ppc.run_revise_action(
            repository=repository,
            product_code=PRODUCT_CODE,
            content_file=str(content_file),
            non_interactive=True,
            confirm_revise=True,
        )


def test_revise_rejects_null_field():
    with pytest.raises(RuntimeError, match="null"):
        ppc.validate_reviewer_content_payload({"short_description": None})


def test_revise_rejects_non_object_json_root(tmp_path):
    path = tmp_path / "content.json"
    path.write_text(json.dumps(["not", "an", "object"]), encoding="utf-8")

    with pytest.raises(RuntimeError, match="must be an object"):
        ppc.load_reviewer_content_file(path)


# --------------------------------------------------------------------------
# 8. Invalid JSON is refused
# --------------------------------------------------------------------------


def test_revise_rejects_invalid_json(tmp_path):
    path = tmp_path / "content.json"
    path.write_text("{not valid json", encoding="utf-8")

    with pytest.raises(RuntimeError, match="not valid JSON"):
        ppc.load_reviewer_content_file(path)


# --------------------------------------------------------------------------
# 9. Invalid UTF-8 is refused
# --------------------------------------------------------------------------


def test_revise_rejects_invalid_utf8(tmp_path):
    path = tmp_path / "content.json"
    # 0xff is not valid UTF-8 in any position.
    path.write_bytes(b'{"short_description": "\xff\xfe bad bytes"}')

    with pytest.raises(RuntimeError, match="UTF-8"):
        ppc.load_reviewer_content_file(path)


# --------------------------------------------------------------------------
# 10. A no-op revision performs no write
# --------------------------------------------------------------------------


def test_revise_no_op_performs_no_write(tmp_path):
    product = make_product()
    existing = make_drafted_content()
    repository = make_repository(product, existing)

    # Identical to the existing value -- nothing actually changes.
    content_file = write_content_file(
        tmp_path,
        {"short_description": existing["short_description"]},
    )

    result = ppc.run_revise_action(
        repository=repository,
        product_code=PRODUCT_CODE,
        content_file=str(content_file),
        non_interactive=True,
        confirm_revise=True,
    )

    assert result is None
    stored = repository.client.tables["product_contents"][0]
    assert stored == existing
    assert repository.client.tables.get("process_logs", []) == []


# --------------------------------------------------------------------------
# 11. Update uses the existing content row -- never an insert
# --------------------------------------------------------------------------


def test_revise_updates_in_place_no_duplicate_row(tmp_path):
    product = make_product()
    existing = make_drafted_content()
    repository = make_repository(product, existing)

    content_file = write_content_file(tmp_path, {"short_description": "Đã sửa."})

    ppc.run_revise_action(
        repository=repository,
        product_code=PRODUCT_CODE,
        content_file=str(content_file),
        non_interactive=True,
        confirm_revise=True,
    )

    stored_rows = repository.client.tables["product_contents"]
    assert len(stored_rows) == 1
    assert stored_rows[0]["product_content_id"] == "content-1"
    assert stored_rows[0]["short_description"] == "Đã sửa."


# --------------------------------------------------------------------------
# 12, 13, 14. Status/bookkeeping invariants
# --------------------------------------------------------------------------


def test_revise_keeps_content_status_drafted(tmp_path):
    product = make_product()
    existing = make_drafted_content()
    repository = make_repository(product, existing)

    content_file = write_content_file(tmp_path, {"short_description": "Đã sửa."})

    result = ppc.run_revise_action(
        repository=repository,
        product_code=PRODUCT_CODE,
        content_file=str(content_file),
        non_interactive=True,
        confirm_revise=True,
    )

    assert result["content_status"] == "DRAFTED"
    stored_product = repository.client.tables["internal_products"][0]
    assert stored_product["content_status"] == "DRAFTED"


def test_revise_keeps_review_required_true(tmp_path):
    product = make_product()
    existing = make_drafted_content()
    repository = make_repository(product, existing)

    content_file = write_content_file(tmp_path, {"short_description": "Đã sửa."})

    result = ppc.run_revise_action(
        repository=repository,
        product_code=PRODUCT_CODE,
        content_file=str(content_file),
        non_interactive=True,
        confirm_revise=True,
    )

    assert result["review_required"] is True


def test_revise_never_sets_approval_bookkeeping(tmp_path):
    product = make_product()
    existing = make_drafted_content()
    repository = make_repository(product, existing)

    content_file = write_content_file(tmp_path, {"short_description": "Đã sửa."})

    result = ppc.run_revise_action(
        repository=repository,
        product_code=PRODUCT_CODE,
        content_file=str(content_file),
        non_interactive=True,
        confirm_revise=True,
    )

    assert result["approved_at"] is None
    assert result["content_status"] != "APPROVED"


# --------------------------------------------------------------------------
# 15. Vietnamese content round-trips exactly
# --------------------------------------------------------------------------


def test_revise_vietnamese_content_roundtrips_exactly(tmp_path):
    product = make_product()
    existing = make_drafted_content()
    repository = make_repository(product, existing)

    short_description = (
        "“Jadoo IQ” là bộ combo 6 cuốn hiện có tại Tiệm Sách Yêu Con, gồm 6 "
        "chủ đề: thời gian, logic, số đếm, hình học, phép tính và không gian."
    )
    long_description = (
        "“Jadoo IQ” là bộ sách combo gồm 6 cuốn, mỗi cuốn tương ứng với một "
        "chủ đề: thời gian, logic, số đếm, hình học, phép tính và không gian."
        "\n\nSản phẩm được Tiệm Sách Yêu Con bán theo trọn bộ combo (1 sản "
        "phẩm/1 SKU), không bán lẻ từng cuốn qua kênh này."
    )

    content_file = write_content_file(
        tmp_path,
        {
            "short_description": short_description,
            "long_description": long_description,
        },
    )

    result = ppc.run_revise_action(
        repository=repository,
        product_code=PRODUCT_CODE,
        content_file=str(content_file),
        non_interactive=True,
        confirm_revise=True,
    )

    assert result["short_description"] == short_description
    assert result["long_description"] == long_description


# --------------------------------------------------------------------------
# 16. The generic APPROVE gate is unweakened by REVISE existing
# --------------------------------------------------------------------------


def test_generic_approve_gate_still_rejects_untouched_draft():
    product = make_product()
    generated = ppc.build_safe_draft(product)

    with pytest.raises(RuntimeError, match="generic metadata-only safe draft"):
        ppc.validate_approval_content(
            existing={"content_status": "DRAFTED", **generated},
            content=generated,
            generated=generated,
        )


# --------------------------------------------------------------------------
# 17. Exact targeting is required -- no implicit newest/all selection
# --------------------------------------------------------------------------


def test_revise_requires_exact_product_code(tmp_path):
    repository = make_repository()
    content_file = write_content_file(tmp_path, {"short_description": "x"})

    with pytest.raises(RuntimeError, match="--product-code"):
        ppc.run_revise_action(
            repository=repository,
            product_code=None,
            content_file=str(content_file),
            non_interactive=True,
            confirm_revise=True,
        )


def test_revise_requires_content_file():
    product = make_product()
    existing = make_drafted_content()
    repository = make_repository(product, existing)

    with pytest.raises(RuntimeError, match="--content-file"):
        ppc.run_revise_action(
            repository=repository,
            product_code=PRODUCT_CODE,
            content_file=None,
            non_interactive=True,
            confirm_revise=True,
        )


# --------------------------------------------------------------------------
# 18. Production write requires explicit confirmation
# --------------------------------------------------------------------------


def test_revise_non_interactive_requires_confirm_flag(tmp_path):
    repository = make_repository()
    content_file = write_content_file(tmp_path, {"short_description": "x"})

    with pytest.raises(RuntimeError, match="--confirm-revise"):
        ppc.run_revise_action(
            repository=repository,
            product_code=PRODUCT_CODE,
            content_file=str(content_file),
            non_interactive=True,
            confirm_revise=False,
        )

    # The gate fires before any repository access.
    assert repository.client.tables == {"internal_products": []}


def test_revise_interactive_cancel_performs_no_write(tmp_path):
    product = make_product()
    existing = make_drafted_content()
    repository = make_repository(product, existing)

    content_file = write_content_file(tmp_path, {"short_description": "Đã sửa."})

    result = ppc.run_revise_action(
        repository=repository,
        product_code=PRODUCT_CODE,
        content_file=str(content_file),
        non_interactive=False,
        confirm_revise=False,
        prompt=lambda _message: "",  # user presses Enter -- cancel
    )

    assert result is None
    stored = repository.client.tables["product_contents"][0]
    assert stored["short_description"] == existing["short_description"]


def test_revise_interactive_confirm_writes(tmp_path):
    product = make_product()
    existing = make_drafted_content()
    repository = make_repository(product, existing)

    content_file = write_content_file(tmp_path, {"short_description": "Đã sửa."})

    result = ppc.run_revise_action(
        repository=repository,
        product_code=PRODUCT_CODE,
        content_file=str(content_file),
        non_interactive=False,
        confirm_revise=False,
        prompt=lambda _message: "REVISE",
    )

    assert result is not None
    assert result["short_description"] == "Đã sửa."


# --------------------------------------------------------------------------
# Audit trail
# --------------------------------------------------------------------------


def test_revise_writes_process_log_with_changed_fields(tmp_path):
    product = make_product()
    existing = make_drafted_content()
    repository = make_repository(product, existing)

    content_file = write_content_file(tmp_path, {"short_description": "Đã sửa."})

    ppc.run_revise_action(
        repository=repository,
        product_code=PRODUCT_CODE,
        content_file=str(content_file),
        non_interactive=True,
        confirm_revise=True,
    )

    logs = repository.client.tables["process_logs"]
    assert len(logs) == 1
    assert logs[0]["process_name"] == "prepare_product_content"
    assert logs[0]["process_step"] == "REVISE"
    assert logs[0]["status"] == "HUMAN_REVIEW"
    assert logs[0]["candidate_id"] == CANDIDATE_ID
    assert "short_description" in logs[0]["message"]


# --------------------------------------------------------------------------
# Pure helper unit tests
# --------------------------------------------------------------------------


def test_diff_content_fields_reports_only_changed_fields():
    existing = make_drafted_content()
    revised = dict(existing)
    revised["short_description"] = "Mới."

    changes = ppc.diff_content_fields(existing, revised)

    assert changes == [("short_description", existing["short_description"], "Mới.")]


def test_build_revised_content_payload_never_changes_product_name():
    existing = make_drafted_content()

    revised = ppc.build_revised_content_payload(
        existing=existing,
        validated_payload={"short_description": "Mới."},
    )

    assert revised["product_name"] == existing["product_name"]
    assert revised["author_summary"] == existing["author_summary"]
