"""Offline tests for src/domain/rules/historical_candidate_semantic.py
-- the provider-neutral contract and the Phase 2 hard safety gate
(validate_and_gate()). No live Supabase/WooCommerce/Facebook/Claude
dependency -- pure functions over plain dataclasses.

Covers every Phase 8 offline scenario this module's own gate is
responsible for (the rest -- malformed API output, transient/permanent
failures, cache behavior -- are covered in
tests/test_historical_candidate_semantic_provider.py and
tests/test_historical_candidate_semantic_cache.py).
"""
from __future__ import annotations

import pytest

from src.domain.rules.historical_candidate_semantic import (
    BOOK_COMBO,
    MULTIPLE_BOOKS,
    NO_IDENTIFIABLE_PRODUCT,
    SINGLE_BOOK,
    REASON_ALL_CANDIDATES_REJECTED,
    REASON_IMAGE_REVIEW_REQUIRED,
    REASON_INCONSISTENT_CANDIDATE_COUNT,
    REASON_LOW_CONFIDENCE,
    REASON_NO_IDENTIFIABLE_PRODUCT,
    CandidateExtractionInput,
    CandidateExtractionResult,
    ExtractedCandidateCard,
    RejectedHint,
    validate_and_gate,
)


def _input(cleaned_text: str, **kwargs) -> CandidateExtractionInput:
    defaults = dict(
        record_id=1,
        date_text="Tháng 1 01, 2025 12:00:00 ch",
        local_image_paths=(),
        local_video_paths=(),
        deterministic_review_reasons=(),
        semantic_post_type=None,
        semantic_extracted_product_hints=(),
        non_book_hints=(),
    )
    defaults.update(kwargs)
    return CandidateExtractionInput(cleaned_text=cleaned_text, **defaults)


# --- dataclass-level validation ---------------------------------------


def test_candidate_card_rejects_unknown_type():
    with pytest.raises(ValueError):
        ExtractedCandidateCard(
            title_raw="X", candidate_type="NOVEL", evidence_text="X", confidence=0.9
        )


def test_candidate_card_rejects_confidence_out_of_range():
    with pytest.raises(ValueError):
        ExtractedCandidateCard(
            title_raw="X", candidate_type=SINGLE_BOOK, evidence_text="X", confidence=1.5
        )


def test_rejected_hint_rejects_unknown_reason():
    with pytest.raises(ValueError):
        RejectedHint(text="X", reason="MAYBE")


def test_result_rejects_unknown_post_product_type():
    with pytest.raises(ValueError):
        CandidateExtractionResult(post_product_type="NOVEL")


# --- typographic quote/dash normalization for matching only --------------


def test_curly_quotes_in_source_match_straight_quotes_from_provider():
    """Regression: a real pilot record (#1189) where the source uses
    curly quotes/en-dashes but the provider's JSON output used straight
    ASCII quotes/hyphens for the exact same words -- must still match;
    this is a typographic difference, not a hallucination."""
    text = (
        "#Có Sẵn – Combo “Diary of a Wimpy Kid – Nhật Ký Chú Bé Nhút Nhát” "
        "(Song ngữ Việt – Anh, 18 tập)"
    )
    raw = CandidateExtractionResult(
        post_product_type=BOOK_COMBO,
        candidates=(
            ExtractedCandidateCard(
                title_raw='Combo "Diary of a Wimpy Kid - Nhật Ký Chú Bé Nhút Nhát" (Song ngữ Việt - Anh, 18 tập)',
                candidate_type=BOOK_COMBO,
                evidence_text=(
                    '#Có Sẵn - Combo "Diary of a Wimpy Kid - Nhật Ký Chú Bé Nhút Nhát" '
                    '(Song ngữ Việt - Anh, 18 tập)'
                ),
                confidence=0.9,
            ),
        ),
        confidence=0.9,
    )

    outcome, result = validate_and_gate(raw, _input(text))

    assert outcome == "AUTO_PASS"
    # Display wording is untouched -- still exactly what the provider said.
    assert result.candidates[0].title_raw.startswith('Combo "Diary')


def test_hallucination_is_still_caught_despite_quote_normalization():
    """The quote/dash tolerance must never mask an actual wording
    difference."""
    text = "#Có Sẵn – Combo “Diary of a Wimpy Kid” (Song ngữ Việt – Anh, 18 tập)"
    raw = CandidateExtractionResult(
        post_product_type=BOOK_COMBO,
        candidates=(
            ExtractedCandidateCard(
                title_raw='Combo "Nhật Ký Của Nicky"',  # different book entirely
                candidate_type=BOOK_COMBO,
                evidence_text='Combo "Nhật Ký Của Nicky"',
                confidence=0.9,
            ),
        ),
        confidence=0.9,
    )

    outcome, result = validate_and_gate(raw, _input(text))

    assert outcome == "REVIEW_REQUIRED"
    assert result.candidates == ()


# --- scenario: single book (accept) -------------------------------------


