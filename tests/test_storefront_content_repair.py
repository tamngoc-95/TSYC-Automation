"""Regression tests for the FT-BATCH-9-2026-10-01 storefront-content defect.

AUTO_REVISE copied truncated, HTML-entity-encoded reference meta snippets
(and retailer SEO boilerplate) into customer-facing content, wrapped them in
stock wording and an internal provenance note, and the approval gate passed
them. These tests protect:

  1. HTML entity decoding
  2. truncated meta-description rejection
  3. internal provenance-note rejection
  4. internal workflow-note rejection
  5. unsupported stock wording rejection
  6. a full approved description is accepted
  7. repaired content stays customer-facing
  8. APPROVED content cannot be overwritten outside the authorized repair
  9. vi -> en/de localization cannot start from invalid vi content

plus the collector's description choice/refresh and the Woo draft gate.
Offline only: in-memory fake repository, no network, no browser.
"""
from __future__ import annotations

import json
from typing import Any

import pytest

import collect_reference_metadata as crm
import create_woocommerce_draft as cwd
import prepare_product_content as ppc
from src.domain.decisions import Outcome
from src.domain.rules import content_rules, storefront_text

from test_historical_low_touch_automation import (
    _generic_content_row,
    _historical_candidate,
    _internal_product,
    _reference,
    _repository,
)

# Real defective shapes observed in production (FT-BATCH-9-2026-10-01).
FAHASA_META_SNIPPET = (
    "Tư Duy Ngược, Tư Duy Ngược Ch&uacute;ng ta thực sự c&oacute; hạnh "
    "ph&uacute;c kh&ocirc;ng? Ch&uacute;ng ta c&oacute; đang sống cuộc đời "
    "m&igrave;nh k..."
)
NETABOOKS_SEO_BOILERPLATE = (
    "Sách ✔️ Đừng Lựa Chọn An Nhàn Khi Còn Trẻ của tác giả Cảnh Thiên, có bán "
    "tại Nhà sách online NetaBooks với ưu đãi Bao sách miễn phí và Gian hàng "
    "NetaBooks tại Tiki với ưu đãi Bao sách miễn phí và tặng Bookmark"
)
FULL_DESCRIPTION = (
    "Tư Duy Ngược\n\n"
    "Chúng ta thực sự có hạnh phúc không? Chúng ta có đang sống cuộc đời "
    "mình không? Có bao giờ bạn tự hỏi như thế, rồi có câu trả lời cho "
    "chính mình?\n\n"
    "Cuốn sách sẽ giải mã bạn là ai, bạn cần Tư duy ngược để thành công và "
    "hạnh phúc như thế nào và các phương pháp giúp bạn dũng cảm sống cuộc "
    "đời mà bạn muốn."
)
LEGACY_TEMPLATE_LONG = (
    "“Đắc Nhân Tâm” hiện có tại Tiệm Sách Yêu Con.\n\n"
    "Đắc Nhân Tâm, Đắc Nh&acirc;n T&acirc;m l&agrave; cuốn s&aacute;ch...\n\n"
    "(Mô tả tham khảo từ nguồn BOOKSTORE đã được xác minh.)"
)
LEGACY_TEMPLATE_SHORT = (
    "“Đắc Nhân Tâm” là ấn phẩm đang có tại Tiệm Sách Yêu Con. "
    "Đắc Nhân Tâm, Đắc Nh&acirc;n T&acirc;m l&agrave; cuốn s&aacute;ch..."
)


@pytest.fixture(autouse=True)
def _isolate_repair_audit_dir(tmp_path, monkeypatch):
    """Repair audit files must never land in the real data/processed/."""
    monkeypatch.setattr(ppc, "REPAIR_AUDIT_DIR", tmp_path / "content_repairs")


