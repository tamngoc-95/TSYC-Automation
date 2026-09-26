"""
Tests for src.domain.rules.lane_rules -- the read-only operational-lane
classifier backing scripts/export_historical_orchestration_state.py.

Pure function, plain-dict/dataclass inputs, no live database required.
"""

from dataclasses import dataclass

from src.domain.rules import lane_rules


@dataclass
class _FakeCandidateState:
    derived_state: str
    recovery_state: str | None = None
    human_gate: bool = False
    terminal: bool = False
    blocked: bool = False
    product_code: str | None = None


class TestClassifyLanePrecedence:
    def test_recovery_state_wins_over_everything(self):
        state = _FakeCandidateState(
            derived_state="READY_FOR_DRAFT",
            recovery_state="CREATE_RESULT_UNCERTAIN",
            human_gate=True,
        )
        assert lane_rules.classify_lane(state) == lane_rules.RECOVERY_REVIEW

    def test_identity_conflict_is_conflict_lane(self):
        state = _FakeCandidateState(derived_state="IDENTITY_CONFLICT", human_gate=True)
        assert lane_rules.classify_lane(state) == lane_rules.CONFLICT

    def test_ready_for_draft_wins_over_human_gate(self):
        # pipeline_state.py sets human_gate=True on READY_FOR_DRAFT itself
        # (the Woo-draft batch authorization gate) -- that must not be
        # relabeled HUMAN_REVIEW.
        state = _FakeCandidateState(derived_state="READY_FOR_DRAFT", human_gate=True)
        assert lane_rules.classify_lane(state) == lane_rules.READY_FOR_DRAFT

    def test_ready_for_draft_historical_has_no_human_gate(self):
        state = _FakeCandidateState(derived_state="READY_FOR_DRAFT_HISTORICAL")
        assert lane_rules.classify_lane(state) == lane_rules.READY_FOR_DRAFT

    def test_ready_for_draft_without_multilingual_is_multilingual_content(self):
        state = _FakeCandidateState(
            derived_state="READY_FOR_DRAFT", human_gate=True, product_code="PROD-1"
        )
        assert (
            lane_rules.classify_lane(state, multilingual_ready=False)
            == lane_rules.MULTILINGUAL_CONTENT
        )

    def test_ready_for_draft_historical_without_multilingual_is_multilingual_content(self):
        state = _FakeCandidateState(
            derived_state="READY_FOR_DRAFT_HISTORICAL", product_code="PROD-1"
        )
        assert (
            lane_rules.classify_lane(state, multilingual_ready=False)
            == lane_rules.MULTILINGUAL_CONTENT
        )

    def test_recovery_and_conflict_still_outrank_multilingual(self):
        recovery = _FakeCandidateState(
            derived_state="READY_FOR_DRAFT_HISTORICAL",
            recovery_state="CREATE_RESULT_UNCERTAIN",
            product_code="PROD-1",
        )
        assert (
            lane_rules.classify_lane(recovery, multilingual_ready=False)
            == lane_rules.RECOVERY_REVIEW
        )

    def test_is_ready_for_draft_state(self):
        assert lane_rules.is_ready_for_draft_state("READY_FOR_DRAFT")
        assert lane_rules.is_ready_for_draft_state("READY_FOR_DRAFT_HISTORICAL")
        assert not lane_rules.is_ready_for_draft_state("DRAFT_CREATED")

    def test_reconciled_is_terminal(self):
        state = _FakeCandidateState(derived_state="RECONCILED", terminal=True)
        assert lane_rules.classify_lane(state) == lane_rules.TERMINAL

    def test_draft_created_is_terminal_even_without_terminal_flag(self):
        # pipeline_state.CandidateState does not set .terminal=True for
        # DRAFT_CREATED, but it still needs no further Fast Track action.
        state = _FakeCandidateState(derived_state="DRAFT_CREATED", terminal=False)
        assert lane_rules.classify_lane(state) == lane_rules.TERMINAL

    def test_duplicate_rejected_is_terminal(self):
        state = _FakeCandidateState(derived_state="DUPLICATE_REJECTED", terminal=True)
        assert lane_rules.classify_lane(state) == lane_rules.TERMINAL

    def test_human_gate_is_human_review(self):
        state = _FakeCandidateState(derived_state="EXTRACTED", human_gate=True)
        assert lane_rules.classify_lane(state) == lane_rules.HUMAN_REVIEW

    def test_blocked_is_enrichment_needed(self):
        state = _FakeCandidateState(derived_state="IMAGE_CAPABILITY_UNAVAILABLE", blocked=True)
        assert lane_rules.classify_lane(state) == lane_rules.ENRICHMENT_NEEDED

    def test_multilingual_content_requires_internal_product(self):
        # Previously asserted CONTENT_DRAFTED -> MULTILINGUAL_CONTENT, which
        # was the lane-B defect (vi not yet approved). The internal-product
        # requirement is now asserted on a post-vi-approval state.
        state = _FakeCandidateState(derived_state="IMAGE_VALIDATED", product_code="PROD-1")
        assert (
            lane_rules.classify_lane(
                state, multilingual_ready=False, vi_content_approved=True
            )
            == lane_rules.MULTILINGUAL_CONTENT
        )
        no_product = _FakeCandidateState(derived_state="IMAGE_VALIDATED", product_code=None)
        assert (
            lane_rules.classify_lane(
                no_product, multilingual_ready=False, vi_content_approved=True
            )
            == lane_rules.FAST_TRACK
        )

    def test_no_multilingual_lane_without_internal_product(self):
        state = _FakeCandidateState(derived_state="EXTRACTED", product_code=None)
        assert (
            lane_rules.classify_lane(state, multilingual_ready=False)
            == lane_rules.FAST_TRACK
        )

    def test_default_fast_track(self):
        state = _FakeCandidateState(derived_state="CONTENT_DRAFTED", product_code="PROD-1")
        assert lane_rules.classify_lane(state, multilingual_ready=True) == lane_rules.FAST_TRACK