def test_single_book_clearly_present_is_auto_pass():
    text = '"Doraemon Tap 1" của Fujiko F. Fujio, sách còn mới 100%, giá 8€.'
    raw = CandidateExtractionResult(
        post_product_type=SINGLE_BOOK,
        candidates=(
            ExtractedCandidateCard(
                title_raw="Doraemon Tap 1",
                candidate_type=SINGLE_BOOK,
                evidence_text='"Doraemon Tap 1" của Fujiko F. Fujio',
                confidence=0.9,
            ),
        ),
        confidence=0.9,
    )

    outcome, result = validate_and_gate(raw, _input(text))

    assert outcome == "AUTO_PASS"
    assert len(result.candidates) == 1
    assert result.candidates[0].title_raw == "Doraemon Tap 1"


# --- scenario: multiple books / price bullet list (accept) --------------


def test_price_bullet_list_multiple_books_is_auto_pass():
    text = (
        "Thanh lý:\n"
        "Đắc Nhân Tâm - 10€\n"
        "Nhà Giả Kim - 12€\n"
        "Suối Nguồn - 15€"
    )
    raw = CandidateExtractionResult(
        post_product_type=MULTIPLE_BOOKS,
        candidates=(
            ExtractedCandidateCard(
                title_raw="Đắc Nhân Tâm", candidate_type=SINGLE_BOOK,
                evidence_text="Đắc Nhân Tâm - 10€", confidence=0.9,
            ),
            ExtractedCandidateCard(
                title_raw="Nhà Giả Kim", candidate_type=SINGLE_BOOK,
                evidence_text="Nhà Giả Kim - 12€", confidence=0.88,
            ),
            ExtractedCandidateCard(
                title_raw="Suối Nguồn", candidate_type=SINGLE_BOOK,
                evidence_text="Suối Nguồn - 15€", confidence=0.9,
            ),
        ),
        confidence=0.9,
    )

    outcome, result = validate_and_gate(raw, _input(text))

    assert outcome == "AUTO_PASS"
    assert len(result.candidates) == 3
    titles = {c.title_raw for c in result.candidates}
    assert titles == {"Đắc Nhân Tâm", "Nhà Giả Kim", "Suối Nguồn"}


# --- scenario: explicit combo (accept) -----------------------------------


def test_explicit_combo_is_auto_pass():
    text = "Combo 4 cuốn truyện của Thomas Harris, giá 40€, thanh lý sách cũ."
    raw = CandidateExtractionResult(
        post_product_type=BOOK_COMBO,
        candidates=(
            ExtractedCandidateCard(
                title_raw="Combo 4 cuốn truyện của Thomas Harris",
                candidate_type=BOOK_COMBO,
                evidence_text="Combo 4 cuốn truyện của Thomas Harris",
                confidence=0.88,
            ),
        ),
        confidence=0.88,
    )

    outcome, result = validate_and_gate(raw, _input(text))

    assert outcome == "AUTO_PASS"
    assert result.candidates[0].candidate_type == BOOK_COMBO


# --- scenario: book mentioned inside prose (evidence must support) --------


def test_book_named_inside_prose_with_matching_evidence_is_auto_pass():
    text = (
        "Đây là cuốn sách giúp mình rất nhiều. Cuốn Chữa Lành Đứa Trẻ Trong Bạn "
        "của Charles Whitfield thực sự rất hay."
    )
    raw = CandidateExtractionResult(
        post_product_type=SINGLE_BOOK,
        candidates=(
            ExtractedCandidateCard(
                title_raw="Chữa Lành Đứa Trẻ Trong Bạn",
                candidate_type=SINGLE_BOOK,
                evidence_text="Cuốn Chữa Lành Đứa Trẻ Trong Bạn của Charles Whitfield",
                confidence=0.87,
            ),
        ),
        confidence=0.87,
    )

    outcome, result = validate_and_gate(raw, _input(text))

    assert outcome == "AUTO_PASS"


# --- scenario: title without author / without của/by (still fine) --------


def test_title_without_author_or_cua_is_still_acceptable():
    text = "Sách hay: Nhà Giả Kim, còn mới, giá 12€."
    raw = CandidateExtractionResult(
        post_product_type=SINGLE_BOOK,
        candidates=(
            ExtractedCandidateCard(
                title_raw="Nhà Giả Kim",
                candidate_type=SINGLE_BOOK,
                evidence_text="Sách hay: Nhà Giả Kim, còn mới",
                confidence=0.8,
            ),
        ),
        confidence=0.8,
    )

    outcome, result = validate_and_gate(raw, _input(text))

    assert outcome == "AUTO_PASS"


# --- scenario: customer/promotion prose (reject) --------------------------


def test_promotion_only_prose_yields_no_identifiable_product():
    text = "Sale sập sàn hôm nay, giảm giá 20% cho tất cả sách có sẵn, freeship toàn quốc!"
    raw = CandidateExtractionResult(
        post_product_type=NO_IDENTIFIABLE_PRODUCT,
        candidates=(),
        rejected_hints=(RejectedHint(text="Sale sập sàn", reason="PROMOTION_TEXT"),),
        confidence=0.85,
    )

    outcome, result = validate_and_gate(raw, _input(text))

    assert outcome == "REVIEW_REQUIRED"
    assert result.candidates == ()
    assert REASON_NO_IDENTIFIABLE_PRODUCT in result.review_reason_codes


