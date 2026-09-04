"""
Regression tests for review_product_images.py's historical draft-safe
identity carve-out (CLAUDE.md section 6.2/9.4).

validate_approval_request() is a pure function -- no Supabase/network
access required.
"""

from __future__ import annotations

import pytest

import review_product_images as review


def _candidate(candidate_code: str, identity_status: str) -> dict:
    return {
        "candidate_id": "cand-1",
        "candidate_code": candidate_code,
        "identity_status": identity_status,
    }


def _images() -> list[dict]:
    return [{"image_id": "img-1", "candidate_id": "cand-1"}]


def test_historical_pending_identity_is_allowed():
    candidate = _candidate("FB-HIST-2026-001-CAN-0001", "IDENTITY_PENDING")

    selected = review.validate_approval_request(
        candidate=candidate,
        images=_images(),
        main_image_id="img-1",
        rights_status="STORE_OWNED",
    )

    assert selected["image_id"] == "img-1"


def test_historical_verified_identity_is_still_allowed():
    """The carve-out only widens what's accepted -- an already-verified
    historical candidate keeps working exactly as before."""
    candidate = _candidate("FB-HIST-2026-001-CAN-0002", "IDENTITY_VERIFIED")

    selected = review.validate_approval_request(
        candidate=candidate,
        images=_images(),
        main_image_id="img-1",
        rights_status="STORE_OWNED",
    )

    assert selected["image_id"] == "img-1"


def test_historical_identity_conflict_is_rejected():
    """CLAUDE.md 9.2/9.4: a confirmed conflict is never allowed through,
    historical or not."""
    candidate = _candidate("FB-HIST-2026-001-CAN-0003", "IDENTITY_CONFLICT")

    with pytest.raises(RuntimeError):
        review.validate_approval_request(
            candidate=candidate,
            images=_images(),
            main_image_id="img-1",
            rights_status="STORE_OWNED",
        )


def test_live_pending_identity_is_still_rejected():
    """Live (non-FB-HIST) candidates keep the original strict
    IDENTITY_VERIFIED-only gate, completely unchanged."""
    candidate = _candidate("FB-2026-001-CAN-0001", "IDENTITY_PENDING")

    with pytest.raises(RuntimeError):
        review.validate_approval_request(
            candidate=candidate,
            images=_images(),
            main_image_id="img-1",
            rights_status="STORE_OWNED",
        )


def test_live_verified_identity_is_allowed():
    candidate = _candidate("FB-2026-001-CAN-0002", "IDENTITY_VERIFIED")

    selected = review.validate_approval_request(
        candidate=candidate,
        images=_images(),
        main_image_id="img-1",
        rights_status="STORE_OWNED",
    )

    assert selected["image_id"] == "img-1"
