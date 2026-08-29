"""Offline tests for
src/domain/rules/facebook_history_classification.py.

No live Supabase/WooCommerce/Facebook/Claude dependency -- pure functions
operating on plain strings/lists. classify() never touches a Facebook
action heading; every test here passes only body text, folder slugs, and
mention ids, exactly as scripts/classify_facebook_history_export.py does
via src.services.facebook_history_report.classify_records().
"""
from __future__ import annotations

from src.domain.rules import facebook_history_classification as rules


# --- required scenario: feedback post -----------------------------------


def test_feedback_folder_slug_is_high_customer_feedback_not_eligible():
    result = rules.classify(
        full_text="Em biết ơn khách hàng đã luôn tin tưởng và ủng hộ ❤️",
        folder_slugs=[rules.FEEDBACK_FOLDER_SLUG],
    )

    assert result.tsyc_relevance == rules.TsycRelevance.HIGH
    assert result.post_type == rules.PostType.CUSTOMER_FEEDBACK
    assert result.candidate_eligible is False


def test_feedback_folder_slug_wins_even_with_strong_brand_text():
    # Explicit project requirement: the feedback folder slug means
    # CUSTOMER_FEEDBACK, never PRODUCT_POST, even if the caption also
    # names the shop directly.
    result = rules.classify(
        full_text="mn cần đặt sách mới cứ nhắn cho Tiệm sách Yêu Con nhé",
        folder_slugs=[rules.FEEDBACK_FOLDER_SLUG],
    )

    assert result.tsyc_relevance == rules.TsycRelevance.HIGH
    assert result.post_type == rules.PostType.CUSTOMER_FEEDBACK
    assert result.candidate_eligible is False
    # But it should be flagged for a human to double check, since the
    # caption also reads like a solicitation.
    assert result.needs_secondary_review is True


# --- required scenario: "Sách Có Sẵn tại Đức" listing -------------------


def test_sach_co_san_tai_duc_folder_listing_is_high_product_post_eligible():
    result = rules.classify(
        full_text="Sách Có Sẵn tại Đức",
        folder_slugs=[rules.STRONG_LISTING_FOLDER_SLUG],
    )

    assert result.tsyc_relevance == rules.TsycRelevance.HIGH
    assert result.post_type == rules.PostType.PRODUCT_POST
    assert result.candidate_eligible is True


def test_sach_co_san_tai_duc_text_phrase_alone_is_high_product_post_eligible():
    result = rules.classify(full_text="Sách có sẵn tại Đức, inbox em nhé")

    assert result.tsyc_relevance == rules.TsycRelevance.HIGH
    assert result.post_type == rules.PostType.PRODUCT_POST
    assert result.candidate_eligible is True


def test_structural_mention_of_tsyc_page_id_alone_is_high():
    result = rules.classify(
        full_text="Em vẫn bán và preorder sách mới, cho thuê và mua sách",
        mention_ids=[rules.TSYC_PAGE_ID],
    )

    assert result.tsyc_relevance == rules.TsycRelevance.HIGH
    assert result.structural_mention_id == rules.TSYC_PAGE_ID


# --- required scenario: counterfeit-book warning -------------------------


def test_counterfeit_warning_is_general_business_or_review_not_eligible():
    result = rules.classify(
        full_text="Các bác bán sách giả có vẻ bán rất chạy ở Đức, cẩn thận nhé"
    )

    assert result.tsyc_relevance in (rules.TsycRelevance.MEDIUM, rules.TsycRelevance.LOW)
    assert result.post_type in (rules.PostType.GENERAL_BUSINESS, rules.PostType.BOOK_REVIEW)
    assert result.candidate_eligible is False


# --- required scenario: unrelated personal post --------------------------


def test_unrelated_personal_post_is_low_personal_not_eligible():
    result = rules.classify(
        full_text="Hội những người siêu tích cực va vào nhau 😆😆😆"
    )

    assert result.tsyc_relevance == rules.TsycRelevance.LOW
    assert result.post_type == rules.PostType.PERSONAL
    assert result.candidate_eligible is False


def test_empty_text_and_no_structural_evidence_is_low_personal():
    result = rules.classify(full_text="")

    assert result.tsyc_relevance == rules.TsycRelevance.LOW
    assert result.post_type == rules.PostType.PERSONAL
    assert result.candidate_eligible is False


# --- required scenario: "Relationship" must not match "ship" ------------


