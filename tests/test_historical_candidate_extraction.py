"""Offline tests for src/domain/rules/historical_candidate_extraction.py.

No live Supabase/WooCommerce/Facebook/Claude dependency -- pure
functions operating on plain strings/tuples. Covers the OFFLINE
historical candidate-extraction preview contract built on top of the
already-tested src/domain/rules/extraction_rules.py engine (see
tests/test_extraction_rules.py for that engine's own coverage; this
file does not re-test its regex patterns, only the historical-preview
adapter layer wrapped around it).
"""
from __future__ import annotations

from src.domain.decisions import Outcome
from src.domain.rules.historical_candidate_extraction import (
    HistoricalExtractionInput,
    extract_historical_candidates,
)


def _make_input(
    record_id: int = 1,
    full_text: str = "",
    semantic_extracted_product_hints: tuple[str, ...] = (),
    deterministic_post_type: str = "PROMOTION",
    deterministic_candidate_eligible: bool = True,
    semantic_post_type: str | None = "PRODUCT_POST",
    decision_source: str = "SEMANTIC",
    local_image_paths: tuple[str, ...] = (),
    local_video_paths: tuple[str, ...] = (),
) -> HistoricalExtractionInput:
    return HistoricalExtractionInput(
        record_id=record_id,
        date_text="Tháng 1 01, 2025 12:00:00 ch",
        full_text=full_text,
        deterministic_post_type=deterministic_post_type,
        deterministic_candidate_eligible=deterministic_candidate_eligible,
        semantic_post_type=semantic_post_type,
        decision_source=decision_source,
        semantic_extracted_product_hints=semantic_extracted_product_hints,
        local_image_paths=local_image_paths,
        local_video_paths=local_video_paths,
    )


# --- clear single-book listing --------------------------------------------


def test_clear_single_book_listing_produces_one_candidate():
    text = "“Doraemon Tap 1” của Fujiko F. Fujio, sách còn mới 100%, giá 8€."

    result = extract_historical_candidates(_make_input(full_text=text))

    assert result.extraction_outcome == Outcome.AUTO_PASS
    assert result.post_product_type == "SINGLE_BOOK"
    assert len(result.candidates) == 1
    candidate = result.candidates[0]
    assert candidate.title_raw == "Doraemon Tap 1"
    assert candidate.candidate_type == "SINGLE_BOOK"
    assert 0.0 <= candidate.confidence <= 1.0
    assert "pattern=" in candidate.source_evidence


# --- clear multi-book listing ----------------------------------------------


def test_clear_multi_book_listing_produces_distinct_candidates():
    text = (
        "Sách có sẵn:\n"
        "1. “Doraemon Tap 1” của Fujiko F. Fujio\n"
        "2. “Conan Tap 5” của Gosho Aoyama\n"
        "3. “Thám Tử Lừng Danh Conan Tap 6” của Gosho Aoyama\n"
    )

    result = extract_historical_candidates(_make_input(full_text=text))

    assert result.extraction_outcome == Outcome.AUTO_PASS
    assert result.post_product_type == "MULTIPLE_BOOKS"
    assert len(result.candidates) == 3
    titles = {candidate.title_raw for candidate in result.candidates}
    assert titles == {
        "Doraemon Tap 1",
        "Conan Tap 5",
        "Thám Tử Lừng Danh Conan Tap 6",
    }
    assert all(candidate.candidate_type == "SINGLE_BOOK" for candidate in result.candidates)


# --- explicit combo ---------------------------------------------------------


def test_explicit_combo_produces_one_book_combo_candidate():
    text = "Combo 14 cuốn Gieo hạt cùng vĩ nhân là một bộ sách ý nghĩa cho bé trên 6 tuổi, giá 42€."

    result = extract_historical_candidates(_make_input(full_text=text))

    assert result.extraction_outcome == Outcome.AUTO_PASS
    assert result.post_product_type == "BOOK_COMBO"
    assert len(result.candidates) == 1
    assert result.candidates[0].candidate_type == "BOOK_COMBO"