_VI_APPROVED = {"content_language": "vi", "content_status": "APPROVED"}
_EN_APPROVED = {"content_language": "en", "content_status": "APPROVED"}
_DE_APPROVED = {"content_language": "de", "content_status": "APPROVED"}


def _classify(derived_state, contents, **state_fields):
    """Classify exactly the way export_historical_orchestration_state.py
    does: both signals derived from the same product_contents rows."""
    state = _FakeCandidateState(
        derived_state=derived_state,
        product_code=state_fields.pop("product_code", "PROD-1"),
        **state_fields,
    )
    return lane_rules.classify_lane(
        state,
        multilingual_ready=lane_rules.has_multilingual_content(contents),
        vi_content_approved=lane_rules.has_approved_vi_content(contents),
    )


class TestMultilingualLaneRequiresApprovedVi:
    """Regression: LANE-B-ENABLEMENT-2026-09-24 -- 9 image-stage candidates
    with zero product_contents rows were labeled MULTILINGUAL_CONTENT."""

    def test_image_ingest_pending_without_contents_is_fast_track(self):
        assert _classify("IMAGE_INGEST_PENDING_HISTORICAL", []) == lane_rules.FAST_TRACK

    def test_image_approval_pending_without_contents_is_fast_track(self):
        assert _classify("IMAGE_APPROVAL_PENDING_HISTORICAL", []) == lane_rules.FAST_TRACK

    def test_image_stage_is_fast_track_even_if_vi_row_approved(self):
        # derived_state gate: an image stage is never lane B.
        assert (
            _classify("IMAGE_APPROVAL_PENDING_HISTORICAL", [_VI_APPROVED])
            == lane_rules.FAST_TRACK
        )

    def test_ready_for_content_without_vi_approved_is_fast_track(self):
        assert _classify("READY_FOR_CONTENT", []) == lane_rules.FAST_TRACK

    def test_content_drafted_with_drafted_vi_is_fast_track(self):
        contents = [{"content_language": "vi", "content_status": "DRAFTED"}]
        assert _classify("CONTENT_DRAFTED", contents) == lane_rules.FAST_TRACK

    def test_content_revise_pending_is_fast_track(self):
        contents = [{"content_language": "vi", "content_status": "REVIEW_REQUIRED"}]
        assert (
            _classify("CONTENT_REVISE_PENDING_HISTORICAL", contents)
            == lane_rules.FAST_TRACK
        )

    def test_post_vi_state_with_vi_approved_and_no_translations_is_multilingual(self):
        assert _classify("IMAGE_VALIDATED", [_VI_APPROVED]) == lane_rules.MULTILINGUAL_CONTENT

    def test_post_vi_state_without_vi_approved_is_fast_track(self):
        assert _classify("IMAGE_VALIDATED", []) == lane_rules.FAST_TRACK

    def test_vi_and_en_only_is_multilingual(self):
        assert (
            _classify("IMAGE_VALIDATED", [_VI_APPROVED, _EN_APPROVED])
            == lane_rules.MULTILINGUAL_CONTENT
        )

    def test_vi_en_de_approved_non_ready_state_is_fast_track(self):
        assert (
            _classify("IMAGE_VALIDATED", [_VI_APPROVED, _EN_APPROVED, _DE_APPROVED])
            == lane_rules.FAST_TRACK
        )

    def test_ready_for_draft_historical_without_translations_is_multilingual(self):
        assert (
            _classify("READY_FOR_DRAFT_HISTORICAL", [_VI_APPROVED])
            == lane_rules.MULTILINGUAL_CONTENT
        )

    def test_ready_for_draft_historical_with_translations_is_ready(self):
        assert (
            _classify(
                "READY_FOR_DRAFT_HISTORICAL", [_VI_APPROVED, _EN_APPROVED, _DE_APPROVED]
            )
            == lane_rules.READY_FOR_DRAFT
        )

    def test_recovery_state_outranks_multilingual(self):
        assert (
            _classify(
                "IMAGE_VALIDATED", [_VI_APPROVED], recovery_state="CREATE_RESULT_UNCERTAIN"
            )
            == lane_rules.RECOVERY_REVIEW
        )

    def test_identity_conflict_outranks_multilingual(self):
        assert (
            _classify("IDENTITY_CONFLICT", [_VI_APPROVED], human_gate=True)
            == lane_rules.CONFLICT
        )

    def test_terminal_outranks_multilingual(self):
        assert _classify("DRAFT_CREATED", [_VI_APPROVED]) == lane_rules.TERMINAL
        assert (
            _classify("IMAGE_VALIDATED", [_VI_APPROVED], terminal=True)
            == lane_rules.TERMINAL
        )

    def test_human_gate_outranks_multilingual(self):
        assert (
            _classify("CONTENT_APPROVED", [_VI_APPROVED], human_gate=True)
            == lane_rules.HUMAN_REVIEW
        )

    def test_blocked_outranks_multilingual(self):
        assert (
            _classify("IMAGE_VALIDATED", [_VI_APPROVED], blocked=True)
            == lane_rules.ENRICHMENT_NEEDED
        )