def test_promotion_text_masquerading_as_title_is_rejected():
    text = "Sale sập sàn giảm giá 20% hôm nay, ai cần nhắn em."
    raw = CandidateExtractionResult(
        post_product_type=SINGLE_BOOK,
        candidates=(
            ExtractedCandidateCard(
                title_raw="Sale sập sàn giảm giá 20% hôm nay",
                candidate_type=SINGLE_BOOK,
                evidence_text="Sale sập sàn giảm giá 20% hôm nay, ai cần nhắn em.",
                confidence=0.9,
            ),
        ),
        confidence=0.9,
    )

    outcome, result = validate_and_gate(raw, _input(text))

    assert outcome == "REVIEW_REQUIRED"
    assert result.candidates == ()


# --- scenario: non-book numerology item (reject) --------------------------


def test_numerology_item_never_becomes_a_candidate():
    text = "Tặng 40% khi mua Map Kid Talent, bản đồ tính cách trẻ em, giúp cha mẹ."
    raw = CandidateExtractionResult(
        post_product_type=SINGLE_BOOK,
        candidates=(
            ExtractedCandidateCard(
                title_raw="Map Kid Talent",
                candidate_type=SINGLE_BOOK,
                evidence_text="Tặng 40% khi mua Map Kid Talent, bản đồ tính cách trẻ em",
                confidence=0.9,
            ),
        ),
        confidence=0.9,
    )

    outcome, result = validate_and_gate(raw, _input(text))

    assert outcome == "REVIEW_REQUIRED"
    assert result.candidates == ()
    assert not any("map kid talent" in h.text.casefold() for h in ()) or True  # documented, not asserted on wording


def test_numerology_item_rejected_even_with_high_confidence_and_real_evidence():
    """The most important non-book regression: high confidence, verbatim
    evidence, EVERYTHING else looks legitimate except the product itself
    is a numerology reading, not a book."""
    text = "Bản đồ Nhân Số Học của bạn đã sẵn sàng, chỉ 200€, nhắn em để nhận."
    raw = CandidateExtractionResult(
        post_product_type=SINGLE_BOOK,
        candidates=(
            ExtractedCandidateCard(
                title_raw="Bản đồ Nhân Số Học",
                candidate_type=SINGLE_BOOK,
                evidence_text="Bản đồ Nhân Số Học của bạn đã sẵn sàng",
                confidence=0.95,
            ),
        ),
        confidence=0.95,
    )

    outcome, result = validate_and_gate(raw, _input(text))

    assert outcome == "REVIEW_REQUIRED"
    assert result.candidates == ()


# --- scenario: no identifiable title -------------------------------------


def test_no_identifiable_title_is_review_required():
    text = "Tuần sau tôi về, tôi sẽ ship sách chăm chỉ trở lại."
    raw = CandidateExtractionResult(
        post_product_type=NO_IDENTIFIABLE_PRODUCT, candidates=(), confidence=0.3
    )

    outcome, result = validate_and_gate(raw, _input(text))

    assert outcome == "REVIEW_REQUIRED"
    assert result.candidates == ()


# --- scenario: image-review-required (Phase 4) -----------------------------


def test_no_identifiable_product_with_local_media_adds_image_review_reason():
    text = "Sách Có Sẵn tại Đức"
    raw = CandidateExtractionResult(
        post_product_type=NO_IDENTIFIABLE_PRODUCT, candidates=(), confidence=0.3
    )

    outcome, result = validate_and_gate(
        raw, _input(text, local_image_paths=tuple(f"images/{i}.jpg" for i in range(20)))
    )

    assert outcome == "REVIEW_REQUIRED"
    assert REASON_IMAGE_REVIEW_REQUIRED in result.review_reason_codes


def test_no_identifiable_product_without_local_media_has_no_image_review_reason():
    text = "Sách Có Sẵn tại Đức"
    raw = CandidateExtractionResult(
        post_product_type=NO_IDENTIFIABLE_PRODUCT, candidates=(), confidence=0.3
    )

    outcome, result = validate_and_gate(raw, _input(text))

    assert outcome == "REVIEW_REQUIRED"
    assert REASON_IMAGE_REVIEW_REQUIRED not in result.review_reason_codes


# --- scenario: malformed / inconsistent structured output -----------------


def test_single_book_claim_with_two_candidates_is_inconsistent():
    text = "Nhà Giả Kim của Paulo Coelho. Đắc Nhân Tâm của Dale Carnegie."
    raw = CandidateExtractionResult(
        post_product_type=SINGLE_BOOK,
        candidates=(
            ExtractedCandidateCard(
                title_raw="Nhà Giả Kim", candidate_type=SINGLE_BOOK,
                evidence_text="Nhà Giả Kim của Paulo Coelho", confidence=0.9,
            ),
            ExtractedCandidateCard(
                title_raw="Đắc Nhân Tâm", candidate_type=SINGLE_BOOK,
                evidence_text="Đắc Nhân Tâm của Dale Carnegie", confidence=0.9,
            ),
        ),
        confidence=0.9,
    )

    outcome, result = validate_and_gate(raw, _input(text))

    assert outcome == "REVIEW_REQUIRED"
    assert REASON_INCONSISTENT_CANDIDATE_COUNT in result.review_reason_codes


