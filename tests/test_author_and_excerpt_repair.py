"""
Regression tests (2026-10-02 storefront follow-up):

  - "Đọc thử" (a publisher-page button label) stored as author: never
    collected, never kept, and replaced only from deterministic
    registered-reference evidence (author_rules / repair_product_author).
  - AUTOIMPORT-CAN-0044: a verbatim quoted novel passage became the
    storefront short description. A summary field must never open with a
    quoted book passage; a leading passage followed by real description
    prose is removed deterministically; a quoted *title* is never flagged.

Offline, pure functions only.
"""
from __future__ import annotations

import collect_reference_metadata as crm
import prepare_product_content as ppc
import repair_product_author as rpa
from src.domain.rules import author_rules, content_rules, storefront_text

# --- author ------------------------------------------------------------


def test_ui_labels_are_invalid_authors_real_names_are_not():
    for value in ("Đọc thử", " đọc thử ", "Xem thêm"):
        assert author_rules.is_invalid_author_value(value)
    for value in ("Viktor E Frankl", "Đỗ Bích Thúy", "Eckhart Tolle", None, ""):
        assert not author_rules.is_invalid_author_value(value)


def test_collector_never_returns_a_ui_label_as_author():
    assert crm.extract_author("Tác giả: Đọc thử\nNhà xuất bản: X", None) is None
    assert crm.extract_author("", {"author": {"name": "Đọc thử"}}) is None
    assert crm.extract_author("Tác giả: Viktor E Frankl\n", None) == "Viktor E Frankl"


PRODUCT = {"title": "Đi Tìm Lẽ Sống", "publisher": "Tổng hợp TP Hồ Chí Minh", "author": "Đọc thử"}


def _ref(**overrides):
    ref = {
        "reference_id": "r-fahasa",
        "source_url_id": "su-1",
        "source_type": "FAHASA",
        "match_decision": "MANUAL_REVIEW",
        "reference_title": "Đi Tìm Lẽ Sống (Tái Bản 2022)",
        "reference_author": "Viktor E Frankl",
        "reference_publisher": "NXB Tổng Hợp TPHCM",
    }
    ref.update(overrides)
    return ref


def test_author_resolved_from_same_title_and_publisher_reference():
    publisher_ref = _ref(reference_id="r-pub", source_type="PUBLISHER", reference_title="Đi Tìm Lẽ Sống",
                         reference_author="Đọc thử", reference_publisher="Tổng hợp TP Hồ Chí Minh")
    author, evidence = author_rules.resolve_author_from_references(PRODUCT, [publisher_ref, _ref()])
    assert author == "Viktor E Frankl"
    assert evidence["qualifying_reference_ids"] == ["r-fahasa"]


def test_publisher_spelling_variants_normalize():
    for variant in ("NXB Tổng hợp TP.HCM", "Nhà xuất bản Tổng Hợp TPHCM", "Tổng hợp tp Hồ Chí Minh"):
        assert author_rules.publisher_key(variant) == author_rules.publisher_key("Tổng hợp TP Hồ Chí Minh")


def test_author_not_resolved_without_deterministic_evidence():
    cases = [
        [_ref(reference_publisher="NXB Trẻ")],  # different publisher
        [_ref(reference_title="Đi Tìm Lẽ Sống Của Tôi")],  # different title
        [_ref(source_url_id=None)],  # no registered provenance
        [_ref(source_type="FACEBOOK_POST")],  # not an approved catalogue source
        [_ref(match_decision="NO_MATCH")],
        [_ref(), _ref(reference_id="r2", reference_author="Someone Else")],  # disagreement
        [],
    ]
    for references in cases:
        author, _ = author_rules.resolve_author_from_references(PRODUCT, references)
        assert author is None, references


def test_repair_plan_resolves_or_clears_and_flags_review():
    resolved = rpa.plan_author_repair({**PRODUCT, "product_metadata": {"x": 1}}, [_ref()], "t")
    assert resolved["outcome"] == rpa.RESOLVED
    assert resolved["update"]["author"] == "Viktor E Frankl"
    assert resolved["update"]["product_metadata"]["x"] == 1
    assert resolved["update"]["product_metadata"]["author_repair"]["previous_author"] == "Đọc thử"
    assert "review_required" not in resolved["update"]

    unresolved = rpa.plan_author_repair(dict(PRODUCT), [], "t")
    assert unresolved["outcome"] == rpa.UNRESOLVED
    assert unresolved["update"]["author"] is None  # a UI label is never kept
    assert unresolved["update"]["review_required"] is True


def test_repair_plan_never_touches_a_real_author():
    plan = rpa.plan_author_repair({**PRODUCT, "author": "Viktor E Frankl"}, [_ref(reference_author="X")], "t")
    assert plan["outcome"] == rpa.NOT_DEFECTIVE
    assert plan["update"] is None


# --- quoted book excerpts ------------------------------------------------