class TestHasApprovedViContent:
    def test_false_when_no_contents(self):
        assert lane_rules.has_approved_vi_content([]) is False

    def test_false_when_vi_drafted(self):
        contents = [{"content_language": "vi", "content_status": "DRAFTED"}]
        assert lane_rules.has_approved_vi_content(contents) is False

    def test_true_when_vi_approved(self):
        assert lane_rules.has_approved_vi_content([_VI_APPROVED]) is True

    def test_false_when_only_en_approved(self):
        assert lane_rules.has_approved_vi_content([_EN_APPROVED]) is False


class TestHasMultilingualContent:
    def test_true_when_en_and_de_approved(self):
        contents = [
            {"content_language": "vi", "content_status": "APPROVED"},
            {"content_language": "en", "content_status": "APPROVED"},
            {"content_language": "de", "content_status": "APPROVED"},
        ]
        assert lane_rules.has_multilingual_content(contents) is True

    def test_false_when_de_missing(self):
        contents = [
            {"content_language": "vi", "content_status": "APPROVED"},
            {"content_language": "en", "content_status": "APPROVED"},
        ]
        assert lane_rules.has_multilingual_content(contents) is False

    def test_false_when_en_drafted_not_approved(self):
        contents = [
            {"content_language": "en", "content_status": "DRAFTED"},
            {"content_language": "de", "content_status": "APPROVED"},
        ]
        assert lane_rules.has_multilingual_content(contents) is False

    def test_false_when_no_contents(self):
        assert lane_rules.has_multilingual_content([]) is False


class TestLaneConstants:
    def test_all_nine_lanes_present(self):
        assert len(lane_rules.LANES) == 9
        assert lane_rules.HISTORICAL_RECOVERY_BACKLOG in lane_rules.LANES

    def test_classify_lane_never_returns_historical_recovery_backlog(self):
        # HISTORICAL_RECOVERY_BACKLOG counts raw_pages with no candidate at
        # all -- there is no CandidateState for it to be classified from.
        for derived_state in (
            "EXTRACTED",
            "READY_FOR_DRAFT",
            "READY_FOR_DRAFT_HISTORICAL",
            "RECONCILED",
            "DRAFT_CREATED",
            "IDENTITY_CONFLICT",
        ):
            state = _FakeCandidateState(derived_state=derived_state, human_gate=True)
            assert lane_rules.classify_lane(state) != lane_rules.HISTORICAL_RECOVERY_BACKLOG
