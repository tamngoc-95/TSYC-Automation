"""Regression tests for the historical low-touch automation gaps closed in
this change:

  Part 1: per-image STORE_OWNED rights classification (not gated on
          len(images) == 1).
  Part 2: FB-HIST-only draft-safe image-reference fallback, decoupled from
          match_decision == MATCH.
  Part 3: FB-HIST-only generic-content auto-revision/approval.
  Part 4: pipeline_state.py/run_batch.py dispatch reachability for all of
          the above.

No live Supabase/filesystem/Playwright dependency anywhere in this file --
FakeSupabaseRepository backs every read/write, and
historical_image_extraction.check_capability is never invoked (none of
these tests exercise the image-ingestion-capability branch).
"""
from __future__ import annotations

from typing import Any

import pytest

import pipeline_state
import prepare_product_content
from pipeline_state import derive_candidate_state, load_candidate_bundle
from prepare_product_content import build_safe_draft, run_auto_revise_action
from src.domain.decisions import Outcome
from src.domain.rules import content_rules, image_rules
from src.domain.rules.image_rules import (
    is_historical_reference_image_draft_safe,
    select_historical_draft_safe_image_reference,
)
from src.domain.rules.content_rules import select_historical_draft_safe_content_reference

from support.fake_supabase import FakeSupabaseRepository


HIST_CANDIDATE_ID = "33333333-3333-3333-3333-333333333301"
HIST_CANDIDATE_CODE = "FB-HIST-2026-LOWTOUCH-CAN-0001"
LIVE_CANDIDATE_ID = "33333333-3333-3333-3333-333333333302"
LIVE_CANDIDATE_CODE = "FB-2026-001-CAN-0001"
INTERNAL_PRODUCT_ID = "44444444-4444-4444-4444-444444444401"
RAW_PAGE_ID = "raw-page-lowtouch-1"
SIBLING_CANDIDATE_ID = "33333333-3333-3333-3333-333333333399"
SIBLING_CANDIDATE_CODE = "FB-HIST-2026-LOWTOUCH-CAN-0002"


def _historical_candidate(**overrides: Any) -> dict[str, Any]:
    row = {
        "candidate_id": HIST_CANDIDATE_ID,
        "candidate_code": HIST_CANDIDATE_CODE,
        "candidate_type": "SINGLE_BOOK",
        "raw_page_id": RAW_PAGE_ID,
        "identity_status": "IDENTITY_PENDING",
        "extracted_title": "Đắc Nhân Tâm",
        "verified_title": "Đắc Nhân Tâm",
        "verified_author": None,
        "verified_isbn": None,
        "verified_publisher": None,
        "possible_isbn": None,
        "source_evidence": {},
    }
    row.update(overrides)
    return row


def _internal_product(**overrides: Any) -> dict[str, Any]:
    row = {
        "internal_product_id": INTERNAL_PRODUCT_ID,
        "candidate_id": HIST_CANDIDATE_ID,
        "product_code": "TSYC-" + HIST_CANDIDATE_CODE,
        "title": "Đắc Nhân Tâm",
        "author": None,
        "isbn": None,
        "publisher": None,
        "page_count": None,
        "weight_grams": 250,
        "length_cm": None,
        "width_cm": None,
        "height_cm": None,
        "language_code": "vi",
        "content_status": "APPROVED",
        "image_status": "PENDING",
        "woocommerce_status": "NOT_CREATED",
        "is_active": True,
    }
    row.update(overrides)
    return row


def _image(**overrides: Any) -> dict[str, Any]:
    row = {
        "image_id": "image-lowtouch-1",
        "candidate_id": HIST_CANDIDATE_ID,
        "source_type": "FACEBOOK",
        "usage_rights_status": "RIGHTS_UNKNOWN",
        "reference_id": None,
        "is_selected_main_image": False,
        "is_publish_eligible": False,
        "image_status": "PENDING",
    }
    row.update(overrides)
    return row


