"""Offline tests for src/domain/rules/facebook_history_semantic.py.

No live Supabase/WooCommerce/Facebook/Claude dependency -- pure functions
and dataclasses only. Covers route_record()'s exhaustive partition of
every branch classify() can produce, and synthesize_final_decision()'s
rules.
"""
from __future__ import annotations

import pytest

from src.domain.rules import facebook_history_classification as rules
from src.domain.rules import facebook_history_semantic as semantic


# --- route_record(): exhaustive partition ---------------------------------


def test_low_relevance_routes_to_skip_low():
    result = rules.classify(full_text="Hội những người siêu tích cực va vào nhau")

    assert result.tsyc_relevance == rules.TsycRelevance.LOW
    assert semantic.route_record(result) == semantic.RoutingDecision.SKIP_LOW


def test_high_eligible_product_post_routes_to_bypass_strong_include():
    result = rules.classify(full_text="Sách có sẵn tại Đức, inbox em nhé")

    assert result.tsyc_relevance == rules.TsycRelevance.HIGH
    assert result.candidate_eligible is True
    assert semantic.route_record(result) == semantic.RoutingDecision.BYPASS_STRONG_INCLUDE


def test_high_eligible_promotion_routes_to_bypass_strong_include():
    text = "Đầu năm mở hàng lấy may, em giảm giá 20% cho toàn bộ sách có sẵn tại Tiệm sách Yêu Con"
    result = rules.classify(full_text=text)

    assert result.tsyc_relevance == rules.TsycRelevance.HIGH
    assert result.post_type == rules.PostType.PROMOTION
    assert result.candidate_eligible is True
    assert semantic.route_record(result) == semantic.RoutingDecision.BYPASS_STRONG_INCLUDE


def test_high_strong_book_review_routes_to_bypass_confident_exclude():
    text = (
        "[Lời cảm ơn và Minigame tặng sách] Em chân thành biết ơn mọi người "
        "đã tham gia cùng Tiệm sách Yêu Con, đây là review sách hay hôm nay"
    )
    result = rules.classify(full_text=text)

    assert result.tsyc_relevance == rules.TsycRelevance.HIGH
    assert result.post_type == rules.PostType.BOOK_REVIEW
    assert result.needs_secondary_review is False
    assert semantic.route_record(result) == semantic.RoutingDecision.BYPASS_CONFIDENT_EXCLUDE


def test_plain_feedback_with_no_extra_evidence_routes_to_bypass_confident_exclude():
    result = rules.classify(
        full_text="Cảm ơn chị nhiều lắm ạ",
        folder_slugs=[rules.FEEDBACK_FOLDER_SLUG],
    )

    assert result.post_type == rules.PostType.CUSTOMER_FEEDBACK
    assert result.needs_secondary_review is False
    assert semantic.route_record(result) == semantic.RoutingDecision.BYPASS_CONFIDENT_EXCLUDE


def test_feedback_with_solicitation_language_routes_to_semantic():
    result = rules.classify(
        full_text="mn cần đặt sách mới cứ nhắn cho Tiệm sách Yêu Con nhé",
        folder_slugs=[rules.FEEDBACK_FOLDER_SLUG],
    )

    assert result.post_type == rules.PostType.CUSTOMER_FEEDBACK
    assert result.needs_secondary_review is True
    assert semantic.route_record(result) == semantic.RoutingDecision.SEND_TO_SEMANTIC


def test_medium_ambiguous_promotion_routes_to_semantic():
    result = rules.classify(full_text="Thanh lý sách cũ giá 8€ một cuốn, ai cần inbox em nhé")

    assert result.tsyc_relevance == rules.TsycRelevance.MEDIUM
    assert result.candidate_eligible is True
    assert result.needs_secondary_review is True
    assert semantic.route_record(result) == semantic.RoutingDecision.SEND_TO_SEMANTIC


def test_medium_commerce_only_routes_to_semantic():
    result = rules.classify(full_text="Giá cả linh tinh, tăng giảm thất thường suốt tuần qua")

    assert result.candidate_eligible is False
    assert result.needs_secondary_review is True
    assert semantic.route_record(result) == semantic.RoutingDecision.SEND_TO_SEMANTIC


