"""Offline tests for src/services/historical_candidate_semantic_cache.py
-- specifically the backward-compatible candidate_list_complete
inference this historical hardening pass (2026-08-30) added, plus the
existing basic get()/set() round-trip. No live Supabase/WooCommerce/
Facebook/Claude dependency -- pure file I/O against a tmp_path cache
directory.

The core question every test here answers: given a cache entry written
by the PRE-hardening provider (no candidate_list_complete key at all),
does CandidateResultCache.get() infer a safe value -- one that never
claims completeness merely because the new dataclass field defaults to
True? See historical_candidate_semantic_cache.py's own module and
_infer_legacy_candidate_list_complete() docstrings for the exact rule.
"""
from __future__ import annotations

import json
from pathlib import Path

from src.domain.rules.historical_candidate_semantic import (
    MULTIPLE_BOOKS,
    SINGLE_BOOK,
    CandidateExtractionResult,
    ExtractedCandidateCard,
)
from src.services.historical_candidate_semantic_cache import (
    LEGACY_COMPLETENESS_INFERRED_MARKER,
    CandidateResultCache,
)


def _write_legacy_payload(cache_dir: Path, key: str, payload: dict) -> None:
    """Write a JSON payload directly, bypassing CandidateResultCache.set()
    -- simulates an entry written by the PRE-hardening provider, which
    never included candidate_list_complete at all."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / f"{key}.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _legacy_candidate(i: int) -> dict:
    return {
        "title_raw": f"Tựa sách số {i}",
        "candidate_type": SINGLE_BOOK,
        "evidence_text": f"{i}. Tựa sách số {i} {i}€",
        "confidence": 0.9,
    }


# --- new-format entries: explicit field used as-is -------------------------


def test_new_format_entry_uses_explicit_true_directly(tmp_path: Path):
    cache = CandidateResultCache(tmp_path)
    result = CandidateExtractionResult(
        post_product_type=SINGLE_BOOK,
        candidates=(
            ExtractedCandidateCard(
                title_raw="X", candidate_type=SINGLE_BOOK, evidence_text="X", confidence=0.9
            ),
        ),
        confidence=0.9,
        candidate_list_complete=True,
    )
    cache.set("key1", result)

    read_back = cache.get("key1")

    assert read_back.candidate_list_complete is True
    assert LEGACY_COMPLETENESS_INFERRED_MARKER not in read_back.review_reason_codes


def test_new_format_entry_uses_explicit_false_directly(tmp_path: Path):
    cache = CandidateResultCache(tmp_path)
    result = CandidateExtractionResult(
        post_product_type=MULTIPLE_BOOKS,
        candidates=tuple(
            ExtractedCandidateCard(
                title_raw=f"T{i}", candidate_type=SINGLE_BOOK, evidence_text=f"T{i}", confidence=0.9
            )
            for i in range(50)
        ),
        confidence=0.9,
        candidate_list_complete=False,
    )
    cache.set("key2", result)

    read_back = cache.get("key2")

    assert read_back.candidate_list_complete is False
    assert LEGACY_COMPLETENESS_INFERRED_MARKER not in read_back.review_reason_codes


# --- legacy (pre-hardening) entries: inferred, and marked as inferred -----


def test_legacy_entry_with_few_candidates_is_inferred_complete(tmp_path: Path):
    """Under the OLD schema, no response could ever exceed 20 items --
    an entry with clearly fewer is provably complete (nothing was cut
    off), the common case for the other 64 pre-hardening entries."""
    payload = {
        "post_product_type": SINGLE_BOOK,
        "candidates": [_legacy_candidate(1)],
        "rejected_hints": [],
        "confidence": 0.9,
        "review_reason_codes": [],
        # no "candidate_list_complete" key at all
    }
    _write_legacy_payload(tmp_path, "legacy_small", payload)
    cache = CandidateResultCache(tmp_path)

    result = cache.get("legacy_small")

    assert result.candidate_list_complete is True
    assert LEGACY_COMPLETENESS_INFERRED_MARKER in result.review_reason_codes


def test_legacy_entry_with_19_candidates_is_inferred_complete(tmp_path: Path):
    payload = {
        "post_product_type": MULTIPLE_BOOKS,
        "candidates": [_legacy_candidate(i) for i in range(1, 20)],  # 19
        "rejected_hints": [],
        "confidence": 0.9,
        "review_reason_codes": [],
    }
    _write_legacy_payload(tmp_path, "legacy_19", payload)
    cache = CandidateResultCache(tmp_path)

    result = cache.get("legacy_19")

    assert len(result.candidates) == 19
    assert result.candidate_list_complete is True
    assert LEGACY_COMPLETENESS_INFERRED_MARKER in result.review_reason_codes


def test_legacy_entry_at_exactly_the_old_20_ceiling_is_never_inferred_complete(tmp_path: Path):
    """The critical, non-obvious case: exactly 20 candidates with NO
    explicit disclosure is INDETERMINATE under the old schema (could be
    genuinely exactly 20, or could have been silently cut off there) --
    "never silently claim completeness after truncation" means this
    must default to False, not True."""
    payload = {
        "post_product_type": MULTIPLE_BOOKS,
        "candidates": [_legacy_candidate(i) for i in range(1, 21)],  # 20
        "rejected_hints": [],
        "confidence": 0.9,
        "review_reason_codes": [],  # no explicit truncation disclosure either
    }
    _write_legacy_payload(tmp_path, "legacy_20", payload)
    cache = CandidateResultCache(tmp_path)

    result = cache.get("legacy_20")

    assert len(result.candidates) == 20
    assert result.candidate_list_complete is False
    assert LEGACY_COMPLETENESS_INFERRED_MARKER in result.review_reason_codes


def test_legacy_entry_with_explicit_truncation_reason_is_inferred_incomplete_regardless_of_count(
    tmp_path: Path,
):
    """Defensive: even a SMALL legacy entry that happens to carry a
    truncation-style reason code must not be treated as complete."""
    payload = {
        "post_product_type": SINGLE_BOOK,
        "candidates": [_legacy_candidate(1)],
        "rejected_hints": [],
        "confidence": 0.9,
        "review_reason_codes": ["SOME_OTHER_CODE", "PARTIAL_LIST_ONLY"],
    }
    _write_legacy_payload(tmp_path, "legacy_flagged", payload)
    cache = CandidateResultCache(tmp_path)

    result = cache.get("legacy_flagged")

    assert result.candidate_list_complete is False


# --- record #1483's real, actual cached shape (regression) ----------------


def test_1483_actual_cached_shape_is_inferred_incomplete(tmp_path: Path):
    """The exact payload shape captured from #1483's real pre-hardening
    cache entry during this hardening pass: 20 candidates (a genuinely
    truncated subset of the record's real 24), plus the provider's own
    real disclosure reason code, no candidate_list_complete key."""
    payload = {
        "post_product_type": MULTIPLE_BOOKS,
        "candidates": [_legacy_candidate(i) for i in range(1, 21)],
        "rejected_hints": [{"text": "Tải lên từ di động", "reason": "GENERIC_TEXT"}],
        "confidence": 0.85,
        "review_reason_codes": ["MANY_TITLES_TRUNCATED_AT_20", "BUNDLE_PRICE_AMBIGUITY"],
    }
    _write_legacy_payload(tmp_path, "record_1483", payload)
    cache = CandidateResultCache(tmp_path)

    result = cache.get("record_1483")

    assert result.candidate_list_complete is False
    assert "MANY_TITLES_TRUNCATED_AT_20" in result.review_reason_codes
    assert LEGACY_COMPLETENESS_INFERRED_MARKER in result.review_reason_codes
    # The 20 candidates themselves are still readable (diagnostic data
    # available to validate_and_gate(), never deleted from the cache).
    assert len(result.candidates) == 20


# --- cache-miss / corruption safety (unchanged behavior) -------------------


def test_missing_entry_is_a_cache_miss(tmp_path: Path):
    cache = CandidateResultCache(tmp_path)
    assert cache.get("does-not-exist") is None


def test_corrupt_json_is_treated_as_a_cache_miss(tmp_path: Path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "corrupt.json").write_text("{not valid json", encoding="utf-8")
    cache = CandidateResultCache(tmp_path)

    assert cache.get("corrupt") is None


def test_round_trip_preserves_all_fields(tmp_path: Path):
    cache = CandidateResultCache(tmp_path)
    original = CandidateExtractionResult(
        post_product_type=MULTIPLE_BOOKS,
        candidates=(
            ExtractedCandidateCard(
                title_raw="A", candidate_type=SINGLE_BOOK, evidence_text="A của X", confidence=0.9
            ),
        ),
        confidence=0.9,
        review_reason_codes=("SOME_CODE",),
        candidate_list_complete=True,
    )
    cache.set("roundtrip", original)

    read_back = cache.get("roundtrip")

    assert read_back == original