def _reference(**overrides: Any) -> dict[str, Any]:
    row = {
        "reference_id": "ref-lowtouch-1",
        "candidate_id": HIST_CANDIDATE_ID,
        "match_decision": "POSSIBLE_MATCH",
        "source_type": "BOOKSTORE",
        "source_url_id": "source-url-lowtouch-1",
        "reference_title": "Đắc Nhân Tâm",
        "reference_isbn": None,
        "reference_author": None,
        "reference_publisher": None,
        "reference_image_url": "https://example-bookstore.test/dac-nhan-tam.jpg",
        "reference_description": (
            "Đắc Nhân Tâm là cuốn sách kinh điển về nghệ thuật giao tiếp và "
            "ứng xử, được hàng triệu độc giả trên thế giới tin đọc."
        ),
    }
    row.update(overrides)
    return row


def _repository(
    candidate: dict[str, Any],
    internal_product: dict[str, Any] | None = None,
    contents: list[dict[str, Any]] | None = None,
    images: list[dict[str, Any]] | None = None,
    references: list[dict[str, Any]] | None = None,
    siblings: list[dict[str, Any]] | None = None,
) -> FakeSupabaseRepository:
    candidates = [candidate] + list(siblings or [])
    tables: dict[str, list[dict[str, Any]]] = {"product_candidates": candidates}

    if internal_product is not None:
        tables["internal_products"] = [internal_product]

    if contents is not None:
        tables["product_contents"] = contents

    if images is not None:
        tables["product_images"] = images

    if references is not None:
        tables["product_references"] = references

    return FakeSupabaseRepository(tables=tables)


# ===========================================================================
# Part 1: per-image STORE_OWNED rights classification (tests 1, 2)
# ===========================================================================


def test_multiple_own_facebook_images_each_classified_store_owned():
    """(1) FB-HIST with multiple own Facebook images: per-image STORE_OWNED
    classification works. Rights are no longer "unknown" just because more
    than one image exists -- but main-image selection among several
    equally-rights-eligible images still requires a human pick, so the
    state is IMAGE_REVIEW_REQUIRED (not the old, misleading
    RIGHTS_REVIEW_REQUIRED)."""
    candidate = _historical_candidate()
    internal_product = _internal_product(image_status="PENDING")
    images = [
        _image(image_id="img-a", source_type="FACEBOOK", reference_id=None),
        _image(image_id="img-b", source_type="FACEBOOK", reference_id=None),
        _image(image_id="img-c", source_type="FACEBOOK", reference_id=None),
    ]
    repository = _repository(candidate, internal_product, images=images)

    bundle = load_candidate_bundle(repository, HIST_CANDIDATE_CODE)
    state = derive_candidate_state(bundle)

    assert state.derived_state == "IMAGE_REVIEW_REQUIRED"
    assert state.human_gate is True
    assert "3 images are auto-classified with publishable rights" in state.human_gate_reason

    # Confirm each image independently classifies STORE_OWNED, regardless
    # of how many images exist -- the actual Part 1 rights decision.
    for image in images:
        decision = image_rules.classify_historical_image_rights(image, {})
        assert decision.outcome == Outcome.AUTO_PASS
        assert decision.evidence["rights_status"] == "STORE_OWNED"


def test_single_own_facebook_image_among_others_is_auto_approved():
    """Exactly one own-Facebook image is still auto-selected + approved,
    even though classify_historical_image_rights() now runs per-image
    (no longer gated on len(images) == 1 at the top of the function)."""
    candidate = _historical_candidate()
    internal_product = _internal_product(image_status="PENDING")
    image = _image(image_id="img-solo", source_type="FACEBOOK", reference_id=None)
    repository = _repository(candidate, internal_product, images=[image])

    bundle = load_candidate_bundle(repository, HIST_CANDIDATE_CODE)
    state = derive_candidate_state(bundle)

    assert state.derived_state == "IMAGE_APPROVAL_PENDING_HISTORICAL"
    assert state.human_gate is False
    assert state.auto_main_image_id == "img-solo"
    assert state.auto_rights_status == "STORE_OWNED"