def _approved_row(internal_product: dict[str, Any], **fields: Any) -> dict[str, Any]:
    row = _generic_content_row(internal_product)
    row.update(
        {
            "content_status": "APPROVED",
            "review_required": False,
            "short_description": "Một cuốn sách kinh điển về nghệ thuật giao tiếp.",
            "long_description": (
                "Đắc Nhân Tâm là cuốn sách kinh điển về nghệ thuật giao tiếp "
                "và ứng xử, được hàng triệu độc giả trên thế giới tin đọc."
            ),
            "seo_description": "Đắc Nhân Tâm – sách kinh điển về nghệ thuật giao tiếp.",
            "author_summary": None,
        }
    )
    row.update(fields)
    return row


# --------------------------------------------------------------------------
# 1-5. Defect detection and normalization
# --------------------------------------------------------------------------


def test_html_entities_are_decoded_and_detected():
    assert storefront_text.decode_html_entities("Ch&uacute;ng ta &amp;aacute;") == "Chúng ta á"
    assert storefront_text.HTML_ENTITY in storefront_text.find_text_defects("Ch&uacute;ng ta")
    normalized = storefront_text.normalize_source_description(
        FAHASA_META_SNIPPET, titles=["Tư Duy Ngược"]
    )
    assert "&" not in normalized
    assert normalized.startswith("Chúng ta thực sự có hạnh phúc không?")


def test_truncated_meta_description_is_rejected():
    normalized = storefront_text.normalize_source_description(
        FAHASA_META_SNIPPET, titles=["Tư Duy Ngược"]
    )
    assert storefront_text.TRUNCATED in storefront_text.find_text_defects(normalized)
    assert not storefront_text.is_usable_source_description(normalized)

    content = {"long_description": normalized, "short_description": "Một cuốn sách hay."}
    check = content_rules.evaluate_storefront_text_quality(content)
    assert check.outcome == Outcome.REVIEW_REQUIRED
    assert "TRUNCATED" in check.reason


def test_cut_off_without_ellipsis_is_truncation():
    """Meta snippets cut at a character limit with no "..." (observed:
    "...một cuộc chơi hoàn toàn") are truncation for prose fields and are
    never usable as a source description."""
    cut = (
        "Một nhà chiến lược với tầm nhìn xa thay đổi cuộc chơi mà doanh "
        "nghiệp của mình đang chơi hay nghĩ ra một cuộc chơi hoàn toàn"
    )
    findings = storefront_text.find_content_defects(
        {"long_description": cut, "seo_title": "Kẻ Làm Thay Đổi Cuộc Chơi"},
        content_rules.CUSTOMER_FACING_FIELDS,
    )
    assert findings == {"long_description": [storefront_text.TRUNCATED]}
    assert not storefront_text.is_usable_source_description(cut)


def test_mid_sentence_ellipsis_is_not_truncation():
    text = "Cậu bé chờ đợi… rồi bật cười. Câu chuyện kết thúc bằng một bài học nhỏ."
    assert storefront_text.find_text_defects(text) == set()


def test_internal_provenance_note_is_rejected():
    content = {"long_description": "Nội dung sách.\n\n(Mô tả tham khảo từ nguồn FAHASA đã được xác minh.)"}
    check = content_rules.evaluate_storefront_text_quality(content)
    assert check.outcome == Outcome.REVIEW_REQUIRED
    assert "PROVENANCE_NOTE" in check.reason


@pytest.mark.parametrize(
    "text",
    [
        "Phần giới thiệu cần được người quản lý kiểm tra trước khi xuất bản.",
        "This description should be completed later.",
        "Nội dung sẽ được bổ sung sau.",
    ],
)
def test_internal_workflow_note_is_rejected(text):
    check = content_rules.evaluate_storefront_text_quality({"long_description": text})
    assert check.outcome == Outcome.REVIEW_REQUIRED
    assert "INTERNAL_WORKFLOW" in check.reason