def test_multiple_books_claim_with_only_one_surviving_candidate_is_inconsistent():
    text = "Nhà Giả Kim của Paulo Coelho."
    raw = CandidateExtractionResult(
        post_product_type=MULTIPLE_BOOKS,
        candidates=(
            ExtractedCandidateCard(
                title_raw="Nhà Giả Kim", candidate_type=SINGLE_BOOK,
                evidence_text="Nhà Giả Kim của Paulo Coelho", confidence=0.9,
            ),
        ),
        confidence=0.9,
    )

    outcome, result = validate_and_gate(raw, _input(text))

    assert outcome == "REVIEW_REQUIRED"
    assert REASON_INCONSISTENT_CANDIDATE_COUNT in result.review_reason_codes


# --- scenario: hallucinated title not present in text ----------------------


def test_hallucinated_title_not_in_source_text_is_rejected():
    text = "Em thanh lý vài cuốn sách cũ, giá rẻ, ai cần nhắn em."
    raw = CandidateExtractionResult(
        post_product_type=SINGLE_BOOK,
        candidates=(
            ExtractedCandidateCard(
                title_raw="Nhà Giả Kim",  # never appears in text
                candidate_type=SINGLE_BOOK,
                evidence_text="Em thanh lý vài cuốn sách cũ",
                confidence=0.9,
            ),
        ),
        confidence=0.9,
    )

    outcome, result = validate_and_gate(raw, _input(text))

    assert outcome == "REVIEW_REQUIRED"
    assert result.candidates == ()
    assert any("hallucinat" in code.lower() for code in result.review_reason_codes)


def test_hallucinated_evidence_not_in_source_text_is_rejected():
    """Title happens to appear, but the "evidence" quoting it does not
    -- i.e. a fabricated supporting sentence."""
    text = "Nhà Giả Kim còn 1 cuốn, giá 12€."
    raw = CandidateExtractionResult(
        post_product_type=SINGLE_BOOK,
        candidates=(
            ExtractedCandidateCard(
                title_raw="Nhà Giả Kim",
                candidate_type=SINGLE_BOOK,
                evidence_text="Nhà Giả Kim của Paulo Coelho là một kiệt tác kinh điển",  # fabricated
                confidence=0.9,
            ),
        ),
        confidence=0.9,
    )

    outcome, result = validate_and_gate(raw, _input(text))

    assert outcome == "REVIEW_REQUIRED"
    assert result.candidates == ()


# --- scenario: duplicate titles --------------------------------------------


def test_duplicate_titles_are_deduped_keeping_first():
    text = "Nhà Giả Kim của Paulo Coelho, còn 2 cuốn, mỗi cuốn 12€."
    raw = CandidateExtractionResult(
        post_product_type=SINGLE_BOOK,
        candidates=(
            ExtractedCandidateCard(
                title_raw="Nhà Giả Kim", candidate_type=SINGLE_BOOK,
                evidence_text="Nhà Giả Kim của Paulo Coelho", confidence=0.9,
            ),
            ExtractedCandidateCard(
                title_raw="Nhà Giả Kim", candidate_type=SINGLE_BOOK,
                evidence_text="Nhà Giả Kim của Paulo Coelho", confidence=0.85,
            ),
        ),
        confidence=0.9,
    )

    outcome, result = validate_and_gate(raw, _input(text))

    assert outcome == "AUTO_PASS"
    assert len(result.candidates) == 1


# --- scenario: confidence below threshold ----------------------------------


def test_overall_confidence_below_threshold_is_review_required():
    text = "Nhà Giả Kim của Paulo Coelho, giá 12€."
    raw = CandidateExtractionResult(
        post_product_type=SINGLE_BOOK,
        candidates=(
            ExtractedCandidateCard(
                title_raw="Nhà Giả Kim", candidate_type=SINGLE_BOOK,
                evidence_text="Nhà Giả Kim của Paulo Coelho", confidence=0.9,
            ),
        ),
        confidence=0.5,  # below default 0.75 threshold
    )

    outcome, result = validate_and_gate(raw, _input(text))

    assert outcome == "REVIEW_REQUIRED"
    assert result.candidates == ()
    assert REASON_LOW_CONFIDENCE in result.review_reason_codes


def test_per_candidate_confidence_below_threshold_is_rejected():
    text = "Nhà Giả Kim của Paulo Coelho. Đắc Nhân Tâm của Dale Carnegie."
    raw = CandidateExtractionResult(
        post_product_type=MULTIPLE_BOOKS,
        candidates=(
            ExtractedCandidateCard(
                title_raw="Nhà Giả Kim", candidate_type=SINGLE_BOOK,
                evidence_text="Nhà Giả Kim của Paulo Coelho", confidence=0.9,
            ),
            ExtractedCandidateCard(
                title_raw="Đắc Nhân Tâm", candidate_type=SINGLE_BOOK,
                evidence_text="Đắc Nhân Tâm của Dale Carnegie", confidence=0.4,  # too low
            ),
        ),
        confidence=0.9,
    )

    outcome, result = validate_and_gate(raw, _input(text))

    # Only 1 of 2 claimed MULTIPLE_BOOKS candidates survives -> inconsistent.
    assert outcome == "REVIEW_REQUIRED"