def test_live_candidate_multiple_images_rights_classification_unchanged():
    """(2) Live candidate: unchanged. classify_historical_image_rights()
    must never be consulted for a non-historical candidate -- rights stay
    based only on the persisted usage_rights_status, exactly as before
    this change."""
    candidate = _historical_candidate(
        candidate_id=LIVE_CANDIDATE_ID,
        candidate_code=LIVE_CANDIDATE_CODE,
        source_evidence={},
    )
    internal_product = _internal_product(
        candidate_id=LIVE_CANDIDATE_ID,
        product_code="TSYC-" + LIVE_CANDIDATE_CODE,
        image_status="PENDING",
    )
    images = [
        _image(
            image_id="img-live-1",
            candidate_id=LIVE_CANDIDATE_ID,
            source_type="FACEBOOK",
            reference_id=None,
        ),
        _image(
            image_id="img-live-2",
            candidate_id=LIVE_CANDIDATE_ID,
            source_type="FACEBOOK",
            reference_id=None,
        ),
    ]
    repository = _repository(candidate, internal_product, images=images)

    bundle = load_candidate_bundle(repository, LIVE_CANDIDATE_CODE)
    state = derive_candidate_state(bundle)

    # Still RIGHTS_REVIEW_REQUIRED -- exactly the old behavior. If the
    # historical per-image classification were ever (incorrectly) applied
    # to a live candidate, this would instead read IMAGE_REVIEW_REQUIRED.
    assert state.derived_state == "RIGHTS_REVIEW_REQUIRED"
    assert state.auto_main_image_id is None


# ===========================================================================
# Part 2: FB-HIST-only draft-safe image-reference fallback (tests 3-7)
# ===========================================================================


def test_possible_match_bookstore_reference_is_draft_safe():
    """(3) FB-HIST POSSIBLE_MATCH bookstore reference + strong title + no
    conflict: reference cover is draft-safe."""
    candidate = _historical_candidate()
    reference = _reference(source_type="BOOKSTORE", match_decision="POSSIBLE_MATCH")

    decision = is_historical_reference_image_draft_safe(candidate, reference)

    assert decision.outcome == Outcome.AUTO_PASS
    assert decision.evidence["source_type"] == "BOOKSTORE"


def test_possible_match_fahasa_reference_is_draft_safe():
    """(4) FB-HIST POSSIBLE_MATCH Fahasa reference + strong title + no
    conflict: reference cover is draft-safe."""
    candidate = _historical_candidate()
    reference = _reference(source_type="FAHASA", match_decision="POSSIBLE_MATCH")

    decision = is_historical_reference_image_draft_safe(candidate, reference)

    assert decision.outcome == Outcome.AUTO_PASS
    assert decision.evidence["source_type"] == "FAHASA"

    selection = select_historical_draft_safe_image_reference(candidate, [reference])
    assert selection.outcome == Outcome.AUTO_PASS
    assert selection.evidence["reference_id"] == reference["reference_id"]


def test_conflicting_isbn_blocks_reference_image_fallback():
    """(5) Conflicting ISBN: reference-image fallback blocked."""
    candidate = _historical_candidate(verified_isbn="9786041234567")
    reference = _reference(reference_isbn="9781234567897")

    decision = is_historical_reference_image_draft_safe(candidate, reference)

    assert decision.outcome == Outcome.REVIEW_REQUIRED
    assert "ISBN" in decision.reason

    selection = select_historical_draft_safe_image_reference(candidate, [reference])
    assert selection.outcome == Outcome.BLOCKED