@pytest.mark.parametrize(
    "text, code",
    [
        ("Sách hiện có tại Tiệm Sách Yêu Con.", storefront_text.STOCK_WORDING),
        ("“X” là ấn phẩm đang có tại Tiệm Sách Yêu Con.", storefront_text.STOCK_WORDING),
        ("Available now at Tiệm Sách Yêu Con.", storefront_text.STOCK_WORDING),
        ("Mua ngay! Giao nhanh, giá tốt.", storefront_text.SHIPPING_WORDING),
        (NETABOOKS_SEO_BOILERPLATE, storefront_text.SOURCE_BOILERPLATE),
    ],
)
def test_stock_shipping_and_retailer_wording_is_rejected(text, code):
    assert code in storefront_text.find_text_defects(text)
    check = content_rules.evaluate_storefront_text_quality({"short_description": text})
    assert check.outcome == Outcome.REVIEW_REQUIRED


def test_retailer_boilerplate_only_source_is_never_usable():
    normalized = storefront_text.normalize_source_description(
        NETABOOKS_SEO_BOILERPLATE, titles=["Đừng Lựa Chọn An Nhàn Khi Còn Trẻ"]
    )
    assert normalized == ""
    assert not storefront_text.is_usable_source_description(normalized)


# --------------------------------------------------------------------------
# 6. Full approved description is accepted
# --------------------------------------------------------------------------


def test_full_description_is_usable_and_approvable():
    normalized = storefront_text.normalize_source_description(
        FULL_DESCRIPTION, titles=["Tư Duy Ngược"]
    )
    assert not normalized.startswith("Tư Duy Ngược\n")  # heading line dropped
    assert storefront_text.is_usable_source_description(normalized)

    product = {"title": "Tư Duy Ngược", "author": "Nguyễn Anh Dũng"}
    generated = {"product_name": "Tư Duy Ngược", "seo_title": "Tư Duy Ngược – Nguyễn Anh Dũng"}
    content = ppc.build_historical_enrichment_content(
        generated=generated, product=product, normalized_description=normalized
    )
    assert content_rules.evaluate_storefront_text_quality(content).is_auto_pass
    # Whole sentences only, never an ellipsis cut.
    assert not content["short_description"].endswith(("...", "…"))
    assert content["short_description"] in content["long_description"]
    assert "Tiệm Sách Yêu Con" not in json.dumps(content, ensure_ascii=False)


def test_leading_sentences_never_cuts_mid_sentence():
    text = "Câu một rất ngắn. Câu hai dài hơn một chút nữa. Câu ba."
    assert storefront_text.leading_sentences(text, 20) == "Câu một rất ngắn."
    assert storefront_text.leading_sentences(text, 5) == ""
    # Never ends on an ellipsis sentence (would read as truncation).
    assert storefront_text.leading_sentences("Câu một. Rồi bé chờ… Câu ba dài hơn nhiều lắm.", 25) == "Câu một."
    # A subtitle line without a full stop is never used as the summary.
    with_subtitle = "Đánh thức nội lực, thay đổi tư duy\n\n" + text
    assert storefront_text.leading_sentences(with_subtitle, 20) == "Câu một rất ngắn."


def test_draft_safe_reference_selection_skips_truncated_description():
    candidate = _historical_candidate()
    truncated = _reference(reference_description=FAHASA_META_SNIPPET, reference_title="Tư Duy Ngược")
    selection = content_rules.select_historical_draft_safe_content_reference(candidate, [truncated])
    assert selection.outcome != Outcome.AUTO_PASS


def test_auto_revise_output_has_no_provenance_or_stock_wording():
    candidate = _historical_candidate()
    internal_product = _internal_product(content_status="REVIEW_REQUIRED", image_status="APPROVED")
    repository = _repository(
        candidate,
        internal_product,
        contents=[_generic_content_row(internal_product)],
        references=[_reference()],
    )
    result = ppc.run_auto_revise_action(
        repository=repository,
        product_code=internal_product["product_code"],
        non_interactive=True,
        confirm_revise=True,
    )
    assert result["content_status"] == "APPROVED"
    for field in content_rules.CUSTOMER_FACING_FIELDS:
        assert not storefront_text.find_text_defects(result.get(field)), field
    assert "Mô tả tham khảo" not in result["long_description"]


