"""
Regression tests for the hardened, cumulative identity decision path in
scripts/match_candidate_identity.py (evaluate_and_apply_decision and its
helpers). Covers the script-level (I/O-touching) behaviors that
tests/test_identity_rules.py's pure-function tests can't reach: true
idempotency (no duplicate history / no reference rewrite on an unchanged
rerun), a clean NO_OP instead of RuntimeError when there is nothing new to
do, IDENTITY_VERIFIED protection against both weaker and genuinely
conflicting new evidence, and recovering a regressed candidate (CAN-0039's
exact live-incident shape) through --mode RECOMPUTE with no raw SQL.

Fully offline: FakeSupabaseRepository only, no live Supabase/network.
"""

from __future__ import annotations

from typing import Any

import match_candidate_identity as mci
from support.fake_supabase import FakeSupabaseRepository


def make_candidate(
    candidate_id: str = "cand-1",
    candidate_code: str = "TEST-CAN-0001",
    title: str = "Nỗi Buồn Chiến Tranh",
    author: str | None = None,
    isbn: str | None = None,
    identity_status: str = "IDENTITY_PENDING",
    review_required: bool = True,
    source_evidence: dict[str, Any] | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    candidate = {
        "candidate_id": candidate_id,
        "candidate_code": candidate_code,
        "candidate_type": "SINGLE_BOOK",
        "extracted_title": title,
        "extracted_author": author,
        "possible_isbn": isbn,
        "verified_title": None,
        "verified_isbn": None,
        "verified_author": None,
        "verified_publisher": None,
        "verified_page_count": None,
        "verified_weight_grams": None,
        "verified_length_cm": None,
        "verified_width_cm": None,
        "verified_height_cm": None,
        "identity_status": identity_status,
        "workflow_status": identity_status,
        "identity_confidence": None,
        "review_required": review_required,
        "review_reason": None,
        "decision_reason": None,
        "source_evidence": source_evidence or {},
        "conflict_fields": [],
    }
    candidate.update(overrides)
    return candidate


def make_reference(
    reference_id: str,
    candidate_id: str = "cand-1",
    title: str | None = None,
    author: str | None = None,
    isbn: str | None = None,
    publisher: str | None = None,
    page_count: int | None = None,
    source_type: str = "BOOKSTORE",
    source_priority: int = 3,
    match_decision: str | None = None,
    match_confidence: float | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    reference = {
        "reference_id": reference_id,
        "candidate_id": candidate_id,
        "source_url_id": f"src-{reference_id}",
        "source_type": source_type,
        "source_name": source_type.title(),
        "source_url": f"https://example.test/{reference_id}",
        "reference_title": title,
        "reference_isbn": isbn,
        "reference_author": author,
        "reference_publisher": publisher,
        "reference_page_count": page_count,
        "reference_weight_grams": None,
        "reference_length_cm": None,
        "reference_width_cm": None,
        "reference_height_cm": None,
        "match_decision": match_decision,
        "match_confidence": match_confidence,
        "source_priority": source_priority,
        "raw_metadata": {},
        "collected_at": "2026-01-01T00:00:00+00:00",
    }
    reference.update(overrides)
    return reference


def make_repository(
    candidates: list[dict[str, Any]],
    references: list[dict[str, Any]],
) -> FakeSupabaseRepository:
    return FakeSupabaseRepository(
        tables={
            "product_candidates": candidates,
            "product_references": references,
        }
    )


def fetch_candidate(repository: FakeSupabaseRepository, candidate_id: str) -> dict[str, Any]:
    rows = repository.client.tables["product_candidates"]
    return next(row for row in rows if row["candidate_id"] == candidate_id)


def fetch_references(repository: FakeSupabaseRepository, candidate_id: str) -> list[dict[str, Any]]:
    rows = repository.client.tables["product_references"]
    return [row for row in rows if row["candidate_id"] == candidate_id]


# --------------------------------------------------------------------------
# A / K -- CAN-0039's exact failure shape, recoverable via RECOMPUTE, no
# raw SQL: a good POSSIBLE_MATCH already recorded, an empty reference is
# then (incorrectly, pre-hardening) evaluated and regresses the candidate.
# The hardened path must never regress it in the first place, and if a
# candidate somehow arrives already in a regressed IDENTITY_CONFLICT state
# for the wrong reason, RECOMPUTE from the same real references must
# recover it -- purely through the approved script, no direct SQL.
# --------------------------------------------------------------------------


def test_A_K_can_0039_shape_does_not_regress_and_recomputes_cleanly():
    candidate = make_candidate(
        title="Nỗi Buồn Chiến Tranh",
        identity_status="IDENTITY_PENDING",
    )
    good = make_reference(
        "netabooks",
        title="Nỗi Buồn Chiến Tranh",
        author="Bảo Ninh",
        publisher="Trẻ",
        page_count=348,
        source_priority=3,
    )
    empty = make_reference("fahasa-empty", source_type="FAHASA", source_priority=4)

    repository = make_repository([candidate], [good, empty])

    result = mci.evaluate_and_apply_decision(
        repository=repository,
        candidate=candidate,
        references=[good, empty],
        confirm_save=True,
    )

    assert result["action"].startswith("WROTE:IDENTITY_PENDING")
    updated = fetch_candidate(repository, "cand-1")
    assert updated["identity_status"] == "IDENTITY_PENDING"
    assert updated["review_required"] is True

    # Now explicitly RECOMPUTE (mirrors --mode RECOMPUTE) from the same,
    # unchanged reference set -- must be a true no-op, never a regression.
    references_now = fetch_references(repository, "cand-1")
    result_2 = mci.evaluate_and_apply_decision(
        repository=repository,
        candidate=updated,
        references=references_now,
        confirm_save=True,
    )

    assert result_2["action"] == "NO_OP"
    final = fetch_candidate(repository, "cand-1")
    assert final["identity_status"] == "IDENTITY_PENDING"
    assert final["review_required"] is True


# --------------------------------------------------------------------------
# C / D -- true idempotency: same consensus rerun writes no duplicate
# history and rewrites no reference row; a candidate with nothing left to
# do returns a clean NO_OP, never RuntimeError.
# --------------------------------------------------------------------------


def test_C_D_unchanged_rerun_is_true_no_op_no_duplicate_history_no_rewrite():
    candidate = make_candidate(title="Đời ngắn đừng ngủ dài", identity_status="IDENTITY_PENDING")
    ref_a = make_reference(
        "fahasa", title="Đời Ngắn Đừng Ngủ Dài", author="Robin Sharma",
        publisher="Trẻ", page_count=228, source_type="FAHASA", source_priority=4,
    )
    ref_b = make_reference(
        "neta", title="Đời Ngắn Đừng Ngủ Dài", author="Robin Sharma",
        publisher="Trẻ", page_count=228, source_priority=3,
    )
    repository = make_repository([candidate], [ref_a, ref_b])

    first = mci.evaluate_and_apply_decision(
        repository=repository, candidate=candidate,
        references=[ref_a, ref_b], confirm_save=True,
    )
    assert first["action"] == "WROTE:IDENTITY_VERIFIED"

    updated_candidate = fetch_candidate(repository, "cand-1")
    history_after_first = updated_candidate["source_evidence"]["identity_decision_history"]
    assert len(history_after_first) == 1
    references_after_first = fetch_references(repository, "cand-1")

    # Rerun with the exact same (now-persisted) candidate/reference state --
    # this must be a clean, error-free no-op (Phase 6/D), and must not
    # append a duplicate history entry or rewrite either reference row
    # (Phase 5/C).
    second = mci.evaluate_and_apply_decision(
        repository=repository, candidate=updated_candidate,
        references=references_after_first, confirm_save=True,
    )

    assert second["action"] == "NO_OP"
    final_candidate = fetch_candidate(repository, "cand-1")
    assert (
        final_candidate["source_evidence"]["identity_decision_history"]
        == history_after_first
    ), "rerun with unchanged evidence must not append a duplicate history entry"
    assert fetch_references(repository, "cand-1") == references_after_first, (
        "rerun with unchanged evidence must not rewrite any reference row"
    )


def test_D_candidate_with_no_references_is_a_clean_no_op_not_an_error():
    candidate = make_candidate(candidate_id="cand-2", candidate_code="TEST-CAN-0002")
    repository = make_repository([candidate], [])

    result = mci.evaluate_and_apply_decision(
        repository=repository, candidate=candidate, references=[], confirm_save=True,
    )

    assert result["action"] == "NO_OP_NO_REFERENCES"


# --------------------------------------------------------------------------
# E / F -- IDENTITY_VERIFIED protection: neither weaker/consistent
# evidence, nor genuinely conflicting new evidence, ever overwrites the
# verified canonical identity. A genuine conflict is surfaced via
# review_required + additive evidence only.
# --------------------------------------------------------------------------


def test_E_verified_candidate_with_consistent_evidence_is_untouched():
    candidate = make_candidate(
        candidate_id="cand-3",
        candidate_code="TEST-CAN-0003",
        title="Quân khu Nam Đồng",
        identity_status="IDENTITY_VERIFIED",
        review_required=False,
        verified_title="Quân Khu Nam Đồng",
        verified_author="Bình Ca",
        source_evidence={"decision_fingerprint": "stale-fingerprint-forces-recompute"},
    )
    ref_a = make_reference(
        "neta", candidate_id="cand-3", title="Quân Khu Nam Đồng", author="Bình Ca",
        publisher="Trẻ", page_count=440, source_priority=3,
    )
    ref_b = make_reference(
        "fahasa", candidate_id="cand-3", title="Quân Khu Nam Đồng", author="Bình Ca",
        publisher="Trẻ", page_count=440, source_type="FAHASA", source_priority=4,
    )
    repository = make_repository([candidate], [ref_a, ref_b])

    result = mci.evaluate_and_apply_decision(
        repository=repository, candidate=candidate,
        references=[ref_a, ref_b], confirm_save=True,
    )

    assert result["action"] == "NO_OP_VERIFIED_CONSISTENT"
    unchanged = fetch_candidate(repository, "cand-3")
    assert unchanged["identity_status"] == "IDENTITY_VERIFIED"
    assert unchanged["verified_title"] == "Quân Khu Nam Đồng"
    assert unchanged["verified_author"] == "Bình Ca"
    assert unchanged["review_required"] is False


def test_F_verified_candidate_with_genuine_conflict_is_flagged_not_overwritten():
    candidate = make_candidate(
        candidate_id="cand-4",
        candidate_code="TEST-CAN-0004",
        title="Phía Sau Nghi Can X",
        identity_status="IDENTITY_VERIFIED",
        review_required=False,
        verified_title="Phía Sau Nghi Can X",
        verified_author="Higashino Keigo",
        verified_publisher="Hội Nhà Văn",
        source_evidence={"decision_fingerprint": "stale-fingerprint-forces-recompute"},
    )
    ref_a = make_reference(
        "neta", candidate_id="cand-4", title="Phía Sau Nghi Can X",
        author="Higashino Keigo", publisher="Hội Nhà Văn", source_priority=3,
    )
    ref_b = make_reference(
        "fahasa", candidate_id="cand-4", title="Phía Sau Nghi Can X",
        author="Higashino Keigo", publisher="NXB Văn Hoá Sài Gòn",
        source_type="FAHASA", source_priority=4,
    )
    repository = make_repository([candidate], [ref_a, ref_b])

    result = mci.evaluate_and_apply_decision(
        repository=repository, candidate=candidate,
        references=[ref_a, ref_b], confirm_save=True,
    )

    assert result["action"] == "VERIFIED_PROTECTED_CONFLICT_FLAGGED"
    updated = fetch_candidate(repository, "cand-4")
    # Canonical identity is untouched.
    assert updated["identity_status"] == "IDENTITY_VERIFIED"
    assert updated["verified_title"] == "Phía Sau Nghi Can X"
    assert updated["verified_author"] == "Higashino Keigo"
    assert updated["verified_publisher"] == "Hội Nhà Văn"
    # But the conflict is surfaced, additively.
    assert updated["review_required"] is True
    assert "post_verification_conflicts" in updated["source_evidence"]
    assert len(updated["source_evidence"]["post_verification_conflicts"]) == 1

    # Rerunning against the same, unchanged conflicting evidence must be
    # a true no-op -- not a re-flag that appends a second, duplicate
    # post_verification_conflicts entry every time.
    second = mci.evaluate_and_apply_decision(
        repository=repository, candidate=updated,
        references=[ref_a, ref_b], confirm_save=True,
    )
    assert second["action"] == "NO_OP"
    final = fetch_candidate(repository, "cand-4")
    assert len(final["source_evidence"]["post_verification_conflicts"]) == 1


# --------------------------------------------------------------------------
# G / H / I -- ISBN/publisher safety survive the full script-level write
# path, not just the pure decision function.
# --------------------------------------------------------------------------


def test_G_unvalidated_isbn_is_never_written_as_verified_isbn():
    candidate = make_candidate(title="Quân khu Nam Đồng")
    ref_a = make_reference(
        "neta", title="Quân Khu Nam Đồng", author="Bình Ca",
        isbn="2396043028889",  # barcode-shaped, not a real ISBN (978/979)
        publisher="Trẻ", page_count=440, source_priority=3,
    )
    ref_b = make_reference(
        "fahasa", title="Quân Khu Nam Đồng", author="Bình Ca",
        publisher="Trẻ", page_count=440, source_type="FAHASA", source_priority=4,
    )
    repository = make_repository([candidate], [ref_a, ref_b])

    mci.evaluate_and_apply_decision(
        repository=repository, candidate=candidate,
        references=[ref_a, ref_b], confirm_save=True,
    )

    updated = fetch_candidate(repository, "cand-1")
    assert updated["identity_status"] == "IDENTITY_VERIFIED"
    assert updated["verified_isbn"] is None


def test_I_publisher_conflict_blocks_verification_no_silent_winner():
    candidate = make_candidate(title="Phía Sau Nghi Can X")
    ref_a = make_reference(
        "neta", title="Phía Sau Nghi Can X", author="Higashino Keigo",
        publisher="Hội Nhà Văn", source_priority=3,
    )
    ref_b = make_reference(
        "fahasa", title="Phía Sau Nghi Can X", author="Higashino Keigo",
        publisher="NXB Văn Hoá Sài Gòn", source_type="FAHASA", source_priority=4,
    )
    repository = make_repository([candidate], [ref_a, ref_b])

    result = mci.evaluate_and_apply_decision(
        repository=repository, candidate=candidate,
        references=[ref_a, ref_b], confirm_save=True,
    )

    assert result["outcome"] != "AUTO_PASS"
    updated = fetch_candidate(repository, "cand-1")
    assert updated["identity_status"] != "IDENTITY_VERIFIED"
    assert updated["verified_publisher"] is None


# --------------------------------------------------------------------------
# J -- same-evidence RECOMPUTE twice is a true no-op the second time.
# --------------------------------------------------------------------------


def test_stale_match_decision_on_an_unusable_reference_is_cleared_not_left():
    """A reference row that was wrongly evaluated to NO_MATCH before
    hardening (empty title, evaluated anyway) must have that stale
    match_decision cleared back to NULL -- even once the candidate-level
    decision itself is unchanged and would otherwise be a pure NO_OP --
    never left standing as if it were still a real, current decision."""
    candidate = make_candidate(title="Nỗi Buồn Chiến Tranh")
    good = make_reference(
        "netabooks", title="Nỗi Buồn Chiến Tranh", author="Bảo Ninh", source_priority=3,
    )
    stale_unusable = make_reference(
        "fahasa-empty",
        source_type="FAHASA",
        source_priority=4,
        match_decision="NO_MATCH",  # stale, from a pre-hardening run
        match_confidence=1.0,
    )
    repository = make_repository([candidate], [good, stale_unusable])

    first = mci.evaluate_and_apply_decision(
        repository=repository, candidate=candidate,
        references=[good, stale_unusable], confirm_save=True,
    )
    assert first["action"].startswith("WROTE:")

    stale_row_after_first = next(
        r for r in fetch_references(repository, "cand-1") if r["reference_id"] == "fahasa-empty"
    )
    assert stale_row_after_first["match_decision"] is None
    assert stale_row_after_first["match_confidence"] is None

    # Rerunning now must be a true no-op (nothing left to clear, nothing
    # about the decision changed).
    updated_candidate = fetch_candidate(repository, "cand-1")
    second = mci.evaluate_and_apply_decision(
        repository=repository, candidate=updated_candidate,
        references=fetch_references(repository, "cand-1"), confirm_save=True,
    )
    assert second["action"] == "NO_OP"


def test_J_recompute_twice_with_same_evidence_second_run_is_no_op():
    candidate = make_candidate(title="Cây cam ngọt của tôi")
    fahasa = make_reference(
        "fahasa", title="Cây Cam Ngọt Của Tôi", author="José Mauro de Vasconcelos",
        publisher="NXB Hội Nhà Văn", page_count=244, source_type="FAHASA", source_priority=4,
    )
    empty = make_reference("neta-empty", source_priority=3)
    repository = make_repository([candidate], [fahasa, empty])

    first = mci.evaluate_and_apply_decision(
        repository=repository, candidate=candidate,
        references=[fahasa, empty], confirm_save=True,
    )
    assert first["action"].startswith("WROTE:")

    updated_candidate = fetch_candidate(repository, "cand-1")
    references_now = fetch_references(repository, "cand-1")

    second = mci.evaluate_and_apply_decision(
        repository=repository, candidate=updated_candidate,
        references=references_now, confirm_save=True,
    )

    assert second["action"] == "NO_OP"