def test_conflicting_sellable_unit_blocks_reference_image_fallback():
    """(6) Conflicting sellable unit: fallback blocked. A BOOK_COMBO
    candidate whose reference title carries no combo/set indicator likely
    represents a single volume, not the full sellable unit (CLAUDE.md
    14.6)."""
    candidate = _historical_candidate(
        candidate_type="BOOK_COMBO",
        verified_title="Đắc Nhân Tâm - Combo 3 Cuốn",
        extracted_title="Đắc Nhân Tâm - Combo 3 Cuốn",
    )
    reference = _reference(
        reference_title="Đắc Nhân Tâm",  # no combo/set indicator
    )

    decision = is_historical_reference_image_draft_safe(candidate, reference)

    assert decision.outcome == Outcome.REVIEW_REQUIRED
    assert "sellable unit" in decision.reason.lower()


def test_unknown_domain_blocks_reference_image_fallback():
    """(7) Unknown domain: fallback blocked. A reference source_type
    outside APPROVED_REFERENCE_SOURCE_RIGHTS (e.g. OTHER, or FACEBOOK
    itself) is never auto-authorized."""
    candidate = _historical_candidate()
    reference = _reference(source_type="OTHER")

    decision = is_historical_reference_image_draft_safe(candidate, reference)

    assert decision.outcome == Outcome.REVIEW_REQUIRED
    assert decision.rule_code == image_rules.IMAGE_RIGHTS_UNKNOWN


def test_no_title_or_no_candidate_type_blocks_fallback():
    """Sanity check on the two non-conflict preconditions: a candidate
    with no meaningful title, or no known candidate_type, is never
    draft-safe regardless of the reference."""
    reference = _reference()

    no_title = is_historical_reference_image_draft_safe(
        _historical_candidate(verified_title=None, extracted_title=None), reference
    )
    assert no_title.outcome == Outcome.REVIEW_REQUIRED

    no_type = is_historical_reference_image_draft_safe(
        _historical_candidate(candidate_type=None), reference
    )
    assert no_type.outcome == Outcome.REVIEW_REQUIRED


# ===========================================================================
# Part 4 (images): ambiguous FB group tries the fallback first (test 8)
# ===========================================================================


def _sibling() -> dict[str, Any]:
    return {
        "candidate_id": SIBLING_CANDIDATE_ID,
        "candidate_code": SIBLING_CANDIDATE_CODE,
        "raw_page_id": RAW_PAGE_ID,
        "candidate_type": "SINGLE_BOOK",
        "identity_status": "IDENTITY_PENDING",
    }


def test_ambiguous_group_image_uses_reference_fallback_when_available(
    monkeypatch: pytest.MonkeyPatch,
):
    """(8) Ambiguous Facebook group image: reference-image fallback
    attempted before human gate -- and used, when a draft-safe reference
    exists, instead of stopping at IMAGE_GROUP_OWNERSHIP_AMBIGUOUS."""
    candidate = _historical_candidate(
        source_evidence={"local_media_paths": ["posts/media/x/1.jpg"]}
    )
    internal_product = _internal_product(image_status="PENDING")
    reference = _reference()
    repository = _repository(
        candidate,
        internal_product,
        references=[reference],
        siblings=[_sibling()],
    )

    bundle = load_candidate_bundle(repository, HIST_CANDIDATE_CODE)
    state = derive_candidate_state(bundle)

    assert state.derived_state == "IMAGE_REFERENCE_FALLBACK_PENDING_HISTORICAL"
    assert state.human_gate is False
    assert state.auto_reference_id == reference["reference_id"]


def test_ambiguous_group_image_stays_human_gate_when_no_fallback_available(
    monkeypatch: pytest.MonkeyPatch,
):
    """Same ambiguous-group setup, but with no draft-safe reference at all
    -- the fallback is attempted (per the above test) and correctly fails,
    so the candidate still stops at the genuine human gate, now with a
    reason naming that the fallback was tried."""
    candidate = _historical_candidate(
        source_evidence={"local_media_paths": ["posts/media/x/1.jpg"]}
    )
    internal_product = _internal_product(image_status="PENDING")
    repository = _repository(
        candidate,
        internal_product,
        references=[],
        siblings=[_sibling()],
    )

    bundle = load_candidate_bundle(repository, HIST_CANDIDATE_CODE)
    state = derive_candidate_state(bundle)

    assert state.derived_state == "IMAGE_GROUP_OWNERSHIP_AMBIGUOUS"
    assert state.human_gate is True
    assert "reference-image fallback was also attempted" in state.human_gate_reason