# --------------------------------------------------------------------------
# 7. Repaired content remains customer-facing
# --------------------------------------------------------------------------


def test_repair_template_row_rebuilds_from_usable_reference():
    candidate = _historical_candidate()
    internal_product = _internal_product()
    existing = _approved_row(
        internal_product,
        long_description=LEGACY_TEMPLATE_LONG,
        short_description=LEGACY_TEMPLATE_SHORT,
    )
    repository = _repository(
        candidate, internal_product, contents=[existing], references=[_reference()]
    )

    report = ppc.run_repair_action(
        repository=repository,
        product_code=internal_product["product_code"],
        non_interactive=True,
        confirm_repair=True,
    )

    assert report["written"] is True
    assert report["strategy"] == "TEMPLATE_REBUILD"
    row = repository.client.tables["product_contents"][0]
    assert row["content_status"] == "APPROVED"
    assert row["review_required"] is False
    for field in content_rules.CUSTOMER_FACING_FIELDS:
        assert not storefront_text.find_text_defects(row.get(field)), field
    assert row["long_description"].startswith("Đắc Nhân Tâm là cuốn sách kinh điển")
    # Provenance kept internally, never in storefront text.
    assert "STOREFRONT_REPAIR" in row["review_notes"]
    logs = repository.client.tables.get("process_logs", [])
    assert any(log.get("status") == "REPAIRED" for log in logs)
    repaired_log = next(log for log in logs if log.get("status") == "REPAIRED")
    assert repaired_log["error_details"]["previous"]["long_description"] == LEGACY_TEMPLATE_LONG
    assert len(repository.client.tables["product_contents"]) == 1


def test_repair_sentence_strip_keeps_factual_text_verbatim():
    candidate = _historical_candidate()
    internal_product = _internal_product()
    factual = (
        "“Đời Ngắn Đừng Ngủ Dài” của Robin Sharma mở đầu bằng thông điệp về "
        "những lựa chọn trong cuộc sống."
    )
    existing = _approved_row(
        internal_product,
        long_description=factual + "\n\nSách hiện có tại Tiệm Sách Yêu Con.",
        seo_description="Sách Đời Ngắn Đừng Ngủ Dài của Robin Sharma, NXB Trẻ, 228 trang - có tại Tiệm Sách Yêu Con.",
    )
    plan = ppc.plan_storefront_repair(existing, internal_product, candidate, [])
    assert plan["outcome"] == ppc.REPAIR_OUTCOME_REPAIRABLE
    assert plan["strategy"] == "SENTENCE_REPAIR"
    assert plan["content"]["long_description"] == factual
    assert plan["content"]["seo_description"] == (
        "Sách Đời Ngắn Đừng Ngủ Dài của Robin Sharma, NXB Trẻ, 228 trang."
    )


def test_repair_with_only_truncated_stored_source_needs_recollection():
    candidate = _historical_candidate()
    internal_product = _internal_product()
    existing = _approved_row(
        internal_product,
        long_description=LEGACY_TEMPLATE_LONG,
        short_description=LEGACY_TEMPLATE_SHORT,
    )
    truncated_ref = _reference(reference_description=FAHASA_META_SNIPPET)
    repository = _repository(
        candidate, internal_product, contents=[existing], references=[truncated_ref]
    )

    report = ppc.run_repair_action(
        repository=repository,
        product_code=internal_product["product_code"],
        non_interactive=True,
        confirm_repair=True,
    )

    assert report["written"] is False
    assert report["outcome"] == ppc.REPAIR_OUTCOME_NEEDS_SOURCE_RECOLLECTION
    row = repository.client.tables["product_contents"][0]
    # Never guessed, never silently downgraded.
    assert row["long_description"] == LEGACY_TEMPLATE_LONG
    assert row["content_status"] == "APPROVED"


# --------------------------------------------------------------------------
# 8. APPROVED content cannot be overwritten outside the authorized repair
# --------------------------------------------------------------------------