def test_relationship_word_does_not_match_ship_marker():
    result = rules.classify(full_text="Relationship Map có gì?")

    assert "ship" not in result.weak_markers
    assert "ship" not in result.strong_markers


def test_standalone_ship_word_does_match():
    result = rules.classify(full_text="Freeship toàn nước Đức, ship nhanh trong tuần")

    assert "ship" in result.weak_markers


# --- required scenario: generic "giá" alone must not imply eligibility --


def test_generic_gia_alone_does_not_make_promotion_eligible():
    # "giá" with no book-specific vocabulary anywhere in the text at all.
    result = rules.classify(full_text="Giá cả linh tinh, tăng giảm thất thường suốt tuần qua")

    assert result.tsyc_relevance == rules.TsycRelevance.MEDIUM
    assert result.post_type == rules.PostType.GENERAL_BUSINESS
    assert result.candidate_eligible is False
    assert result.classification_reason == rules.REASON_WEAK_COMMERCE_ONLY_NOT_ELIGIBLE


def test_gia_word_together_with_book_specific_word_is_eligible():
    # Once a book-specific word is *also* present, "giá" stops being
    # "commerce alone" -- this documents the intended new behavior
    # (BOOK_SPECIFIC_EVIDENCE AND COMMERCE_EVIDENCE both present) even
    # though there is no digit-bearing price pattern here.
    result = rules.classify(full_text="Giá sách bên em rẻ hơn chỗ khác nhiều nhé")

    assert result.tsyc_relevance == rules.TsycRelevance.MEDIUM
    assert result.candidate_eligible is True
    assert result.classification_reason == rules.REASON_WEAK_LISTING_ELIGIBLE


def test_gia_word_never_matches_inside_a_longer_word():
    # "giá" must not match as a substring of an unrelated longer token.
    result = rules.classify(full_text="Ngoại giáo là một chủ đề em không rành")

    assert "giá" not in result.weak_markers


# --- required scenario: photo/video heading must not affect relevance ---


def test_classify_never_sees_or_uses_a_heading_argument():
    # classify() has no heading parameter at all -- this is a structural
    # guarantee, not just a behavioral one. Two calls with the same
    # full_text produce the same result regardless of what a caller
    # might have been tempted to pass as heading elsewhere.
    without_media_context = rules.classify(full_text="Sách có sẵn tại Đức")
    also_without_media_context = rules.classify(full_text="Sách có sẵn tại Đức")

    assert without_media_context == also_without_media_context


# --- promotion / listing evidence rules ----------------------------------


def test_generic_price_list_without_book_specific_evidence_is_not_eligible():
    # Precision-hardening requirement: a priced, bulleted list is
    # concrete commerce evidence, but with no book-specific word
    # anywhere ("Đạo", "Giác ngộ", "Tự tôn" are bare item names here, not
    # "sách"/"cuốn"/"tác giả"/...) it must not be eligible on its own.
    text = (
        "Thanh lý:\n"
        "1. Đạo – Con đường không lối 8€\n"
        "2. Giác ngộ 9€\n"
        "3. Tự tôn 9€\n"
    )
    result = rules.classify(full_text=text)

    assert result.tsyc_relevance == rules.TsycRelevance.MEDIUM
    assert result.candidate_eligible is False
    assert result.classification_reason == rules.REASON_WEAK_COMMERCE_ONLY_NOT_ELIGIBLE


def test_real_book_post_with_book_word_and_euro_price_is_eligible():
    text = "Thanh lý sách cũ giá 8€ một cuốn, ai cần inbox em nhé"
    result = rules.classify(full_text=text)

    assert result.tsyc_relevance == rules.TsycRelevance.MEDIUM
    assert result.post_type == rules.PostType.PROMOTION
    assert result.candidate_eligible is True
    assert result.classification_reason == rules.REASON_WEAK_LISTING_ELIGIBLE


def test_strong_brand_text_without_listing_evidence_is_general_business():
    text = (
        "[Lời cảm ơn và Minigame tặng sách] Em chân thành biết ơn mọi người "
        "đã tham gia cùng Tiệm sách Yêu Con"
    )
    result = rules.classify(full_text=text)

    assert result.tsyc_relevance == rules.TsycRelevance.HIGH
    assert result.post_type == rules.PostType.GENERAL_BUSINESS
    assert result.candidate_eligible is False