# --- no identifiable title => REVIEW_REQUIRED + 0 candidates --------------


def test_book_signal_without_reliable_title_is_review_required_with_zero_candidates():
    text = "Sách hôm nay siêu hời cả nhà ơi, ai cần nhắn em nha, hết là hết luôn đó!"

    result = extract_historical_candidates(_make_input(full_text=text))

    assert result.extraction_outcome == Outcome.REVIEW_REQUIRED
    assert result.candidates == ()
    assert result.review_reasons != ()


# --- #774 / #1287-style: strong-include record with no text title --------


def test_generic_website_announcement_like_774_is_review_required():
    text = (
        "📚 Tiệm Sách Yêu Con đã có website rồi cả nhà ơi! Sau nhiều năm bán "
        "sách, giới thiệu sách và vận hành Thư Viện Sách Cộng Đồng tại Đức, "
        "cuối cùng mình cũng hoàn thành một điều đã ấp ủ từ rất lâu: xây "
        "dựng website riêng cho Tiệm sách Yêu Con ở Đức. Mục đích rất đơn "
        "giản thôi: để mọi người tìm sách, đặt sách, mượn sách hoặc thuê "
        "sách thuận tiện hơn."
    )

    result = extract_historical_candidates(
        _make_input(
            full_text=text,
            deterministic_post_type="PROMOTION",
            decision_source="DETERMINISTIC_STRONG",
            semantic_post_type=None,
            local_image_paths=("images/1.jpg",),
        )
    )

    assert result.extraction_outcome == Outcome.REVIEW_REQUIRED
    assert result.post_product_type == "UNKNOWN"
    assert result.candidates == ()


def test_bare_generic_phrase_like_1287_is_review_required_with_zero_candidates():
    text = "Sách Có Sẵn tại Đức"

    result = extract_historical_candidates(
        _make_input(
            full_text=text,
            decision_source="DETERMINISTIC_STRONG",
            semantic_post_type=None,
            # The real record #1287 is a 20-photo album post with no
            # other caption text -- attached media is why this defers
            # to REVIEW_REQUIRED rather than an automatic AUTO_REJECT.
            local_image_paths=tuple(f"images/{i}.jpg" for i in range(20)),
        )
    )

    assert result.extraction_outcome == Outcome.REVIEW_REQUIRED
    assert result.candidates == ()


def test_bare_generic_phrase_with_no_media_at_all_is_auto_reject():
    """Without any attached media, a text reduced to pure boilerplate
    genuinely has nothing to review -- AUTO_REJECT is the correct,
    confident outcome (still zero candidates, still safe)."""
    text = "Sách Có Sẵn tại Đức"

    result = extract_historical_candidates(
        _make_input(
            full_text=text,
            decision_source="DETERMINISTIC_STRONG",
            semantic_post_type=None,
        )
    )

    assert result.extraction_outcome == Outcome.AUTO_REJECT
    assert result.candidates == ()


# --- #1455-style: real book plus a non-book bonus hint --------------------


def test_book_kept_while_unrelated_bonus_hint_is_excluded_as_non_book():
    text = (
        "“Doraemon Tap 1” của Fujiko F. Fujio, giảm giá 15%. Tặng kèm 30 "
        "phút luận giải bản đồ tính cách trẻ em khi mua Map Kid Talent "
        "giảm 40%."
    )

    result = extract_historical_candidates(
        _make_input(
            full_text=text,
            semantic_extracted_product_hints=(
                "Doraemon Tap 1",
                "Map Kid Talent giảm 40%",
            ),
        )
    )

    assert len(result.candidates) == 1
    assert result.candidates[0].title_raw == "Doraemon Tap 1"
    # The confirmed hint (matches the extracted title) is not reported as
    # an unconfirmed non-book hint...
    assert "Doraemon Tap 1" not in result.non_book_hints
    # ...but the unrelated bonus item never becomes a candidate and is
    # reported as an unconfirmed hint instead.
    assert "Map Kid Talent giảm 40%" in result.non_book_hints
    assert all(
        candidate.title_raw != "Map Kid Talent giảm 40%" for candidate in result.candidates
    )
    assert not any("map kid talent" in c.title_raw.casefold() for c in result.candidates)