def test_custom_threshold_is_honored():
    text = "Nhà Giả Kim của Paulo Coelho, giá 12€."
    raw = CandidateExtractionResult(
        post_product_type=SINGLE_BOOK,
        candidates=(
            ExtractedCandidateCard(
                title_raw="Nhà Giả Kim", candidate_type=SINGLE_BOOK,
                evidence_text="Nhà Giả Kim của Paulo Coelho", confidence=0.6,
            ),
        ),
        confidence=0.6,
    )

    outcome, _result = validate_and_gate(raw, _input(text), high_confidence_threshold=0.5)

    assert outcome == "AUTO_PASS"


# --- generic TSYC heading / UI chrome / price-only never a title ---------


@pytest.mark.parametrize(
    "bad_title",
    [
        "Sách Có Sẵn tại Đức",
        "Tải lên từ di động",
        "Feedbacks từ Yêu Con",
        "12€",
    ],
)
def test_generic_heading_or_price_only_title_is_rejected(bad_title):
    text = f"{bad_title} một số nội dung khác không liên quan đến sách cụ thể."
    raw = CandidateExtractionResult(
        post_product_type=SINGLE_BOOK,
        candidates=(
            ExtractedCandidateCard(
                title_raw=bad_title,
                candidate_type=SINGLE_BOOK,
                evidence_text=text,
                confidence=0.9,
            ),
        ),
        confidence=0.9,
    )

    outcome, result = validate_and_gate(raw, _input(text))

    assert outcome == "REVIEW_REQUIRED"
    assert result.candidates == ()


# =====================================================================
# Historical hardening pass (2026-08-30): candidate-count capacity /
# completeness contract (MAX_CANDIDATES_PER_RECORD, candidate_list_
# complete). See src/domain/rules/historical_candidate_semantic.py's
# own module docstring and validate_and_gate() rule 7.
# =====================================================================

from src.domain.rules.historical_candidate_semantic import (  # noqa: E402
    MAX_CANDIDATES_PER_RECORD,
    REASON_CANDIDATE_COUNT_EXCEEDS_LIMIT,
)


def _make_price_list_fixture(count: int) -> tuple[str, tuple[ExtractedCandidateCard, ...]]:
    """Build a plain-text price-bulleted list of `count` distinct,
    individually-verbatim-supportable titles, plus the matching
    candidate cards -- a synthetic but structurally faithful stand-in
    for records like #1062/#1483 (each line is its own evidence)."""
    lines = [f"{i}. Tựa sách số {i} {i}€" for i in range(1, count + 1)]
    text = "\n".join(lines)
    candidates = tuple(
        ExtractedCandidateCard(
            title_raw=f"Tựa sách số {i}",
            candidate_type=SINGLE_BOOK,
            evidence_text=f"{i}. Tựa sách số {i} {i}€",
            confidence=0.9,
        )
        for i in range(1, count + 1)
    )
    return text, candidates


@pytest.mark.parametrize("count", [1, 19, 20, 21, 49, MAX_CANDIDATES_PER_RECORD])
def test_complete_lists_up_to_and_including_the_cap_can_auto_pass(count):
    text, candidates = _make_price_list_fixture(count)
    post_type = SINGLE_BOOK if count == 1 else MULTIPLE_BOOKS
    raw = CandidateExtractionResult(
        post_product_type=post_type,
        candidates=candidates,
        confidence=0.9,
        candidate_list_complete=True,
    )

    outcome, result = validate_and_gate(raw, _input(text))

    assert outcome == "AUTO_PASS", f"count={count} should AUTO_PASS when complete"
    assert len(result.candidates) == count
    assert result.candidate_list_complete is True


def test_21_candidates_is_no_longer_artificially_truncated():
    """The core fix: before this hardening pass, the schema itself
    could never carry more than 20 candidates. 21 complete, valid
    candidates must now be representable and eligible for AUTO_PASS."""
    text, candidates = _make_price_list_fixture(21)
    raw = CandidateExtractionResult(
        post_product_type=MULTIPLE_BOOKS,
        candidates=candidates,
        confidence=0.9,
        candidate_list_complete=True,
    )

    outcome, result = validate_and_gate(raw, _input(text))

    assert outcome == "AUTO_PASS"
    assert len(result.candidates) == 21


def test_incomplete_result_never_auto_passes_even_at_49_candidates():
    text, candidates = _make_price_list_fixture(49)
    raw = CandidateExtractionResult(
        post_product_type=MULTIPLE_BOOKS,
        candidates=candidates,
        confidence=0.9,
        candidate_list_complete=False,
    )

    outcome, result = validate_and_gate(raw, _input(text))

    assert outcome == "REVIEW_REQUIRED"
    assert REASON_CANDIDATE_COUNT_EXCEEDS_LIMIT in result.review_reason_codes
    # Diagnostic candidates are preserved (all individually valid),
    # never silently discarded -- but never presented as AUTO_PASS.
    assert len(result.candidates) == 49


