"""Offline tests for src/domain/rules/content_rules.py.

No live Supabase/WooCommerce/Facebook dependency -- pure functions
operating on plain dicts.
"""
from __future__ import annotations

from src.domain.decisions import DecisionResult, Outcome
from src.domain.rules import content_rules as rules


# --- evaluate_internal_boilerplate --------------------------------------


def test_clean_content_auto_passes():
    content = {
        "short_description": "Một cuốn sách hay cho trẻ em.",
        "long_description": "Nội dung chi tiết về cuốn sách này.",
    }

    result = rules.evaluate_internal_boilerplate(content)

    assert result.outcome == Outcome.AUTO_PASS
    assert result.rule_code == rules.CONTENT_INTERNAL_BOILERPLATE


def test_manager_review_phrase_requires_review():
    """CLAUDE.md section 15.1's exact forbidden example."""
    content = {
        "short_description": "Great book. Manager must review this before publishing.",
    }

    result = rules.evaluate_internal_boilerplate(content)

    assert result.outcome == Outcome.REVIEW_REQUIRED
    assert result.rule_code == rules.CONTENT_INTERNAL_BOILERPLATE
    assert "short_description" in result.evidence["findings"]


def test_pending_manager_review_phrase_requires_review():
    content = {"long_description": "pending manager review"}

    result = rules.evaluate_internal_boilerplate(content)

    assert result.outcome == Outcome.REVIEW_REQUIRED


def test_should_be_completed_later_phrase_requires_review():
    content = {
        "seo_description": "This description should be completed later.",
    }

    result = rules.evaluate_internal_boilerplate(content)

    assert result.outcome == Outcome.REVIEW_REQUIRED


def test_todo_marker_requires_review():
    content = {"product_details": "TODO: add more details"}

    result = rules.evaluate_internal_boilerplate(content)

    assert result.outcome == Outcome.REVIEW_REQUIRED


def test_empty_content_field_is_not_flagged():
    content = {"short_description": None, "long_description": ""}

    result = rules.evaluate_internal_boilerplate(content)

    assert result.outcome == Outcome.AUTO_PASS


# --- evaluate_unsupported_claims -----------------------------------------


def test_claims_matching_verified_data_auto_pass():
    claimed = {"author": "Nguyen Nhat Anh", "publisher": "NXB Kim Dong"}
    verifiable = {"author": "Nguyen Nhat Anh", "publisher": "NXB Kim Dong"}

    result = rules.evaluate_unsupported_claims(claimed, verifiable)

    assert result.outcome == Outcome.AUTO_PASS
    assert result.rule_code == rules.CONTENT_VERIFIED_FACTS_ONLY


def test_claim_with_no_verifiable_source_requires_review():
    claimed = {"author": "Nguyen Nhat Anh", "awards": "Won a national prize"}
    verifiable = {"author": "Nguyen Nhat Anh"}

    result = rules.evaluate_unsupported_claims(claimed, verifiable)

    assert result.outcome == Outcome.REVIEW_REQUIRED
    assert result.rule_code == rules.CONTENT_UNSUPPORTED_CLAIM
    assert "awards" in result.evidence["unsupported_fields"]


def test_claim_conflicting_with_verified_value_requires_review():
    claimed = {"publisher": "NXB Tre"}
    verifiable = {"publisher": "NXB Kim Dong"}

    result = rules.evaluate_unsupported_claims(claimed, verifiable)

    assert result.outcome == Outcome.REVIEW_REQUIRED
    assert "publisher" in result.evidence["unsupported_fields"]


def test_empty_claim_values_are_skipped():
    claimed = {"author": "Nguyen Nhat Anh", "translator": None}
    verifiable = {"author": "Nguyen Nhat Anh"}

    result = rules.evaluate_unsupported_claims(claimed, verifiable)

    assert result.outcome == Outcome.AUTO_PASS


# --- evaluate_reference_conflict -----------------------------------------


def test_no_conflicting_fields_auto_passes():
    result = rules.evaluate_reference_conflict([])

    assert result.outcome == Outcome.AUTO_PASS
    assert result.rule_code == rules.CONTENT_VERIFIED_FACTS_ONLY


def test_conflicting_fields_require_review():
    result = rules.evaluate_reference_conflict(["page_count", "publisher"])

    assert result.outcome == Outcome.REVIEW_REQUIRED
    assert result.rule_code == rules.CONTENT_REFERENCE_CONFLICT


# --- evaluate_optional_metadata ------------------------------------------


def test_no_missing_optional_metadata():
    result = rules.evaluate_optional_metadata([])

    assert result.outcome == Outcome.AUTO_PASS
    assert result.rule_code == rules.CONTENT_MISSING_OPTIONAL_METADATA
    assert result.warnings == ()


def test_missing_optional_metadata_is_non_blocking():
    result = rules.evaluate_optional_metadata(["isbn", "weight_grams"])

    assert result.outcome == Outcome.AUTO_PASS
    assert result.rule_code == rules.CONTENT_MISSING_OPTIONAL_METADATA
    assert len(result.warnings) == 2


# --- evaluate_safe_approval ----------------------------------------------


def _pass(code: str) -> DecisionResult:
    return DecisionResult(outcome=Outcome.AUTO_PASS, rule_code=code, reason="ok")


def _review(code: str) -> DecisionResult:
    return DecisionResult(
        outcome=Outcome.REVIEW_REQUIRED, rule_code=code, reason="not sure"
    )


def test_safe_approval_all_checks_pass_auto_approves():
    checks = [_pass("A"), _pass("B")]

    result = rules.evaluate_safe_approval(
        is_first_draft=False,
        is_generic_safe_draft=False,
        checks=checks,
    )

    assert result.outcome == Outcome.AUTO_PASS
    assert result.rule_code == rules.CONTENT_SAFE_APPROVAL


def test_safe_approval_blocked_on_first_draft():
    result = rules.evaluate_safe_approval(
        is_first_draft=True,
        is_generic_safe_draft=True,
        checks=[],
    )

    assert result.outcome == Outcome.BLOCKED
    assert result.rule_code == rules.CONTENT_SAFE_APPROVAL


def test_safe_approval_requires_review_for_generic_draft():
    result = rules.evaluate_safe_approval(
        is_first_draft=False,
        is_generic_safe_draft=True,
        checks=[],
    )

    assert result.outcome == Outcome.REVIEW_REQUIRED


def test_safe_approval_requires_review_when_a_check_fails():
    checks = [_pass("A"), _review("CONTENT_UNSUPPORTED_CLAIM")]

    result = rules.evaluate_safe_approval(
        is_first_draft=False,
        is_generic_safe_draft=False,
        checks=checks,
    )

    assert result.outcome == Outcome.REVIEW_REQUIRED
    assert "CONTENT_UNSUPPORTED_CLAIM" in result.evidence["failing_rule_codes"]


def test_safe_approval_propagates_warnings_from_passing_checks():
    passing_with_warning = DecisionResult(
        outcome=Outcome.AUTO_PASS,
        rule_code="CONTENT_MISSING_OPTIONAL_METADATA",
        reason="ok",
        warnings=("isbn is missing",),
    )

    result = rules.evaluate_safe_approval(
        is_first_draft=False,
        is_generic_safe_draft=False,
        checks=[passing_with_warning],
    )

    assert result.outcome == Outcome.AUTO_PASS
    assert "isbn is missing" in result.warnings