def test_repair_refuses_valid_approved_content():
    candidate = _historical_candidate()
    internal_product = _internal_product()
    existing = _approved_row(internal_product)
    repository = _repository(candidate, internal_product, contents=[existing], references=[_reference()])

    with pytest.raises(RuntimeError, match="passes storefront validation"):
        ppc.run_repair_action(
            repository=repository,
            product_code=internal_product["product_code"],
            non_interactive=True,
            confirm_repair=True,
        )
    assert repository.client.tables["product_contents"][0] == existing


def test_repair_requires_explicit_confirmation():
    candidate = _historical_candidate()
    internal_product = _internal_product()
    existing = _approved_row(internal_product, long_description=LEGACY_TEMPLATE_LONG)
    repository = _repository(candidate, internal_product, contents=[existing], references=[_reference()])

    with pytest.raises(RuntimeError, match="--confirm-repair"):
        ppc.run_repair_action(
            repository=repository,
            product_code=internal_product["product_code"],
            non_interactive=True,
            confirm_repair=False,
        )


def test_repair_refuses_non_approved_rows():
    candidate = _historical_candidate()
    internal_product = _internal_product(content_status="DRAFTED")
    existing = _approved_row(internal_product, content_status="DRAFTED", long_description=LEGACY_TEMPLATE_LONG)
    repository = _repository(candidate, internal_product, contents=[existing], references=[_reference()])

    with pytest.raises(RuntimeError, match="only applies to an existing APPROVED"):
        ppc.run_repair_action(
            repository=repository,
            product_code=internal_product["product_code"],
            non_interactive=True,
            confirm_repair=True,
        )


def test_repair_preview_writes_nothing():
    candidate = _historical_candidate()
    internal_product = _internal_product()
    existing = _approved_row(internal_product, long_description=LEGACY_TEMPLATE_LONG)
    repository = _repository(candidate, internal_product, contents=[existing], references=[_reference()])

    report = ppc.run_repair_action(
        repository=repository,
        product_code=internal_product["product_code"],
        non_interactive=True,
        confirm_repair=False,
        preview_only=True,
    )
    assert report["outcome"] == ppc.REPAIR_OUTCOME_REPAIRABLE
    assert report["written"] is False
    assert repository.client.tables["product_contents"][0]["long_description"] == LEGACY_TEMPLATE_LONG


def test_approval_gate_rejects_legacy_template_content():
    product = _internal_product()
    generated = ppc.build_safe_draft(product)
    content = {**generated, "long_description": LEGACY_TEMPLATE_LONG, "short_description": LEGACY_TEMPLATE_SHORT}
    with pytest.raises(RuntimeError, match="CONTENT_STOREFRONT_TEXT_DEFECT"):
        ppc.validate_approval_content(
            existing={"content_status": "DRAFTED", **content},
            content=content,
            generated=generated,
        )


# --------------------------------------------------------------------------
# 9. Localization cannot start from invalid Vietnamese canonical content
# --------------------------------------------------------------------------


def test_translation_refuses_defective_approved_vietnamese():
    vi = {
        "content_status": "APPROVED",
        "review_required": False,
        "long_description": LEGACY_TEMPLATE_LONG,
        "short_description": LEGACY_TEMPLATE_SHORT,
    }
    with pytest.raises(RuntimeError, match="fails storefront validation"):
        ppc.require_approved_vietnamese_content(vi, "TSYC-X")


def test_translation_accepts_valid_approved_vietnamese():
    vi = {
        "content_status": "APPROVED",
        "review_required": False,
        "long_description": "Đắc Nhân Tâm là cuốn sách kinh điển về nghệ thuật giao tiếp.",
        "short_description": "Sách kinh điển về nghệ thuật giao tiếp.",
    }
    assert ppc.require_approved_vietnamese_content(vi, "TSYC-X") is vi


# --------------------------------------------------------------------------
# Collector: description choice and audited refresh
# --------------------------------------------------------------------------