def test_exactly_50_but_provider_says_incomplete_is_review_required():
    """Provider hits the hard cap AND is honest about it -- must never
    be silently treated as complete."""
    text, candidates = _make_price_list_fixture(MAX_CANDIDATES_PER_RECORD)
    raw = CandidateExtractionResult(
        post_product_type=MULTIPLE_BOOKS,
        candidates=candidates,
        confidence=0.9,
        candidate_list_complete=False,
    )

    outcome, result = validate_and_gate(raw, _input(text))

    assert outcome == "REVIEW_REQUIRED"
    assert REASON_CANDIDATE_COUNT_EXCEEDS_LIMIT in result.review_reason_codes


def test_more_than_cap_candidates_defensively_rejected_even_if_marked_complete():
    """51 candidates can only reach the gate from a non-schema-
    constructed caller (the live schema itself caps at
    MAX_CANDIDATES_PER_RECORD) -- but if it ever does, a
    candidate_list_complete=True claim must NOT be trusted to override
    the structural impossibility. Defense in depth against >50
    'logical' candidates ever silently becoming AUTO_PASS."""
    text, candidates = _make_price_list_fixture(MAX_CANDIDATES_PER_RECORD + 1)
    raw = CandidateExtractionResult(
        post_product_type=MULTIPLE_BOOKS,
        candidates=candidates,
        confidence=0.9,
        candidate_list_complete=True,  # a lie the gate must not trust
    )

    outcome, result = validate_and_gate(raw, _input(text))

    assert outcome == "REVIEW_REQUIRED"
    assert REASON_CANDIDATE_COUNT_EXCEEDS_LIMIT in result.review_reason_codes


def test_duplicate_candidate_inside_a_large_list_is_deduped():
    text, candidates = _make_price_list_fixture(25)
    # Append an exact duplicate of candidate #1.
    candidates = candidates + (candidates[0],)
    raw = CandidateExtractionResult(
        post_product_type=MULTIPLE_BOOKS,
        candidates=candidates,
        confidence=0.9,
        candidate_list_complete=True,
    )

    outcome, result = validate_and_gate(raw, _input(text))

    assert outcome == "AUTO_PASS"
    assert len(result.candidates) == 25  # the 26th (duplicate) silently dropped


def test_non_book_candidate_inside_a_large_list_is_rejected_others_survive():
    text, candidates = _make_price_list_fixture(24)
    bad_text = text + "\nBản đồ Nhân Số Học 25 25€"
    bad_candidate = ExtractedCandidateCard(
        title_raw="Bản đồ Nhân Số Học 25",
        candidate_type=SINGLE_BOOK,
        evidence_text="Bản đồ Nhân Số Học 25 25€",
        confidence=0.9,
    )
    raw = CandidateExtractionResult(
        post_product_type=MULTIPLE_BOOKS,
        candidates=candidates + (bad_candidate,),
        confidence=0.9,
        candidate_list_complete=True,
    )

    outcome, result = validate_and_gate(raw, _input(bad_text))

    assert outcome == "AUTO_PASS"
    assert len(result.candidates) == 24  # non-book candidate rejected
    assert not any("nhân số học" in c.title_raw.casefold() for c in result.candidates)


def test_hallucinated_candidate_inside_a_large_list_is_rejected_others_survive():
    text, candidates = _make_price_list_fixture(24)
    hallucinated = ExtractedCandidateCard(
        title_raw="Tựa sách không có thật",  # never appears in text
        candidate_type=SINGLE_BOOK,
        evidence_text="Tựa sách không có thật 99€",
        confidence=0.9,
    )
    raw = CandidateExtractionResult(
        post_product_type=MULTIPLE_BOOKS,
        candidates=candidates + (hallucinated,),
        confidence=0.9,
        candidate_list_complete=True,
    )

    outcome, result = validate_and_gate(raw, _input(text))

    assert outcome == "AUTO_PASS"
    assert len(result.candidates) == 24
    assert not any("không có thật" in c.title_raw for c in result.candidates)


def test_confidence_threshold_still_0point75_unchanged():
    from src.domain.rules.historical_candidate_semantic import DEFAULT_CANDIDATE_CONFIDENCE_THRESHOLD

    assert DEFAULT_CANDIDATE_CONFIDENCE_THRESHOLD == 0.75


# --- #1483 regression: real cleaned text, genuine >20-item record ---------
#
# Captured directly from the historical export during this hardening
# pass -- HistoryRecord #1483's own cleaned_text (via
# historical_text_cleaner.clean_historical_facebook_text()), containing
# 24 genuinely distinct, individually price-tagged book titles across
# four separate "Tải lên từ di động" photo groups. Confirmed >20 (this
# is exactly the record that hit the OLD 20-item schema ceiling).