# ===========================================================================
# Part 3: FB-HIST-only generic-content auto-revision (tests 9-14)
# ===========================================================================


def _generic_content_row(product: dict[str, Any]) -> dict[str, Any]:
    generated = build_safe_draft(product)
    return {
        "product_content_id": "content-lowtouch-1",
        "internal_product_id": product["internal_product_id"],
        "content_language": "vi",
        **generated,
        "content_status": "REVIEW_REQUIRED",
        "review_required": True,
        "review_notes": (
            "Automatic approval declined: Content is still the generic "
            "metadata-only safe draft."
        ),
        "generation_method": "RULE_BASED",
    }


def test_generic_historical_content_dispatches_auto_revise():
    """(9) Generic historical content: auto-REVISE dispatched. When a
    draft-safe reference description is available, the state is
    CONTENT_REVISE_PENDING_HISTORICAL (automatable), not the human-gated
    CONTENT_REVIEW_REQUIRED."""
    candidate = _historical_candidate()
    internal_product = _internal_product(
        content_status="REVIEW_REQUIRED", image_status="APPROVED"
    )
    content = _generic_content_row(internal_product)
    reference = _reference()
    repository = _repository(
        candidate,
        internal_product,
        contents=[content],
        references=[reference],
    )

    bundle = load_candidate_bundle(repository, HIST_CANDIDATE_CODE)
    state = derive_candidate_state(bundle)

    assert state.derived_state == "CONTENT_REVISE_PENDING_HISTORICAL"
    assert state.human_gate is False


def test_auto_revise_action_approves_valid_enrichment():
    """(10) Revised valid content: auto-APPROVED. run_auto_revise_action()
    builds enriched content from the reference description and it passes
    the exact same deterministic APPROVE validation a human would."""
    candidate = _historical_candidate()
    internal_product = _internal_product(
        content_status="REVIEW_REQUIRED", image_status="APPROVED"
    )
    content = _generic_content_row(internal_product)
    reference = _reference()
    repository = _repository(
        candidate,
        internal_product,
        contents=[content],
        references=[reference],
    )

    result = run_auto_revise_action(
        repository=repository,
        product_code=internal_product["product_code"],
        non_interactive=True,
        confirm_revise=True,
    )

    assert result["content_status"] == "APPROVED"
    assert result["review_required"] is False
    assert "Đắc Nhân Tâm là cuốn sách kinh điển" in result["long_description"]

    # Only ever one product_contents row for this product -- REVISE/
    # AUTO_REVISE update in place, never insert a second row.
    assert len(repository.client.tables["product_contents"]) == 1

    internal_row = repository.client.tables["internal_products"][0]
    assert internal_row["content_status"] == "APPROVED"


def test_conflicting_content_reference_remains_human_review():
    """(11) Conflicting/unsupported content: remains human review. A
    reference with a conflicting ISBN can never enrich the draft
    automatically -- the candidate stays at the human-gated
    CONTENT_REVIEW_REQUIRED, and run_auto_revise_action() itself refuses."""
    candidate = _historical_candidate(verified_isbn="9786041234567")
    internal_product = _internal_product(
        content_status="REVIEW_REQUIRED", image_status="APPROVED"
    )
    content = _generic_content_row(internal_product)
    reference = _reference(reference_isbn="9781234567897")
    repository = _repository(
        candidate,
        internal_product,
        contents=[content],
        references=[reference],
    )

    bundle = load_candidate_bundle(repository, HIST_CANDIDATE_CODE)
    state = derive_candidate_state(bundle)

    assert state.derived_state == "CONTENT_REVIEW_REQUIRED"
    assert state.human_gate is True

    selection = select_historical_draft_safe_content_reference(candidate, [reference])
    assert selection.outcome == Outcome.BLOCKED

    with pytest.raises(RuntimeError, match="No draft-safe reference"):
        run_auto_revise_action(
            repository=repository,
            product_code=internal_product["product_code"],
            non_interactive=True,
            confirm_revise=True,
        )


