"""Offline tests for src/services/facebook_history_semantic_provider.py.

No live Supabase/WooCommerce/Facebook/Claude dependency. Covers the
required scenarios for MockHistoricalSemanticProvider, a "no network
access" regression test, and one safety pin on ClaudeHistoricalSemantic
Provider (construction without credentials must fail safely, never fall
back to acting like the mock) -- see tests/test_facebook_history_claude_
semantic_provider.py for ClaudeHistoricalSemanticProvider's full
behavioral test suite.
"""
from __future__ import annotations

import socket

import pytest

from src.domain.rules.facebook_history_classification import (
    PostType,
    REASON_NEGATIVE_BUSINESS_EXCLUSION,
    REASON_STRONG_REVIEW,
    REASON_WEAK_COMMERCE_ONLY_NOT_ELIGIBLE,
    REASON_WEAK_LISTING_ELIGIBLE,
    REASON_WEAK_REVIEW_FOLDER,
    classify,
)
from src.domain.rules.facebook_history_semantic import SemanticClassificationInput
from src.services.facebook_history_semantic_provider import (
    ClaudeHistoricalSemanticProvider,
    ClaudeProviderConfigurationError,
    MockHistoricalSemanticProvider,
)


def _request(full_text: str, **det_overrides) -> SemanticClassificationInput:
    """Build a SemanticClassificationInput the same way the real
    orchestrator (src.services.facebook_history_secondary_classification.
    run_secondary_classification) would -- by first running the actual
    deterministic classifier, then wrapping its result."""
    det = classify(full_text=full_text)
    fields = dict(
        record_id=1,
        date_text="Tháng 6 09, 2025 12:25:09 ch",
        full_text=full_text,
        heading="Tâm Võ đã thêm một ảnh mới.",
        strong_markers=det.strong_markers,
        weak_markers=det.weak_markers,
        folder_slug_evidence=det.folder_slug_evidence,
        structural_mention_id=det.structural_mention_id,
        local_image_count=1,
        local_video_count=0,
        deterministic_tsyc_relevance=det.tsyc_relevance,
        deterministic_post_type=det.post_type,
        deterministic_candidate_eligible=det.candidate_eligible,
        deterministic_classification_reason=det.classification_reason,
    )
    fields.update(det_overrides)
    return SemanticClassificationInput(**fields)


# --- required scenario: #1267-style currency-conversion note -------------


def test_currency_conversion_note_with_incidental_sach_is_not_relevant():
    text = (
        "Bác nào gửi tiền về Việt Nam mà gửi ít ít nhỏ lẻ thì dùng Tap Tap "
        "Send được nè. Tỉ giá giờ quá đẹp🤩🤩🤩1€=30000vnd. Tiền thu được "
        "em dồn vào mua sách thư viện nha."
    )
    request = _request(text)
    result = MockHistoricalSemanticProvider().classify(request)

    assert result.product_migration_relevant is False
    assert "CURRENCY_CONVERSION_CONTEXT_NOT_A_LISTING" in result.reason_codes
    # And end to end: this must never auto-include.
    assert not (result.product_migration_relevant and result.confidence >= 0.75)


# --- required scenario: clear book sale => relevant, high confidence -----


def test_clear_book_sale_is_relevant_with_high_confidence():
    text = "Thanh lý sách cũ giá 8€ một cuốn, ai cần inbox em nhé"
    request = _request(text)
    result = MockHistoricalSemanticProvider().classify(request)

    assert request.deterministic_post_type == PostType.PROMOTION
    assert request.deterministic_candidate_eligible is True
    assert result.product_migration_relevant is True
    assert result.confidence >= 0.75
    assert result.semantic_post_type == PostType.PRODUCT_POST


# --- required scenario: customer feedback => not relevant -----------------


def test_customer_feedback_is_not_relevant():
    request = _request(
        "mn cần đặt sách mới cứ nhắn cho Tiệm sách Yêu Con nhé",
        deterministic_post_type=PostType.CUSTOMER_FEEDBACK,
    )
    result = MockHistoricalSemanticProvider().classify(request)

    assert result.product_migration_relevant is False
    assert result.confidence >= 0.75
    assert result.reason_codes == ("FEEDBACK_CLASSIFICATION_TRUSTED",)


# --- required scenario: book review without sales intent => not relevant -


def test_book_review_is_not_relevant():
    request = _request(
        "Review sách hay hôm nay: một cuốn sách tuyệt vời",
        deterministic_post_type=PostType.BOOK_REVIEW,
    )
    result = MockHistoricalSemanticProvider().classify(request)

    assert result.product_migration_relevant is False
    assert result.confidence >= 0.75
    assert result.semantic_post_type == PostType.BOOK_REVIEW


# --- required scenario: ambiguous promotion => low confidence ------------


def test_ambiguous_priced_list_without_book_vocabulary_is_low_confidence():
    text = (
        "Thanh lý:\n"
        "1. Đạo – Con đường không lối 8€\n"
        "2. Giác ngộ 9€\n"
        "3. Tự tôn 9€\n"
    )
    request = _request(text)

    assert request.deterministic_candidate_eligible is False
    assert request.deterministic_classification_reason == REASON_WEAK_COMMERCE_ONLY_NOT_ELIGIBLE

    result = MockHistoricalSemanticProvider().classify(request)

    # Low enough that synthesize_final_decision() would land on
    # REVIEW_REQUIRED, not a silent auto-decision either way.
    assert result.confidence < 0.75


# --- provider never makes a network call ----------------------------------


def test_mock_provider_never_touches_the_network(monkeypatch):
    def _blow_up(*args, **kwargs):
        raise AssertionError("MockHistoricalSemanticProvider attempted a network call")

    monkeypatch.setattr(socket, "socket", _blow_up)
    monkeypatch.setattr(socket, "create_connection", _blow_up)

    provider = MockHistoricalSemanticProvider()
    texts = [
        "Sách có sẵn tại Đức",
        "Thanh lý sách cũ giá 8€ một cuốn",
        "Review sách hay hôm nay",
        "Tỉ giá giờ quá đẹp 1€=30000vnd, mua sách thư viện",
        "Giá cả linh tinh",
        "",
    ]
    for text in texts:
        provider.classify(_request(text))
    # No AssertionError raised above means no network call was attempted.


# --- deterministic / idempotent --------------------------------------------


def test_mock_provider_is_deterministic_and_idempotent():
    provider = MockHistoricalSemanticProvider()
    request = _request("Thanh lý sách cũ giá 8€ một cuốn, ai cần inbox em nhé")

    first = provider.classify(request)
    second = provider.classify(request)

    assert first == second


# --- Claude provider: construction without credentials fails safely -------
#
# The real ClaudeHistoricalSemanticProvider implementation (and its full
# behavioral test suite -- successful/malformed/retry/cache/routing-
# bypass scenarios, all via fake clients, no live API calls) lives in
# tests/test_facebook_history_claude_semantic_provider.py. This module
# only pins the one safety property relevant to *this* file's scope: it
# must never silently construct successfully (and fall back to acting
# like the mock) when no credentials are configured.


def test_claude_provider_without_credentials_fails_safely(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    with pytest.raises(ClaudeProviderConfigurationError):
        ClaudeHistoricalSemanticProvider()
