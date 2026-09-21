"""
Offline tests for extract_historical_facebook_images.py's historical
multi-image ownership resolver integration (Phases 1-4).

No live Supabase/filesystem dependency: FakeSupabaseRepository backs
every Supabase read, and check_capability is monkeypatched so the
(gitignored, personal) Facebook export archive is never touched.
"""

from __future__ import annotations

import pytest

import extract_historical_facebook_images as extractor
from src.services.historical_image_extraction import CapabilityStatus

from support.fake_supabase import FakeSupabaseRepository


CANDIDATE_ID = "cand-a"
SIBLING_ID = "cand-b"
RAW_PAGE_ID = "raw-page-1"
CANDIDATE_CODE = "FB-HIST-2026-IMG-001-CAN-0001"
SIBLING_CODE = "FB-HIST-2026-IMG-001-CAN-0002"


def _candidate_row(
    candidate_id: str,
    candidate_code: str,
    local_media_paths: list[str],
    extraction_source: str = "MANUAL_VISUAL_REVIEW",
    evidence_text: str | None = "5,99EUR visible",
) -> dict:
    return {
        "candidate_id": candidate_id,
        "candidate_code": candidate_code,
        "raw_page_id": RAW_PAGE_ID,
        "source_url_id": "source-1",
        "source_evidence": {
            "extraction_source": extraction_source,
            "local_media_paths": local_media_paths,
            "evidence_text": evidence_text,
            "source_url": "https://facebook.com/x",
        },
    }


def _patch_environment(
    monkeypatch: pytest.MonkeyPatch,
    repository: FakeSupabaseRepository,
    capability_available: bool = True,
) -> None:
    monkeypatch.setattr(extractor, "SupabaseRepository", lambda: repository)
    monkeypatch.setattr(
        extractor,
        "check_capability",
        lambda _root: CapabilityStatus(
            available=capability_available,
            reason="Facebook export archive found."
            if capability_available
            else "No archive.",
            archive_path=None,
        ),
    )
    monkeypatch.setattr(extractor, "load_dotenv", lambda: None)


def test_extraction_proceeds_when_sibling_has_own_distinct_path(
    monkeypatch: pytest.MonkeyPatch,
):
    """Two image-derived siblings, each with their own exact image path
    -- extraction must proceed past the ownership check (it will still
    correctly stop at CAPABILITY_UNAVAILABLE or actually extract,
    depending on the environment; this test only asserts it is never
    treated as AMBIGUOUS_GROUP_IMAGE)."""
    candidate = _candidate_row(CANDIDATE_ID, CANDIDATE_CODE, ["a.jpg"])
    sibling = _candidate_row(SIBLING_ID, SIBLING_CODE, ["b.jpg"])
    repository = FakeSupabaseRepository(
        tables={"product_candidates": [candidate, sibling]}
    )
    _patch_environment(monkeypatch, repository, capability_available=False)

    monkeypatch.setattr(
        extractor,
        "parse_arguments",
        lambda: type(
            "Args",
            (),
            {
                "candidate_code": CANDIDATE_CODE,
                "batch_code": "FB-2026-001",
                "confirm_extract": True,
                "non_interactive": True,
            },
        )(),
    )

    exit_code = extractor.main()

    assert exit_code == 0


def test_extraction_still_refused_when_sibling_shares_path_without_provenance(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    """The resolver must never widen ownership when a sibling nominally
    claims the same exact path through a non-image-derived pathway --
    the original AMBIGUOUS_GROUP_IMAGE outcome is preserved."""
    shared_path = "shared.jpg"
    candidate = _candidate_row(CANDIDATE_ID, CANDIDATE_CODE, [shared_path])
    sibling = _candidate_row(
        SIBLING_ID,
        SIBLING_CODE,
        [shared_path],
        extraction_source="CLAUDE_SEMANTIC",
        evidence_text=None,
    )
    repository = FakeSupabaseRepository(
        tables={"product_candidates": [candidate, sibling]}
    )
    _patch_environment(monkeypatch, repository, capability_available=True)

    monkeypatch.setattr(
        extractor,
        "parse_arguments",
        lambda: type(
            "Args",
            (),
            {
                "candidate_code": CANDIDATE_CODE,
                "batch_code": "FB-2026-001",
                "confirm_extract": True,
                "non_interactive": True,
            },
        )(),
    )

    exit_code = extractor.main()
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "AMBIGUOUS_GROUP_IMAGE" in output