def test_auto_revise_rerun_produces_no_duplicate_content_row():
    """(12) Rerun: no duplicate image/content rows. Running AUTO_REVISE
    again after content is already APPROVED must refuse (REVISE never
    touches APPROVED content) rather than insert a second row or
    silently redo the write."""
    candidate = _historical_candidate()
    internal_product = _internal_product(
        content_status="REVIEW_REQUIRED", image_status="APPROVED"
    )
    content = _generic_content_row(internal_product)
    reference = _reference()
    repository = _repository(
        candidate,
        internal_product,
        contents=[content],
        references=[reference],
    )

    run_auto_revise_action(
        repository=repository,
        product_code=internal_product["product_code"],
        non_interactive=True,
        confirm_revise=True,
    )
    assert len(repository.client.tables["product_contents"]) == 1

    with pytest.raises(RuntimeError, match="REVISE refuses to modify APPROVED content"):
        run_auto_revise_action(
            repository=repository,
            product_code=internal_product["product_code"],
            non_interactive=True,
            confirm_revise=True,
        )

    # Still exactly one content row, and it is still APPROVED -- the
    # rerun changed nothing.
    assert len(repository.client.tables["product_contents"]) == 1
    assert repository.client.tables["product_contents"][0]["content_status"] == "APPROVED"


def test_auto_revise_never_regresses_approved_content():
    """(13) No status regression: AUTO_REVISE must never be reachable
    against an already-APPROVED content row, mirroring REVISE's own
    APPROVED-content protection (CLAUDE.md section 2.7)."""
    candidate = _historical_candidate()
    internal_product = _internal_product(content_status="APPROVED")
    content = _generic_content_row(internal_product)
    content["content_status"] = "APPROVED"
    content["review_required"] = False
    reference = _reference()
    repository = _repository(
        candidate,
        internal_product,
        contents=[content],
        references=[reference],
    )

    with pytest.raises(RuntimeError, match="REVISE refuses to modify APPROVED content"):
        run_auto_revise_action(
            repository=repository,
            product_code=internal_product["product_code"],
            non_interactive=True,
            confirm_revise=True,
        )

    assert repository.client.tables["product_contents"][0]["content_status"] == "APPROVED"


def test_live_candidate_content_never_auto_revised():
    """(14) No change to FB-2026-* behavior. A live candidate's generic
    REVIEW_REQUIRED content must never derive as CONTENT_REVISE_PENDING_
    HISTORICAL, and --action AUTO_REVISE must refuse it outright."""
    candidate = _historical_candidate(
        candidate_id=LIVE_CANDIDATE_ID,
        candidate_code=LIVE_CANDIDATE_CODE,
        source_evidence={},
    )
    internal_product = _internal_product(
        candidate_id=LIVE_CANDIDATE_ID,
        product_code="TSYC-" + LIVE_CANDIDATE_CODE,
        content_status="REVIEW_REQUIRED",
        image_status="APPROVED",
    )
    content = _generic_content_row(internal_product)
    reference = _reference(candidate_id=LIVE_CANDIDATE_ID)
    repository = _repository(
        candidate,
        internal_product,
        contents=[content],
        references=[reference],
    )

    bundle = load_candidate_bundle(repository, LIVE_CANDIDATE_CODE)
    state = derive_candidate_state(bundle)

    assert state.derived_state == "CONTENT_REVIEW_REQUIRED"
    assert state.human_gate is True

    with pytest.raises(RuntimeError, match="only available for FB-HIST"):
        run_auto_revise_action(
            repository=repository,
            product_code=internal_product["product_code"],
            non_interactive=True,
            confirm_revise=True,
        )