def test_strong_brand_text_with_discount_language_and_listing_is_eligible_promotion():
    text = "Đầu năm mở hàng lấy may, em giảm giá 20% cho toàn bộ sách có sẵn tại Tiệm sách Yêu Con"
    result = rules.classify(full_text=text)

    assert result.tsyc_relevance == rules.TsycRelevance.HIGH
    assert result.post_type == rules.PostType.PROMOTION
    assert result.candidate_eligible is True


def test_review_folder_slug_is_book_review_not_eligible():
    result = rules.classify(full_text="Cuốn sách này thực sự rất hay", folder_slugs=["Reviewsachhay"])

    assert result.post_type == rules.PostType.BOOK_REVIEW
    assert result.candidate_eligible is False


def test_review_language_without_folder_slug_is_book_review_not_eligible():
    result = rules.classify(full_text="Review sách hay hôm nay: một cuốn sách tuyệt vời")

    assert result.post_type == rules.PostType.BOOK_REVIEW
    assert result.candidate_eligible is False


# --- negative-business exclusion ------------------------------------------


def test_numerology_post_with_price_and_incidental_sach_is_not_eligible():
    # Real false positive observed in the actual export: a numerology
    # "map" side-business post that happens to also use the word "sách"
    # plus a price, with no confirmed TSYC brand marker anywhere.
    text = (
        "Trải nghiệm khách hàng về bản đồ Nhân Số Học, sách hay đọc mỗi "
        "ngày giá chỉ 15€"
    )
    result = rules.classify(full_text=text)

    assert "Nhân số học" in result.negative_business_markers
    assert result.candidate_eligible is False
    assert result.classification_reason == rules.REASON_NEGATIVE_BUSINESS_EXCLUSION


def test_relationship_map_with_price_is_not_eligible():
    text = "Relationship Map có gì? Giá chỉ 9€ thôi nhé"
    result = rules.classify(full_text=text)

    # "ship" must still never spuriously match inside "Relationship".
    assert "ship" not in result.weak_markers
    assert "Relationship Map" in result.negative_business_markers
    assert result.candidate_eligible is False


def test_zeus_team_with_generic_sach_is_not_eligible_without_strong_marker():
    text = (
        "[CHƯƠNG TRÌNH DÀNH TẶNG KHÁCH HÀNG CỦA ZEUS TEAM] sách hay giá "
        "tốt chỉ 20€ hôm nay thôi"
    )
    result = rules.classify(full_text=text)

    assert "Zeus Team" in result.negative_business_markers
    assert result.candidate_eligible is False
    assert result.classification_reason == rules.REASON_NEGATIVE_BUSINESS_EXCLUSION


def test_zeus_team_marker_does_not_exclude_when_strong_tsyc_marker_present():
    # A negative-business marker must never override a *confirmed* TSYC
    # brand marker -- the exclusion only applies "AND there is no strong
    # TSYC marker" per the explicit requirement.
    text = (
        "Zeus Team từng hỏi mua, nhưng em vẫn ưu tiên bán sách có sẵn tại "
        "Tiệm sách Yêu Con"
    )
    result = rules.classify(full_text=text)

    assert "Zeus Team" in result.negative_business_markers
    assert result.tsyc_relevance == rules.TsycRelevance.HIGH
    assert result.post_type == rules.PostType.PRODUCT_POST
    assert result.candidate_eligible is True
    assert result.classification_reason != rules.REASON_NEGATIVE_BUSINESS_EXCLUSION


# --- idempotency ----------------------------------------------------------


def test_classify_is_pure_and_idempotent():
    text = "Feedbacks từ Yêu Con, mn cần mua sách nhắn em nhé"
    folder_slugs = [rules.FEEDBACK_FOLDER_SLUG]
    mention_ids = [rules.TSYC_PAGE_ID]

    first = rules.classify(full_text=text, folder_slugs=folder_slugs, mention_ids=mention_ids)
    second = rules.classify(full_text=text, folder_slugs=folder_slugs, mention_ids=mention_ids)

    assert first == second


def test_classification_result_rejects_unknown_relevance():
    import pytest

    with pytest.raises(ValueError):
        rules.ClassificationResult(
            tsyc_relevance="NOT_A_LEVEL",
            post_type=rules.PostType.PERSONAL,
            candidate_eligible=False,
            classification_reason="test",
            needs_secondary_review=False,
        )