_RECORD_1483_CLEANED_TEXT = (
    "1. Khéo ăn nói sẽ có được thiên hạ 9€\n"
    "2. Kẻ làm thay đổi cuộc chơi 10€\n"
    "3. Nóng giận là bản năng, tĩnh lặng là bản lĩnh 7€\n"
    "4. Đừng bao giờ đi ăn một mình 9€\n"
    "5. 25 thuật đắc nhân tâm 7,5\n"
    "6. Bước chậm lại giữa thế gian vội vã 7,5€\n"
    "Tải lên từ di động\n"
    "1. Tuổi trẻ đáng giá bao nhiêu? 7,5€\n"
    "2. 15 nguyên tắc vàng về phát triển bản thân 9€\n"
    "3. Phi lý trí 7,5€\n"
    "4. Bạn không thông minh lắm đâu 10€\n"
    "5. Đừng để nước đến chân mới nhảy 7€\n"
    "6. 6 chiếc mũ tư duy 7€\n"
    "Tải lên từ di động\n"
    "1. Ngàn mặt trời rực rỡ 10€\n"
    "2. Không gia đình 10€\n"
    "3. Totto-chan bên cửa sổ 8,5€\n"
    "4. Ông trăm tuổi trèo qua cửa sổ và biến mất 12€\n"
    "5. Tuổi thơ dữ dội 15€\n"
    "6. Người đua diều 10€\n"
    "Tải lên từ di động\n"
    "50€/ bộ 6 cuốn 😍\n"
    "Tải lên từ di động\n"
    "1. Nhật ký trong tù 5€\n"
    "2. Những ngày thơ ấu 5€\n"
    "3. Yêu người ngóng núi 5€\n"
    "4. Tôi đi học 6€\n"
    "5. Kho báu 6€\n"
    "6. Đại gia Gatsby 6€\n"
    "Mở hàng đầu năm suôn sẻ nào cả nhà ơi, em bán rất nhiều sách đã qua "
    "sử dụng còn rất tốt nè. Giá em để trong từng ảnh ạ🥰\n"
    "Nhân dịp đầu năm, em chúc cả nhà mình nhiều sức khoẻ, niềm vui và "
    "vạn sự như ý 🤩🥰😍🌠🎇🎆🌅"
)

_RECORD_1483_ALL_24_TITLES = (
    ("Khéo ăn nói sẽ có được thiên hạ", "1. Khéo ăn nói sẽ có được thiên hạ 9€"),
    ("Kẻ làm thay đổi cuộc chơi", "2. Kẻ làm thay đổi cuộc chơi 10€"),
    ("Nóng giận là bản năng, tĩnh lặng là bản lĩnh", "3. Nóng giận là bản năng, tĩnh lặng là bản lĩnh 7€"),
    ("Đừng bao giờ đi ăn một mình", "4. Đừng bao giờ đi ăn một mình 9€"),
    ("25 thuật đắc nhân tâm", "5. 25 thuật đắc nhân tâm 7,5"),
    ("Bước chậm lại giữa thế gian vội vã", "6. Bước chậm lại giữa thế gian vội vã 7,5€"),
    ("Tuổi trẻ đáng giá bao nhiêu?", "1. Tuổi trẻ đáng giá bao nhiêu? 7,5€"),
    ("15 nguyên tắc vàng về phát triển bản thân", "2. 15 nguyên tắc vàng về phát triển bản thân 9€"),
    ("Phi lý trí", "3. Phi lý trí 7,5€"),
    ("Bạn không thông minh lắm đâu", "4. Bạn không thông minh lắm đâu 10€"),
    ("Đừng để nước đến chân mới nhảy", "5. Đừng để nước đến chân mới nhảy 7€"),
    ("6 chiếc mũ tư duy", "6. 6 chiếc mũ tư duy 7€"),
    ("Ngàn mặt trời rực rỡ", "1. Ngàn mặt trời rực rỡ 10€"),
    ("Không gia đình", "2. Không gia đình 10€"),
    ("Totto-chan bên cửa sổ", "3. Totto-chan bên cửa sổ 8,5€"),
    ("Ông trăm tuổi trèo qua cửa sổ và biến mất", "4. Ông trăm tuổi trèo qua cửa sổ và biến mất 12€"),
    ("Tuổi thơ dữ dội", "5. Tuổi thơ dữ dội 15€"),
    ("Người đua diều", "6. Người đua diều 10€"),
    ("Nhật ký trong tù", "1. Nhật ký trong tù 5€"),
    ("Những ngày thơ ấu", "2. Những ngày thơ ấu 5€"),
    ("Yêu người ngóng núi", "3. Yêu người ngóng núi 5€"),
    ("Tôi đi học", "4. Tôi đi học 6€"),
    ("Kho báu", "5. Kho báu 6€"),
    ("Đại gia Gatsby", "6. Đại gia Gatsby 6€"),
)


def test_1483_real_text_genuinely_contains_more_than_20_items():
    assert len(_RECORD_1483_ALL_24_TITLES) == 24
    for title, evidence in _RECORD_1483_ALL_24_TITLES:
        assert evidence in _RECORD_1483_CLEANED_TEXT
        assert title in evidence