def test_all_hints_are_non_book_when_zero_candidates_extracted():
    text = "Giải cứu sách phần 2, giảm tất cả 15%, freeship cho đơn từ 50€."

    result = extract_historical_candidates(
        _make_input(
            full_text=text,
            semantic_extracted_product_hints=(
                "Giải cứu sách phần 2",
                "Map Kid Talent giảm 40%",
            ),
        )
    )

    assert result.candidates == ()
    assert set(result.non_book_hints) == {
        "Giải cứu sách phần 2",
        "Map Kid Talent giảm 40%",
    }


# --- TSYC business marker never becomes a title ----------------------------


def test_tsyc_business_marker_alone_never_becomes_a_title():
    for marker_text in (
        "Sách có sẵn tại Đức",
        "Sách có sẵn ở Đức",
        "Tiệm sách Yêu Con",
        "Tiệm Sách Yêu Con ở Đức",
        "Feedbacks từ Yêu Con",
    ):
        result = extract_historical_candidates(_make_input(full_text=marker_text))

        assert result.candidates == (), f"{marker_text!r} must not become a candidate"


# --- price-only line never becomes a title ---------------------------------


def test_price_only_bullet_lines_never_become_titles():
    text = "Thanh lý:\n" "1. 6€\n" "2. 8€\n"

    result = extract_historical_candidates(_make_input(full_text=text))

    # Both bullet lines are price-only noise -- fewer than two usable
    # list items survive, so this is REVIEW_REQUIRED with zero
    # candidates, never a fabricated "6€"/"8€" title.
    assert result.candidates == ()
    assert result.extraction_outcome != Outcome.AUTO_PASS


# --- duplicate normalized title within record deduped ----------------------


def test_duplicate_titles_within_one_record_are_deduped():
    text = (
        "- “Doraemon Tap 1” của Fujiko F. Fujio\n"
        "- “Doraemon Tap 1” của Fujiko F. Fujio\n"
        "- “Conan Tap 5” của Gosho Aoyama\n"
    )

    result = extract_historical_candidates(_make_input(full_text=text))

    assert len(result.candidates) == 2
    titles = [candidate.title_raw for candidate in result.candidates]
    assert len(titles) == len(set(titles))


# --- repeated run is byte-identical (pure function, no hidden state) ------


def test_repeated_run_is_identical():
    text = (
        "Sách có sẵn:\n"
        "1. “Doraemon Tap 1” của Fujiko F. Fujio\n"
        "2. “Conan Tap 5” của Gosho Aoyama\n"
    )
    extraction_input = _make_input(
        full_text=text,
        semantic_extracted_product_hints=("Doraemon Tap 1", "Conan Tap 5"),
    )

    first = extract_historical_candidates(extraction_input)
    second = extract_historical_candidates(extraction_input)

    assert first == second


# --- no network / no Supabase / no WooCommerce side effects ---------------


def test_extraction_is_a_pure_offline_function(monkeypatch):
    """Guard against any accidental network/socket usage creeping into
    this module -- it must remain a pure function over plain strings."""
    import socket

    def _forbidden_socket(*args, **kwargs):  # pragma: no cover - guard only
        raise AssertionError("historical_candidate_extraction must never open a socket")

    monkeypatch.setattr(socket, "socket", _forbidden_socket)

    text = "“Doraemon Tap 1” của Fujiko F. Fujio, sách còn mới 100%."
    result = extract_historical_candidates(_make_input(full_text=text))

    assert len(result.candidates) == 1