def test_collector_prefers_full_description_over_meta_snippet():
    chosen = crm.choose_description([FULL_DESCRIPTION, FAHASA_META_SNIPPET])
    assert chosen == storefront_text.clean_source_text(FULL_DESCRIPTION)
    # Even when the snippet comes first, a usable full text wins.
    chosen = crm.choose_description([FAHASA_META_SNIPPET, NETABOOKS_SEO_BOILERPLATE, FULL_DESCRIPTION])
    assert chosen == storefront_text.clean_source_text(FULL_DESCRIPTION)


def test_collector_never_uses_a_container_with_customer_reviews():
    """NetaBooks' outer container also holds reviews and the buy box; a
    review must never become product copy, even if it ends a sentence."""
    with_reviews = (
        FULL_DESCRIPTION
        + "\n\n0% | 0 đánh giá\n\nGỬI ĐÁNH GIÁ CỦA BẠN\n\nĐặng Quý\n\n"
        "Nguyên Phong là cái tên bảo chứng cho những cuốn sách nên đọc!\n\n"
        "Trả lời 6 năm trước\n\nTiết kiệm: 19.600 ₫-20%\n\nCHỌN MUA"
    )
    assert storefront_text.contains_page_chrome(with_reviews)
    assert crm.choose_description([with_reviews]) == storefront_text.clean_source_text(with_reviews)
    chosen = crm.choose_description([with_reviews, FULL_DESCRIPTION])
    assert chosen == storefront_text.clean_source_text(FULL_DESCRIPTION)
    reference = _reference(reference_description="old")
    assert crm.build_description_refresh_update(reference, with_reviews, "t") is None


def test_trailing_page_chrome_is_trimmed():
    text = (
        FULL_DESCRIPTION
        + "\n\n…\n\nMục lục:\n\nChương 1: Mở đầu\n\nTài liệu tham khảo\n\n"
        "Xem tất cả sách của tác giả Nguyễn Anh Dũng"
    )
    normalized = storefront_text.normalize_source_description(text, titles=["Tư Duy Ngược"])
    assert normalized.endswith("dũng cảm sống cuộc đời mà bạn muốn.")
    assert storefront_text.is_usable_source_description(normalized)


def test_collector_stores_decoded_text_when_nothing_usable():
    chosen = crm.choose_description([FAHASA_META_SNIPPET])
    assert chosen is not None and "&uacute;" not in chosen


def test_description_refresh_preserves_previous_value_and_identity():
    reference = _reference(
        reference_description=FAHASA_META_SNIPPET,
        reference_title="Tư Duy Ngược",
        match_decision="MATCH",
        raw_metadata={"parser_name": "fahasa"},
    )
    update = crm.build_description_refresh_update(
        reference=reference,
        new_description=storefront_text.clean_source_text(FULL_DESCRIPTION),
        refreshed_at="2026-10-01T00:00:00+00:00",
    )
    assert set(update) == {"reference_description", "raw_metadata", "updated_at"}
    history = update["raw_metadata"]["description_refresh"]["history"]
    assert history[-1]["previous_reference_description"] == FAHASA_META_SNIPPET
    assert update["raw_metadata"]["parser_name"] == "fahasa"


def test_description_refresh_refuses_unusable_text():
    reference = _reference(reference_description="old")
    assert crm.build_description_refresh_update(reference, FAHASA_META_SNIPPET, "t") is None
    assert crm.build_description_refresh_update(reference, None, "t") is None


# --------------------------------------------------------------------------
# WooCommerce: defective content never reaches a draft
# --------------------------------------------------------------------------


def test_woo_draft_creation_refuses_defective_content():
    with pytest.raises(RuntimeError, match="fails storefront validation"):
        cwd.require_storefront_valid_content(
            {"long_description": LEGACY_TEMPLATE_LONG, "short_description": "Ok."}
        )
    cwd.require_storefront_valid_content(
        {"long_description": "Đắc Nhân Tâm là cuốn sách kinh điển.", "short_description": "Sách kinh điển."}
    )