def test_negative_business_excluded_routes_to_semantic():
    text = "[CHƯƠNG TRÌNH DÀNH TẶNG KHÁCH HÀNG CỦA ZEUS TEAM] sách hay giá tốt chỉ 20€ hôm nay thôi"
    result = rules.classify(full_text=text)

    assert result.classification_reason == rules.REASON_NEGATIVE_BUSINESS_EXCLUSION
    assert result.needs_secondary_review is True
    assert semantic.route_record(result) == semantic.RoutingDecision.SEND_TO_SEMANTIC


def test_route_record_is_pure_and_idempotent():
    result = rules.classify(full_text="Sách có sẵn tại Đức")

    first = semantic.route_record(result)
    second = semantic.route_record(result)

    assert first == second


# --- synthesize_final_decision() -------------------------------------------


def _det(**overrides) -> rules.ClassificationResult:
    defaults = dict(
        tsyc_relevance=rules.TsycRelevance.MEDIUM,
        post_type=rules.PostType.PROMOTION,
        candidate_eligible=True,
        classification_reason=rules.REASON_WEAK_LISTING_ELIGIBLE,
        needs_secondary_review=True,
    )
    defaults.update(overrides)
    return rules.ClassificationResult(**defaults)


def test_bypass_strong_include_synthesizes_include():
    det = _det(
        tsyc_relevance=rules.TsycRelevance.HIGH,
        post_type=rules.PostType.PRODUCT_POST,
        candidate_eligible=True,
        classification_reason=rules.REASON_STRONG_LISTING,
        needs_secondary_review=False,
    )

    final = semantic.synthesize_final_decision(
        record_id=1,
        deterministic=det,
        routing_decision=semantic.RoutingDecision.BYPASS_STRONG_INCLUDE,
        semantic=None,
    )

    assert final.final_migration_decision == semantic.FinalDecision.INCLUDE
    assert final.decision_source == semantic.DECISION_SOURCE_DETERMINISTIC_STRONG
    assert final.semantic is None
    assert final.deterministic is det


def test_skip_low_synthesizes_exclude():
    det = _det(
        tsyc_relevance=rules.TsycRelevance.LOW,
        post_type=rules.PostType.PERSONAL,
        candidate_eligible=False,
        classification_reason=rules.REASON_NO_EVIDENCE,
        needs_secondary_review=False,
    )

    final = semantic.synthesize_final_decision(
        record_id=2,
        deterministic=det,
        routing_decision=semantic.RoutingDecision.SKIP_LOW,
        semantic=None,
    )

    assert final.final_migration_decision == semantic.FinalDecision.EXCLUDE
    assert final.decision_source == semantic.DECISION_SOURCE_DETERMINISTIC_LOW


def test_bypass_confident_exclude_synthesizes_exclude():
    det = _det(
        tsyc_relevance=rules.TsycRelevance.HIGH,
        post_type=rules.PostType.BOOK_REVIEW,
        candidate_eligible=False,
        classification_reason=rules.REASON_STRONG_REVIEW,
        needs_secondary_review=False,
    )

    final = semantic.synthesize_final_decision(
        record_id=3,
        deterministic=det,
        routing_decision=semantic.RoutingDecision.BYPASS_CONFIDENT_EXCLUDE,
        semantic=None,
    )

    assert final.final_migration_decision == semantic.FinalDecision.EXCLUDE
    assert final.decision_source == semantic.DECISION_SOURCE_DETERMINISTIC_CONFIDENT


def test_semantic_high_confidence_relevant_synthesizes_include():
    det = _det()
    sem = semantic.SemanticClassificationResult(
        semantic_post_type=rules.PostType.PRODUCT_POST,
        product_migration_relevant=True,
        confidence=0.9,
        reason_codes=("BOOK_SPECIFIC_AND_COMMERCE_EVIDENCE_CONFIRMED",),
    )

    final = semantic.synthesize_final_decision(
        record_id=4,
        deterministic=det,
        routing_decision=semantic.RoutingDecision.SEND_TO_SEMANTIC,
        semantic=sem,
    )

    assert final.final_migration_decision == semantic.FinalDecision.INCLUDE
    assert final.decision_source == semantic.DECISION_SOURCE_SEMANTIC
    assert final.semantic is sem