EXCERPT = (
    "“... tại sao con người lại làm thế này với nhau? Súa muốn hỏi, vì sao Súa chấp nhận "
    "làm vợ Phống rồi, ở yên trong nhà họ Tráng rồi mà Phống vẫn không vừa lòng? Súa thấy "
    "sợ Phống, thấy ghê tởm, rùng mình, buồn nôn.”"
)
DESCRIPTION = (
    "Lặng yên dưới vực sâu, với lối viết chân thực đến gai người, không ngừng tra vấn "
    "chúng ta bằng câu hỏi nhức nhối ấy. Tiểu thuyết của Đỗ Bích Thúy là nỗi trăn trở "
    "khôn nguôi về cuộc sống với biết bao những bi kịch."
)


def test_quoted_title_is_not_an_excerpt():
    assert not storefront_text.starts_with_quoted_excerpt("“Gấu con đi ngủ” là sách tranh của Lê Minh.")
    long_title = "Combo Sách Diary Of A Wimpy Kid - Nhật Ký Chú Bé Nhút Nhát - Song Ngữ Việt-Anh: Tập 1 - 18"
    assert not storefront_text.starts_with_quoted_excerpt(f"“{long_title}” – Jeff Kinney.", titles=[long_title])
    assert storefront_text.starts_with_quoted_excerpt(EXCERPT)


def test_summary_opening_with_excerpt_is_a_defect():
    content = {"product_name": "Lặng Yên Dưới Vực Sâu", "short_description": EXCERPT,
               "long_description": f"{EXCERPT}\n\n{DESCRIPTION}", "seo_description": EXCERPT[:150]}
    findings = storefront_text.find_content_defects(content, content_rules.CUSTOMER_FACING_FIELDS)
    assert storefront_text.QUOTED_EXCERPT in findings["short_description"]
    assert storefront_text.QUOTED_EXCERPT in findings["long_description"]
    assert storefront_text.QUOTED_EXCERPT in findings["seo_description"]


def test_excerpt_only_long_description_is_not_flagged_but_its_summary_is():
    content = {"product_name": "T", "short_description": EXCERPT, "long_description": EXCERPT}
    findings = storefront_text.find_content_defects(content, content_rules.CUSTOMER_FACING_FIELDS)
    assert "long_description" not in findings
    assert storefront_text.QUOTED_EXCERPT in findings["short_description"]


def test_source_normalization_drops_leading_excerpt_and_summary_skips_it():
    normalized = storefront_text.normalize_source_description(f"{EXCERPT}\n\n{DESCRIPTION}")
    assert normalized == DESCRIPTION
    assert storefront_text.leading_sentences(f"{EXCERPT}\n\n{DESCRIPTION}", 300).startswith("Lặng yên")


def test_repair_removes_excerpt_without_adding_words():
    existing = {
        "product_content_id": "pc-1",
        "content_status": "APPROVED",
        "product_name": "Lặng Yên Dưới Vực Sâu",
        "short_description": EXCERPT,
        "long_description": f"{EXCERPT}\n\n{DESCRIPTION}",
        "author_summary": None,
        "product_details": "Tác giả: Đỗ Bích Thúy",
        "seo_title": "Lặng Yên Dưới Vực Sâu – Đỗ Bích Thúy",
        "seo_description": EXCERPT[:150],
    }
    product = {"title": "Lặng Yên Dưới Vực Sâu", "author": "Đỗ Bích Thúy"}
    plan = ppc.plan_storefront_repair(existing, product, {}, [])

    assert plan["outcome"] == ppc.REPAIR_OUTCOME_REPAIRABLE
    assert plan["category"] == ppc.REPAIR_CATEGORY_QUOTED_EXCERPT
    content = plan["content"]
    assert content["long_description"] == DESCRIPTION
    assert "Súa" not in content["short_description"]
    assert "Súa" not in content["seo_description"]
    assert not storefront_text.find_content_defects(content, content_rules.CUSTOMER_FACING_FIELDS)


def test_excerpt_only_repair_uses_verified_title_line_for_summary():
    existing = {
        "content_status": "APPROVED",
        "product_name": "Thương Nhớ Mười Hai",
        "short_description": EXCERPT,
        "long_description": EXCERPT,
        "seo_title": "Thương Nhớ Mười Hai – Vũ Bằng",
        "seo_description": "“Thương Nhớ Mười Hai” – Vũ Bằng.",
    }
    plan = ppc.plan_storefront_repair(existing, {"title": "Thương Nhớ Mười Hai", "author": "Vũ Bằng"}, {}, [])
    assert plan["outcome"] == ppc.REPAIR_OUTCOME_REPAIRABLE
    assert plan["content"]["short_description"] == "“Thương Nhớ Mười Hai” – Vũ Bằng."
    assert plan["content"]["long_description"] == EXCERPT  # nothing invented to replace it