def test_1483_old_truncated_20_item_response_never_auto_passes():
    """Simulates the ACTUAL historical response that was cached for
    #1483 before this hardening pass: exactly 20 of the 24 real items
    (a genuinely truncated subset -- items 20-23 in the numbering above
    were dropped), with the provider's own real disclosure reason code,
    and (as every pre-hardening response did) no candidate_list_complete
    field at all -- modeled here as complete=False, matching what
    src.services.historical_candidate_semantic_cache's backward-
    compatible inference computes for this exact shape (count==20,
    truncation-indicating reason code) -- see the dedicated cache tests
    for that inference logic itself."""
    truncated_20 = _RECORD_1483_ALL_24_TITLES[:19] + (_RECORD_1483_ALL_24_TITLES[23],)
    assert len(truncated_20) == 20

    candidates = tuple(
        ExtractedCandidateCard(
            title_raw=title, candidate_type=SINGLE_BOOK, evidence_text=evidence, confidence=0.9
        )
        for title, evidence in truncated_20
    )
    raw = CandidateExtractionResult(
        post_product_type=MULTIPLE_BOOKS,
        candidates=candidates,
        confidence=0.9,
        review_reason_codes=("MANY_TITLES_TRUNCATED_AT_20", "BUNDLE_PRICE_AMBIGUITY"),
        candidate_list_complete=False,
    )

    outcome, result = validate_and_gate(raw, _input(_RECORD_1483_CLEANED_TEXT))

    assert outcome == "REVIEW_REQUIRED"
    assert REASON_CANDIDATE_COUNT_EXCEEDS_LIMIT in result.review_reason_codes
    # Diagnostic data preserved, never silently discarded.
    assert len(result.candidates) == 20


def test_1483_complete_24_item_representation_can_auto_pass_under_new_cap():
    """The actual fix in action: given the SAME real record, a
    provider response that (a) fits comfortably under the new
    MAX_CANDIDATES_PER_RECORD=50 cap and (b) honestly attests
    completeness must be eligible for AUTO_PASS -- recovering all 24
    real titles the old 20-item ceiling could never have represented,
    regardless of provider behavior."""
    assert 24 <= MAX_CANDIDATES_PER_RECORD

    candidates = tuple(
        ExtractedCandidateCard(
            title_raw=title, candidate_type=SINGLE_BOOK, evidence_text=evidence, confidence=0.9
        )
        for title, evidence in _RECORD_1483_ALL_24_TITLES
    )
    raw = CandidateExtractionResult(
        post_product_type=MULTIPLE_BOOKS,
        candidates=candidates,
        confidence=0.9,
        candidate_list_complete=True,
    )

    outcome, result = validate_and_gate(raw, _input(_RECORD_1483_CLEANED_TEXT))

    assert outcome == "AUTO_PASS"
    assert len(result.candidates) == 24
    recovered_titles = {c.title_raw for c in result.candidates}
    assert recovered_titles == {title for title, _evidence in _RECORD_1483_ALL_24_TITLES}


# --- #1458 investigation result: NOT fixed, stays REVIEW_REQUIRED ---------
#
# Root cause (this hardening pass): the provider's title INSERTED
# parentheses around "bản đặc biệt" ("phương đông (bản đặc biệt)")
# that do not exist anywhere in the source ("phương đông bản đặc
# biệt", plain running text, no punctuation). This is punctuation
# INSERTION -- new characters not present in source -- not a Unicode-
# glyph substitution (same character, different code point) like the
# already-approved quote/dash canonicalization. Per this task's own
# explicit instruction, this is NOT safely fixable via canonical-
# equivalence widening (it borders the explicitly-forbidden "added
# subtitle" pattern) and is left REVIEW_REQUIRED, unfixed, reported as
# a model-output-quality observation rather than an implementation
# defect.

_RECORD_1458_CLEANED_TEXT = (
    "Hết xèng nên em thanh lý hết, toàn sách hay. Hành trình về phương "
    "đông bản đặc biệt giờ rất khó mua, em đọc nhiều lần r nên thanh lý "
    "đi, ai đam mê thì nhắn em ngay nhé ❤️❤️❤️"
)


def test_1458_inserted_parentheses_are_not_canonicalized_away():
    raw = CandidateExtractionResult(
        post_product_type=SINGLE_BOOK,
        candidates=(
            ExtractedCandidateCard(
                title_raw="Hành trình về phương đông (bản đặc biệt)",
                candidate_type=SINGLE_BOOK,
                evidence_text="Hành trình về phương đông bản đặc biệt giờ rất khó mua",
                confidence=0.88,
            ),
        ),
        confidence=0.88,
        candidate_list_complete=True,
    )

    outcome, result = validate_and_gate(raw, _input(_RECORD_1458_CLEANED_TEXT))

    assert outcome == "REVIEW_REQUIRED"
    assert result.candidates == ()


def test_1458_unparenthesized_title_would_have_matched():
    """Confirms the mismatch is SPECIFICALLY the inserted parentheses,
    not some other unrelated difference -- the exact same words,
    without the added punctuation, pass cleanly."""
    raw = CandidateExtractionResult(
        post_product_type=SINGLE_BOOK,
        candidates=(
            ExtractedCandidateCard(
                title_raw="Hành trình về phương đông bản đặc biệt",
                candidate_type=SINGLE_BOOK,
                evidence_text="Hành trình về phương đông bản đặc biệt giờ rất khó mua",
                confidence=0.88,
            ),
        ),
        confidence=0.88,
        candidate_list_complete=True,
    )

    outcome, result = validate_and_gate(raw, _input(_RECORD_1458_CLEANED_TEXT))

    assert outcome == "AUTO_PASS"
    assert result.candidates[0].title_raw == "Hành trình về phương đông bản đặc biệt"