def test_semantic_high_confidence_not_relevant_synthesizes_exclude():
    det = _det()
    sem = semantic.SemanticClassificationResult(
        semantic_post_type=rules.PostType.GENERAL_BUSINESS,
        product_migration_relevant=False,
        confidence=0.8,
        reason_codes=("CURRENCY_CONVERSION_CONTEXT_NOT_A_LISTING",),
    )

    final = semantic.synthesize_final_decision(
        record_id=5,
        deterministic=det,
        routing_decision=semantic.RoutingDecision.SEND_TO_SEMANTIC,
        semantic=sem,
    )

    assert final.final_migration_decision == semantic.FinalDecision.EXCLUDE
    assert final.decision_source == semantic.DECISION_SOURCE_SEMANTIC


def test_semantic_low_confidence_synthesizes_review_required():
    det = _det()
    sem = semantic.SemanticClassificationResult(
        semantic_post_type=rules.PostType.PROMOTION,
        product_migration_relevant=True,
        confidence=0.55,
        reason_codes=("PRICED_ITEM_LIST_WITHOUT_CONFIRMED_BOOK_VOCABULARY",),
    )

    final = semantic.synthesize_final_decision(
        record_id=6,
        deterministic=det,
        routing_decision=semantic.RoutingDecision.SEND_TO_SEMANTIC,
        semantic=sem,
    )

    assert final.final_migration_decision == semantic.FinalDecision.REVIEW_REQUIRED
    assert final.decision_source == semantic.DECISION_SOURCE_SEMANTIC_LOW_CONFIDENCE


def test_confidence_threshold_is_configurable_not_hardcoded():
    det = _det()
    sem = semantic.SemanticClassificationResult(
        semantic_post_type=rules.PostType.PRODUCT_POST,
        product_migration_relevant=True,
        confidence=0.6,
        reason_codes=("X",),
    )

    default_final = semantic.synthesize_final_decision(
        record_id=7,
        deterministic=det,
        routing_decision=semantic.RoutingDecision.SEND_TO_SEMANTIC,
        semantic=sem,
    )
    assert default_final.final_migration_decision == semantic.FinalDecision.REVIEW_REQUIRED

    lowered_threshold_final = semantic.synthesize_final_decision(
        record_id=7,
        deterministic=det,
        routing_decision=semantic.RoutingDecision.SEND_TO_SEMANTIC,
        semantic=sem,
        high_confidence_threshold=0.5,
    )
    assert lowered_threshold_final.final_migration_decision == semantic.FinalDecision.INCLUDE


def test_send_to_semantic_without_semantic_result_raises():
    det = _det()

    with pytest.raises(ValueError):
        semantic.synthesize_final_decision(
            record_id=8,
            deterministic=det,
            routing_decision=semantic.RoutingDecision.SEND_TO_SEMANTIC,
            semantic=None,
        )


def test_synthesize_final_decision_never_mutates_deterministic_result():
    det = _det()
    original_reason = det.classification_reason

    semantic.synthesize_final_decision(
        record_id=9,
        deterministic=det,
        routing_decision=semantic.RoutingDecision.BYPASS_STRONG_INCLUDE,
        semantic=None,
    )

    assert det.classification_reason == original_reason


def test_synthesize_final_decision_is_pure_and_idempotent():
    det = _det()
    sem = semantic.SemanticClassificationResult(
        semantic_post_type=rules.PostType.PRODUCT_POST,
        product_migration_relevant=True,
        confidence=0.9,
    )

    first = semantic.synthesize_final_decision(
        record_id=10,
        deterministic=det,
        routing_decision=semantic.RoutingDecision.SEND_TO_SEMANTIC,
        semantic=sem,
    )
    second = semantic.synthesize_final_decision(
        record_id=10,
        deterministic=det,
        routing_decision=semantic.RoutingDecision.SEND_TO_SEMANTIC,
        semantic=sem,
    )

    assert first == second


def test_semantic_classification_result_rejects_bad_confidence():
    with pytest.raises(ValueError):
        semantic.SemanticClassificationResult(
            semantic_post_type=rules.PostType.PRODUCT_POST,
            product_migration_relevant=True,
            confidence=1.5,
        )


def test_semantic_classification_result_rejects_unknown_post_type():
    with pytest.raises(ValueError):
        semantic.SemanticClassificationResult(
            semantic_post_type="NOT_A_TYPE",
            product_migration_relevant=True,
            confidence=0.5,
        )
