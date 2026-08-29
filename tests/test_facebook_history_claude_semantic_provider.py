"""Offline tests for ClaudeHistoricalSemanticProvider
(src/services/facebook_history_semantic_provider.py).

No live Anthropic API call anywhere in this file -- every test injects a
fake `client` object (or, for cache-only tests, no client at all) via
ClaudeHistoricalSemanticProvider's `client=` constructor parameter.
ANTHROPIC_API_KEY is never read or required here.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError

import anthropic

from src.domain.rules.facebook_history_classification import (
    FEEDBACK_FOLDER_SLUG,
    PostType,
    REASON_STRONG_LISTING,
    REASON_STRONG_REVIEW,
    STRONG_LISTING_FOLDER_SLUG,
    classify,
)
from src.domain.rules.facebook_history_semantic import (
    FinalDecision,
    RoutingDecision,
    SemanticClassificationInput,
    route_record,
    synthesize_final_decision,
)
from src.services.facebook_history_parser import HistoryRecord
from src.services.facebook_history_report import classify_records
from src.services.facebook_history_secondary_classification import run_secondary_classification
from src.services.facebook_history_semantic_cache import (
    SemanticResultCache,
    compute_cache_key,
    compute_input_hash,
)
from src.services.facebook_history_semantic_provider import (
    PROMPT_VERSION,
    PROVIDER_NAME,
    ClaudeHistoricalSemanticProvider,
    ClaudeProviderConfigurationError,
    _ClaudeSemanticResponseSchema,
)


# --- test doubles ------------------------------------------------------------


class _FakeMessages:
    """Stands in for client.messages -- .parse() replays canned
    responses/exceptions in order, recording every call it received."""

    def __init__(self, responses: list) -> None:
        self._responses = list(responses)
        self.calls: list[dict] = []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        if not self._responses:
            raise AssertionError("Fake Claude client called more times than expected")
        item = self._responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class _FakeClient:
    def __init__(self, responses: list) -> None:
        self.messages = _FakeMessages(responses)


class _ExplodingMessages:
    """Raises loudly if ever called -- proves a bypassed record never
    reaches the classifier at all."""

    def parse(self, **kwargs):
        raise AssertionError("ClaudeHistoricalSemanticProvider was called but should have been bypassed")


class _ExplodingClient:
    def __init__(self) -> None:
        self.messages = _ExplodingMessages()


def _fake_parsed_response(**schema_fields) -> SimpleNamespace:
    schema = _ClaudeSemanticResponseSchema(**schema_fields)
    return SimpleNamespace(parsed_output=schema, stop_reason="end_turn")


def _fake_request() -> httpx.Request:
    return httpx.Request("POST", "https://api.anthropic.com/v1/messages")


def _fake_status_error(cls, status_code: int) -> Exception:
    return cls(f"fake {cls.__name__}", response=httpx.Response(status_code, request=_fake_request()), body=None)


def _request(**overrides) -> SemanticClassificationInput:
    defaults = dict(
        record_id=1,
        date_text="Tháng 6 09, 2025 12:25:09 ch",
        full_text="Thanh lý sách cũ giá 8€ một cuốn, ai cần inbox em nhé",
        heading="Tâm Võ đã thêm một ảnh mới.",
        strong_markers=(),
        weak_markers=("sách", "giá"),
        folder_slug_evidence=(),
        structural_mention_id=None,
        local_image_count=1,
        local_video_count=0,
        deterministic_tsyc_relevance="MEDIUM",
        deterministic_post_type=PostType.PROMOTION,
        deterministic_candidate_eligible=True,
        deterministic_classification_reason="test",
    )
    defaults.update(overrides)
    return SemanticClassificationInput(**defaults)


def _provider(responses=None, **overrides) -> ClaudeHistoricalSemanticProvider:
    client = overrides.pop("client", None) or _FakeClient(responses or [])
    kwargs = dict(client=client, cache_dir=None, sleep=lambda _seconds: None)
    kwargs.update(overrides)
    return ClaudeHistoricalSemanticProvider(**kwargs)


# --- construction / configuration -------------------------------------------


def test_missing_api_key_raises_without_falling_back_to_mock(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    with pytest.raises(ClaudeProviderConfigurationError):
        ClaudeHistoricalSemanticProvider()


def test_explicit_client_bypasses_api_key_requirement(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    provider = ClaudeHistoricalSemanticProvider(client=_FakeClient([]), cache_dir=None)
    assert provider.model  # constructed without raising


def test_default_model_is_centralized_constant():
    provider = _provider()
    from src.services.facebook_history_semantic_provider import DEFAULT_MODEL

    assert provider.model == DEFAULT_MODEL


def test_model_is_configurable_via_env_var(monkeypatch):
    monkeypatch.setenv("TSYC_CLAUDE_SEMANTIC_MODEL", "claude-sonnet-5")

    provider = _provider()

    assert provider.model == "claude-sonnet-5"


# --- required scenario: successful structured response ----------------------


def test_successful_structured_response_is_returned_and_cached(tmp_path: Path):
    fake = _FakeClient(
        [
            _fake_parsed_response(
                semantic_post_type=PostType.PRODUCT_POST,
                product_migration_relevant=True,
                confidence=0.9,
                reason_codes=["CONCRETE_LISTING_WITH_PRICE"],
                extracted_product_hints=["8€"],
            )
        ]
    )
    provider = ClaudeHistoricalSemanticProvider(client=fake, cache_dir=tmp_path, sleep=lambda _s: None)

    result, provenance = provider.classify_with_provenance(_request())

    assert result.semantic_post_type == PostType.PRODUCT_POST
    assert result.product_migration_relevant is True
    assert result.confidence == 0.9
    assert result.reason_codes == ("CONCRETE_LISTING_WITH_PRICE",)
    assert result.extracted_product_hints == ("8€",)
    assert provenance.provider == PROVIDER_NAME
    assert provenance.cache_hit is False
    assert len(fake.messages.calls) == 1


# --- required scenario: malformed JSON / invalid enum / bad confidence ------


def test_malformed_json_routes_to_low_confidence_not_include():
    fake = _FakeClient([ValidationError.from_exception_data("test", [])])
    provider = _provider(client=fake)

    result = provider.classify(_request())

    assert result.product_migration_relevant is False
    assert result.confidence == 0.0
    assert "CLAUDE_OUTPUT_VALIDATION_FAILED" in result.reason_codes


def test_invalid_enum_value_routes_to_low_confidence_not_include():
    def _raise_invalid_enum(**_kwargs):
        _ClaudeSemanticResponseSchema.model_validate(
            {"semantic_post_type": "NOT_A_REAL_TYPE", "product_migration_relevant": True, "confidence": 0.9}
        )

    class _RaisingMessages:
        def parse(self, **kwargs):
            _raise_invalid_enum(**kwargs)

    provider = _provider(client=SimpleNamespace(messages=_RaisingMessages()))

    result = provider.classify(_request())

    assert result.product_migration_relevant is False
    assert result.confidence == 0.0
    assert "CLAUDE_OUTPUT_VALIDATION_FAILED" in result.reason_codes


def test_confidence_outside_range_routes_to_low_confidence_not_include():
    def _raise_bad_confidence(**_kwargs):
        _ClaudeSemanticResponseSchema.model_validate(
            {"semantic_post_type": PostType.PRODUCT_POST, "product_migration_relevant": True, "confidence": 1.7}
        )

    class _RaisingMessages:
        def parse(self, **kwargs):
            _raise_bad_confidence(**kwargs)

    provider = _provider(client=SimpleNamespace(messages=_RaisingMessages()))

    result = provider.classify(_request())

    assert result.product_migration_relevant is False
    assert result.confidence == 0.0
    assert "CLAUDE_OUTPUT_VALIDATION_FAILED" in result.reason_codes


def test_malformed_output_final_decision_is_never_include():
    fake = _FakeClient([ValidationError.from_exception_data("test", [])])
    provider = _provider(client=fake)
    det = classify(full_text=_request().full_text)

    semantic = provider.classify(_request())
    final = synthesize_final_decision(
        record_id=1,
        deterministic=det,
        routing_decision=RoutingDecision.SEND_TO_SEMANTIC,
        semantic=semantic,
    )

    assert final.final_migration_decision != FinalDecision.INCLUDE


def test_malformed_output_is_never_cached(tmp_path: Path):
    fake = _FakeClient(
        [
            ValidationError.from_exception_data("test", []),
            _fake_parsed_response(
                semantic_post_type=PostType.PRODUCT_POST,
                product_migration_relevant=True,
                confidence=0.9,
            ),
        ]
    )
    provider = ClaudeHistoricalSemanticProvider(client=fake, cache_dir=tmp_path, sleep=lambda _s: None)
    request = _request()

    first, first_provenance = provider.classify_with_provenance(request)
    assert first.confidence == 0.0
    assert first_provenance.cache_hit is False

    # A second call for the *same* request must NOT hit a cached
    # malformed result -- it must call the API again.
    second, second_provenance = provider.classify_with_provenance(request)
    assert second.product_migration_relevant is True
    assert second_provenance.cache_hit is False
    assert len(fake.messages.calls) == 2


# --- required scenario: transient failure + bounded retry -------------------


def test_transient_failure_then_success_is_retried_within_bound():
    sleeps: list[float] = []
    fake = _FakeClient(
        [
            _fake_status_error(anthropic.RateLimitError, 429),
            _fake_parsed_response(
                semantic_post_type=PostType.PRODUCT_POST,
                product_migration_relevant=True,
                confidence=0.9,
            ),
        ]
    )
    provider = _provider(client=fake, max_attempts=3, sleep=sleeps.append)

    result = provider.classify(_request())

    assert result.product_migration_relevant is True
    assert len(fake.messages.calls) == 2
    assert len(sleeps) == 1  # exactly one backoff sleep before the retry


def test_retry_is_bounded_not_indefinite():
    fake = _FakeClient(
        [
            _fake_status_error(anthropic.RateLimitError, 429),
            _fake_status_error(anthropic.RateLimitError, 429),
            _fake_status_error(anthropic.RateLimitError, 429),
        ]
    )
    provider = _provider(client=fake, max_attempts=3, sleep=lambda _s: None)

    with pytest.raises(anthropic.RateLimitError):
        provider.classify(_request())

    assert len(fake.messages.calls) == 3  # exactly max_attempts, never more


# --- required scenario: permanent failure ------------------------------------


def test_permanent_failure_raises_immediately_without_retry():
    fake = _FakeClient([_fake_status_error(anthropic.BadRequestError, 400)])
    provider = _provider(client=fake, max_attempts=3, sleep=lambda _s: None)

    with pytest.raises(anthropic.BadRequestError):
        provider.classify(_request())

    assert len(fake.messages.calls) == 1  # never retried


def test_semantic_disagreement_is_not_retried():
    """A confident-but-low-relevance answer is a real answer, not a
    failure -- classify() must return it on the first call, never retry
    to "get a different answer"."""
    fake = _FakeClient(
        [
            _fake_parsed_response(
                semantic_post_type=PostType.GENERAL_BUSINESS,
                product_migration_relevant=False,
                confidence=0.9,
            )
        ]
    )
    provider = _provider(client=fake)

    result = provider.classify(_request())

    assert result.product_migration_relevant is False
    assert len(fake.messages.calls) == 1


# --- required scenario: cache hit avoids API call ----------------------------


def test_cache_hit_avoids_api_call(tmp_path: Path):
    request = _request()
    priming_provider = ClaudeHistoricalSemanticProvider(
        client=_FakeClient(
            [
                _fake_parsed_response(
                    semantic_post_type=PostType.PRODUCT_POST,
                    product_migration_relevant=True,
                    confidence=0.9,
                )
            ]
        ),
        cache_dir=tmp_path,
        sleep=lambda _s: None,
    )
    first_result, first_provenance = priming_provider.classify_with_provenance(request)
    assert first_provenance.cache_hit is False

    # A brand new provider instance, backed by an exploding client --
    # if the cache is not consulted first, this raises.
    cached_provider = ClaudeHistoricalSemanticProvider(
        client=_ExplodingClient(), cache_dir=tmp_path, sleep=lambda _s: None
    )
    second_result, second_provenance = cached_provider.classify_with_provenance(request)

    assert second_result == first_result
    assert second_provenance.cache_hit is True


# --- required scenario: same input => same cache key ------------------------


def test_same_input_produces_same_cache_key():
    request_a = _request()
    request_b = _request()  # separately constructed, equal by value

    hash_a = compute_input_hash(request_a)
    hash_b = compute_input_hash(request_b)
    assert hash_a == hash_b

    key_a = compute_cache_key(provider="claude", model="claude-opus-5", prompt_version="v1", input_hash=hash_a)
    key_b = compute_cache_key(provider="claude", model="claude-opus-5", prompt_version="v1", input_hash=hash_b)
    assert key_a == key_b


def test_different_input_produces_different_input_hash():
    hash_a = compute_input_hash(_request(full_text="Sách có sẵn tại Đức"))
    hash_b = compute_input_hash(_request(full_text="Một nội dung hoàn toàn khác"))
    assert hash_a != hash_b


# --- required scenario: changed prompt version => different cache key -------


def test_changed_prompt_version_produces_different_cache_key():
    input_hash = compute_input_hash(_request())

    key_v1 = compute_cache_key(provider="claude", model="claude-opus-5", prompt_version="v1", input_hash=input_hash)
    key_v2 = compute_cache_key(provider="claude", model="claude-opus-5", prompt_version="v2", input_hash=input_hash)

    assert key_v1 != key_v2


def test_prompt_version_is_a_centralized_constant_used_by_the_provider(tmp_path: Path):
    fake = _FakeClient(
        [
            _fake_parsed_response(
                semantic_post_type=PostType.PRODUCT_POST,
                product_migration_relevant=True,
                confidence=0.9,
            )
        ]
    )
    provider = ClaudeHistoricalSemanticProvider(client=fake, cache_dir=tmp_path, sleep=lambda _s: None)

    _result, provenance = provider.classify_with_provenance(_request())

    assert provenance.prompt_version == PROMPT_VERSION


# --- required fixture scenarios: known Vietnamese ambiguities ---------------
#
# These exercise the plumbing end-to-end (provider -> synthesize_final_
# decision) using a canned fake response representing the CORRECT judgment
# call for each named scenario -- they prove our code relays and acts on
# that judgment correctly, not that the live model reasons correctly
# (which cannot be verified offline; see the module docstring).


def test_gia_tri_dinh_duong_fixture_is_not_treated_as_commerce_sale():
    request = _request(
        full_text=(
            "Em có đọc một cơ số các sách dinh dưỡng, giá trị dinh dưỡng "
            "của từng loại sữa khác nhau nhiều lắm."
        ),
        deterministic_post_type=PostType.GENERAL_BUSINESS,
        deterministic_candidate_eligible=False,
    )
    fake = _FakeClient(
        [
            _fake_parsed_response(
                semantic_post_type=PostType.PERSONAL,
                product_migration_relevant=False,
                confidence=0.85,
                reason_codes=["VALUE_NOT_PRICE", "INCIDENTAL_BOOK_MENTION"],
            )
        ]
    )
    provider = _provider(client=fake)
    det = classify(full_text=request.full_text)

    semantic = provider.classify(request)
    final = synthesize_final_decision(
        record_id=1, deterministic=det, routing_decision=RoutingDecision.SEND_TO_SEMANTIC, semantic=semantic
    )

    assert semantic.product_migration_relevant is False
    assert final.final_migration_decision == FinalDecision.EXCLUDE


def test_ti_gia_with_incidental_book_mention_is_not_migration_relevant():
    request = _request(
        full_text=(
            "Tỉ giá giờ quá đẹp 1€=30000vnd. Tiền thu được em dồn vào mua "
            "sách thư viện nha."
        ),
        deterministic_post_type=PostType.PROMOTION,
        deterministic_candidate_eligible=True,
    )
    fake = _FakeClient(
        [
            _fake_parsed_response(
                semantic_post_type=PostType.GENERAL_BUSINESS,
                product_migration_relevant=False,
                confidence=0.85,
                reason_codes=["CURRENCY_CONVERSION_NOTE"],
            )
        ]
    )
    provider = _provider(client=fake)
    det = classify(full_text=request.full_text)

    semantic = provider.classify(request)
    final = synthesize_final_decision(
        record_id=1, deterministic=det, routing_decision=RoutingDecision.SEND_TO_SEMANTIC, semantic=semantic
    )

    assert final.final_migration_decision != FinalDecision.INCLUDE


def test_clear_tsyc_book_sale_fixture_is_relevant_and_includes():
    request = _request(full_text="Thanh lý sách cũ giá 8€ một cuốn, ai cần inbox em nhé")
    fake = _FakeClient(
        [
            _fake_parsed_response(
                semantic_post_type=PostType.PRODUCT_POST,
                product_migration_relevant=True,
                confidence=0.92,
                reason_codes=["CONCRETE_LISTING_WITH_PRICE"],
                extracted_product_hints=["8€"],
            )
        ]
    )
    provider = _provider(client=fake)
    det = classify(full_text=request.full_text)

    semantic = provider.classify(request)
    final = synthesize_final_decision(
        record_id=1, deterministic=det, routing_decision=RoutingDecision.SEND_TO_SEMANTIC, semantic=semantic
    )

    assert final.final_migration_decision == FinalDecision.INCLUDE


def test_book_review_fixture_is_not_relevant():
    request = _request(
        full_text="Review sách hay hôm nay: một cuốn sách tuyệt vời",
        deterministic_post_type=PostType.BOOK_REVIEW,
        deterministic_candidate_eligible=False,
    )
    fake = _FakeClient(
        [
            _fake_parsed_response(
                semantic_post_type=PostType.BOOK_REVIEW,
                product_migration_relevant=False,
                confidence=0.88,
                reason_codes=["REVIEW_WITHOUT_SALE_OFFER"],
            )
        ]
    )
    provider = _provider(client=fake)
    det = classify(full_text=request.full_text)

    semantic = provider.classify(request)
    final = synthesize_final_decision(
        record_id=1, deterministic=det, routing_decision=RoutingDecision.SEND_TO_SEMANTIC, semantic=semantic
    )

    assert final.final_migration_decision == FinalDecision.EXCLUDE


def test_customer_feedback_fixture_is_not_relevant():
    request = _request(
        full_text="Em biết ơn khách hàng đã luôn tin tưởng và ủng hộ, cần đặt sách mới cứ nhắn em",
        deterministic_post_type=PostType.CUSTOMER_FEEDBACK,
        deterministic_candidate_eligible=False,
    )
    fake = _FakeClient(
        [
            _fake_parsed_response(
                semantic_post_type=PostType.CUSTOMER_FEEDBACK,
                product_migration_relevant=False,
                confidence=0.9,
                reason_codes=["FEEDBACK_NOT_A_LISTING"],
            )
        ]
    )
    provider = _provider(client=fake)
    det = classify(
        full_text=request.full_text,
        folder_slugs=[FEEDBACK_FOLDER_SLUG],
    )

    semantic = provider.classify(request)
    final = synthesize_final_decision(
        record_id=1, deterministic=det, routing_decision=RoutingDecision.SEND_TO_SEMANTIC, semantic=semantic
    )

    assert final.final_migration_decision == FinalDecision.EXCLUDE


# --- required scenario: LOW / strong-bypass records never call the provider


def _record(**overrides) -> HistoryRecord:
    defaults = dict(
        record_index=1,
        date_text="Tháng 5 03, 2025 10:16:09 ch",
        heading="Tâm Võ đã thêm 3 ảnh mới.",
        full_text="Sách Có Sẵn tại Đức",
        text_preview="Sách Có Sẵn tại Đức",
        external_links=(),
        local_image_paths=(),
        local_video_paths=(),
        folder_slugs=(STRONG_LISTING_FOLDER_SLUG,),
        mention_ids=(),
        mention_names=(),
    )
    defaults.update(overrides)
    return HistoryRecord(**defaults)


def test_low_record_never_reaches_claude_provider():
    det = classify(full_text="Hội những người siêu tích cực va vào nhau 😆😆😆")
    assert route_record(det) == RoutingDecision.SKIP_LOW

    records = [_record(full_text="Hội những người siêu tích cực va vào nhau 😆😆😆", folder_slugs=())]
    classified = classify_records(records)
    provider = ClaudeHistoricalSemanticProvider(client=_ExplodingClient(), cache_dir=None)

    results = run_secondary_classification(classified, provider)  # must not raise

    assert results[0].final.final_migration_decision == FinalDecision.EXCLUDE
    assert results[0].final.semantic is None


def test_strong_deterministic_bypass_never_reaches_claude_provider():
    det = classify(full_text="Sách có sẵn tại Đức, inbox em nhé")
    assert det.classification_reason == REASON_STRONG_LISTING
    assert route_record(det) == RoutingDecision.BYPASS_STRONG_INCLUDE

    records = [_record(full_text="Sách Có Sẵn tại Đức", folder_slugs=(STRONG_LISTING_FOLDER_SLUG,))]
    classified = classify_records(records)
    provider = ClaudeHistoricalSemanticProvider(client=_ExplodingClient(), cache_dir=None)

    results = run_secondary_classification(classified, provider)  # must not raise

    assert results[0].final.final_migration_decision == FinalDecision.INCLUDE
    assert results[0].final.semantic is None


def test_confident_exclude_bypass_never_reaches_claude_provider():
    text = (
        "[Lời cảm ơn và Minigame tặng sách] Em chân thành biết ơn mọi người "
        "đã tham gia cùng Tiệm sách Yêu Con, đây là review sách hay hôm nay"
    )
    det = classify(full_text=text)
    assert det.classification_reason == REASON_STRONG_REVIEW
    assert route_record(det) == RoutingDecision.BYPASS_CONFIDENT_EXCLUDE

    records = [_record(full_text=text, folder_slugs=())]
    classified = classify_records(records)
    provider = ClaudeHistoricalSemanticProvider(client=_ExplodingClient(), cache_dir=None)

    results = run_secondary_classification(classified, provider)  # must not raise

    assert results[0].final.final_migration_decision == FinalDecision.EXCLUDE
    assert results[0].final.semantic is None
