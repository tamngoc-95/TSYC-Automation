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
        state = _FakeCandidateState(derived_state="CONTENT_DRAFTED", product_code="PROD-1")
        assert (
            lane_rules.classify_lane(state, multilingual_ready=False)
            == lane_rules.MULTILINGUAL_CONTENT
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
