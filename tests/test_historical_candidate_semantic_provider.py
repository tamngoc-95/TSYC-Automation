"""Offline tests for ClaudeHistoricalCandidateProvider
(src/services/historical_candidate_semantic_provider.py).

No live Anthropic API call anywhere in this file -- every test injects
a fake `client` object (or, for cache-only tests, no client at all) via
the constructor's `client=` parameter. ANTHROPIC_API_KEY is never read
or required here.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import anthropic
import httpx
import pytest
from pydantic import ValidationError

from src.domain.rules.historical_candidate_semantic import (
    BOOK_COMBO,
    MULTIPLE_BOOKS,
    NO_IDENTIFIABLE_PRODUCT,
    SINGLE_BOOK,
    REASON_IMAGE_REVIEW_REQUIRED,
    CandidateExtractionInput,
    validate_and_gate,
)
from src.services.historical_candidate_semantic_cache import compute_cache_key, compute_input_hash
from src.services.historical_candidate_semantic_provider import (
    PROMPT_VERSION,
    PROVIDER_NAME,
    ClaudeCandidateProviderConfigurationError,
    ClaudeHistoricalCandidateProvider,
    MockHistoricalCandidateProvider,
    _ClaudeCandidateResponseSchema,
)


# --- test doubles ------------------------------------------------------------


class _FakeMessages:
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
    def parse(self, **kwargs):
        raise AssertionError("Provider was called but should have been served from cache")


class _ExplodingClient:
    def __init__(self) -> None:
        self.messages = _ExplodingMessages()


def _fake_parsed_response(**schema_fields) -> SimpleNamespace:
    # candidate_list_complete defaults to True here ONLY as a test-
    # fixture convenience for the many tests below that aren't about
    # completeness at all -- the PRODUCTION schema itself has no such
    # default (it is a required field); see test_candidate_list_
    # complete_is_required_on_new_responses for that production
    # behavior tested directly against the real schema.
    schema_fields.setdefault("candidate_list_complete", True)
    schema = _ClaudeCandidateResponseSchema(**schema_fields)
    return SimpleNamespace(parsed_output=schema, stop_reason="end_turn")


def _fake_request() -> httpx.Request:
    return httpx.Request("POST", "https://api.anthropic.com/v1/messages")


def _fake_status_error(cls, status_code: int) -> Exception:
    return cls(f"fake {cls.__name__}", response=httpx.Response(status_code, request=_fake_request()), body=None)


def _input(**overrides) -> CandidateExtractionInput:
    defaults = dict(
        record_id=1,
        cleaned_text="Nhà Giả Kim của Paulo Coelho, giá 12€.",
        date_text="Tháng 6 09, 2025 12:25:09 ch",
        local_image_paths=(),
        local_video_paths=(),
        deterministic_review_reasons=("A multi-item list was detected but fewer than two items yielded a reliable title.",),
        semantic_post_type="PRODUCT_POST",
        semantic_extracted_product_hints=(),
        non_book_hints=(),
    )
    defaults.update(overrides)
    return CandidateExtractionInput(**defaults)


def _provider(responses=None, **overrides) -> ClaudeHistoricalCandidateProvider:
    client = overrides.pop("client", None) or _FakeClient(responses or [])
    kwargs = dict(client=client, cache_dir=None, sleep=lambda _seconds: None)
    kwargs.update(overrides)
    return ClaudeHistoricalCandidateProvider(**kwargs)


# --- construction / configuration -------------------------------------------


def test_missing_api_key_raises_without_falling_back_to_mock(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    with pytest.raises(ClaudeCandidateProviderConfigurationError):
        ClaudeHistoricalCandidateProvider()


def test_explicit_client_bypasses_api_key_requirement(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    provider = ClaudeHistoricalCandidateProvider(client=_FakeClient([]), cache_dir=None)
    assert provider.model


def test_default_model_is_centralized_constant():
    provider = _provider()
    from src.services.historical_candidate_semantic_provider import DEFAULT_MODEL

    assert provider.model == DEFAULT_MODEL


def test_prompt_version_is_distinct_from_classification_layer():
    from src.services.facebook_history_semantic_provider import PROMPT_VERSION as CLASSIFICATION_PROMPT_VERSION

    assert PROMPT_VERSION != CLASSIFICATION_PROMPT_VERSION
    assert PROMPT_VERSION == "tsyc-fb-candidate-extraction-v1"


def test_cache_dir_is_distinct_from_classification_layer():
    from src.services.facebook_history_semantic_provider import DEFAULT_CACHE_DIR as CLASSIFICATION_CACHE_DIR
    from src.services.historical_candidate_semantic_provider import DEFAULT_CACHE_DIR as CANDIDATE_CACHE_DIR

    assert CANDIDATE_CACHE_DIR != CLASSIFICATION_CACHE_DIR


# --- required scenario: successful structured response + cache --------------


def test_successful_structured_response_is_returned_and_cached(tmp_path: Path):
    fake = _FakeClient(
        [
            _fake_parsed_response(
                post_product_type=SINGLE_BOOK,
                candidates=[
                    {
                        "title_raw": "Nhà Giả Kim",
                        "candidate_type": SINGLE_BOOK,
                        "evidence_text": "Nhà Giả Kim của Paulo Coelho",
                        "confidence": 0.9,
                    }
                ],
                confidence=0.9,
            )
        ]
    )
    provider = ClaudeHistoricalCandidateProvider(client=fake, cache_dir=tmp_path, sleep=lambda _s: None)

    result, provenance = provider.extract_with_provenance(_input())

    assert result.post_product_type == SINGLE_BOOK
    assert result.candidates[0].title_raw == "Nhà Giả Kim"
    assert provenance.provider == PROVIDER_NAME
    assert provenance.cache_hit is False
    assert len(fake.messages.calls) == 1


# --- required scenario: malformed JSON / invalid enum / bad confidence ------


def test_malformed_json_routes_to_no_identifiable_product():
    fake = _FakeClient([ValidationError.from_exception_data("test", [])])
    provider = _provider(client=fake)

    result = provider.extract(_input())

    assert result.post_product_type == NO_IDENTIFIABLE_PRODUCT
    assert result.candidates == ()
    assert result.confidence == 0.0
    assert "CLAUDE_OUTPUT_VALIDATION_FAILED" in result.review_reason_codes


def test_invalid_enum_value_routes_to_no_identifiable_product():
    class _RaisingMessages:
        def parse(self, **kwargs):
            _ClaudeCandidateResponseSchema.model_validate(
                {"post_product_type": "NOT_A_REAL_TYPE", "confidence": 0.9}
            )

    provider = _provider(client=SimpleNamespace(messages=_RaisingMessages()))

    result = provider.extract(_input())

    assert result.post_product_type == NO_IDENTIFIABLE_PRODUCT
    assert result.confidence == 0.0


def test_confidence_outside_range_routes_to_no_identifiable_product():
    class _RaisingMessages:
        def parse(self, **kwargs):
            _ClaudeCandidateResponseSchema.model_validate(
                {"post_product_type": SINGLE_BOOK, "confidence": 1.7}
            )

    provider = _provider(client=SimpleNamespace(messages=_RaisingMessages()))

    result = provider.extract(_input())

    assert result.post_product_type == NO_IDENTIFIABLE_PRODUCT


def test_malformed_output_never_becomes_auto_pass_through_gate():
    fake = _FakeClient([ValidationError.from_exception_data("test", [])])
    provider = _provider(client=fake)
    request = _input()

    raw = provider.extract(request)
    outcome, _sanitized = validate_and_gate(raw, request)

    assert outcome == "REVIEW_REQUIRED"


def test_malformed_output_is_never_cached(tmp_path: Path):
    fake = _FakeClient(
        [
            ValidationError.from_exception_data("test", []),
            _fake_parsed_response(post_product_type=NO_IDENTIFIABLE_PRODUCT, confidence=0.3),
        ]
    )
    provider = ClaudeHistoricalCandidateProvider(client=fake, cache_dir=tmp_path, sleep=lambda _s: None)
    request = _input()

    first, first_provenance = provider.extract_with_provenance(request)
    assert "CLAUDE_OUTPUT_VALIDATION_FAILED" in first.review_reason_codes
    assert first_provenance.cache_hit is False

    second, second_provenance = provider.extract_with_provenance(request)
    assert "CLAUDE_OUTPUT_VALIDATION_FAILED" not in second.review_reason_codes
    assert second_provenance.cache_hit is False
    assert len(fake.messages.calls) == 2


# --- required scenario: transient failure + bounded retry -------------------


def test_transient_failure_then_success_is_retried_within_bound():
    sleeps: list[float] = []
    fake = _FakeClient(
        [
            _fake_status_error(anthropic.RateLimitError, 429),
            _fake_parsed_response(post_product_type=NO_IDENTIFIABLE_PRODUCT, confidence=0.3),
        ]
    )
    provider = _provider(client=fake, max_attempts=3, sleep=sleeps.append)

    result = provider.extract(_input())

    assert result.post_product_type == NO_IDENTIFIABLE_PRODUCT
    assert len(fake.messages.calls) == 2
    assert len(sleeps) == 1


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
        provider.extract(_input())

    assert len(fake.messages.calls) == 3


# --- required scenario: permanent failure ------------------------------------


def test_permanent_failure_raises_immediately_without_retry():
    fake = _FakeClient([_fake_status_error(anthropic.BadRequestError, 400)])
    provider = _provider(client=fake, max_attempts=3, sleep=lambda _s: None)

    with pytest.raises(anthropic.BadRequestError):
        provider.extract(_input())

    assert len(fake.messages.calls) == 1


# --- required scenario: cache hit avoids API call ----------------------------


def test_cache_hit_avoids_api_call(tmp_path: Path):
    request = _input()
    priming = ClaudeHistoricalCandidateProvider(
        client=_FakeClient(
            [_fake_parsed_response(post_product_type=NO_IDENTIFIABLE_PRODUCT, confidence=0.3)]
        ),
        cache_dir=tmp_path,
        sleep=lambda _s: None,
    )
    first_result, first_provenance = priming.extract_with_provenance(request)
    assert first_provenance.cache_hit is False

    cached_provider = ClaudeHistoricalCandidateProvider(
        client=_ExplodingClient(), cache_dir=tmp_path, sleep=lambda _s: None
    )
    second_result, second_provenance = cached_provider.extract_with_provenance(request)

    assert second_result == first_result
    assert second_provenance.cache_hit is True


def test_same_input_produces_same_cache_key():
    hash_a = compute_input_hash(_input())
    hash_b = compute_input_hash(_input())
    assert hash_a == hash_b

    key_a = compute_cache_key(
        provider="claude", model="claude-opus-5", prompt_version="v1", schema_version="v1", input_hash=hash_a
    )
    key_b = compute_cache_key(
        provider="claude", model="claude-opus-5", prompt_version="v1", schema_version="v1", input_hash=hash_b
    )
    assert key_a == key_b


def test_different_cleaned_text_produces_different_input_hash():
    hash_a = compute_input_hash(_input(cleaned_text="Nhà Giả Kim của Paulo Coelho"))
    hash_b = compute_input_hash(_input(cleaned_text="Một nội dung hoàn toàn khác"))
    assert hash_a != hash_b


def test_changed_prompt_version_produces_different_cache_key():
    input_hash = compute_input_hash(_input())

    key_v1 = compute_cache_key(
        provider="claude", model="claude-opus-5", prompt_version="v1", schema_version="v1", input_hash=input_hash
    )
    key_v2 = compute_cache_key(
        provider="claude", model="claude-opus-5", prompt_version="v2", schema_version="v1", input_hash=input_hash
    )
    assert key_v1 != key_v2


def test_changed_schema_version_produces_different_cache_key():
    input_hash = compute_input_hash(_input())

    key_a = compute_cache_key(
        provider="claude", model="claude-opus-5", prompt_version="v1", schema_version="v1", input_hash=input_hash
    )
    key_b = compute_cache_key(
        provider="claude", model="claude-opus-5", prompt_version="v1", schema_version="v2", input_hash=input_hash
    )
    assert key_a != key_b


def test_prompt_version_used_by_provider_matches_constant(tmp_path: Path):
    fake = _FakeClient(
        [_fake_parsed_response(post_product_type=NO_IDENTIFIABLE_PRODUCT, confidence=0.3)]
    )
    provider = ClaudeHistoricalCandidateProvider(client=fake, cache_dir=tmp_path, sleep=lambda _s: None)

    _result, provenance = provider.extract_with_provenance(_input())

    assert provenance.prompt_version == PROMPT_VERSION


# --- required fixture scenarios (Phase 8) -----------------------------------
#
# These exercise the plumbing end to end (provider -> validate_and_gate)
# using a canned fake response representing a claimed judgment -- they
# prove our code relays and gates that judgment correctly, not that a
# live model reasons correctly.


def test_title_without_author_fixture():
    request = _input(cleaned_text="Sách hay: Nhà Giả Kim, còn mới, giá 12€.")
    fake = _FakeClient(
        [
            _fake_parsed_response(
                post_product_type=SINGLE_BOOK,
                candidates=[
                    {
                        "title_raw": "Nhà Giả Kim",
                        "candidate_type": SINGLE_BOOK,
                        "evidence_text": "Sách hay: Nhà Giả Kim, còn mới",
                        "confidence": 0.85,
                    }
                ],
                confidence=0.85,
            )
        ]
    )
    provider = _provider(client=fake)

    raw = provider.extract(request)
    outcome, result = validate_and_gate(raw, request)

    assert outcome == "AUTO_PASS"
    assert result.candidates[0].title_raw == "Nhà Giả Kim"


def test_title_without_cua_or_by_fixture():
    request = _input(cleaned_text='"Doraemon Tap 1" giá 8€ ạ.')
    fake = _FakeClient(
        [
            _fake_parsed_response(
                post_product_type=SINGLE_BOOK,
                candidates=[
                    {
                        "title_raw": "Doraemon Tap 1",
                        "candidate_type": SINGLE_BOOK,
                        "evidence_text": '"Doraemon Tap 1" giá 8€',
                        "confidence": 0.85,
                    }
                ],
                confidence=0.85,
            )
        ]
    )
    provider = _provider(client=fake)

    raw = provider.extract(request)
    outcome, result = validate_and_gate(raw, request)

    assert outcome == "AUTO_PASS"


def test_customer_promotion_prose_fixture_yields_review_required():
    request = _input(cleaned_text="Em biết ơn tất cả khách hàng đã đặt sách và chờ đợi.")
    fake = _FakeClient(
        [_fake_parsed_response(post_product_type=NO_IDENTIFIABLE_PRODUCT, confidence=0.4)]
    )
    provider = _provider(client=fake)

    raw = provider.extract(request)
    outcome, result = validate_and_gate(raw, request)

    assert outcome == "REVIEW_REQUIRED"
    assert result.candidates == ()


def test_non_book_numerology_fixture_never_becomes_a_candidate():
    request = _input(cleaned_text="Bản đồ Nhân Số Học của bạn đã sẵn sàng, 200€.")
    fake = _FakeClient(
        [
            _fake_parsed_response(
                post_product_type=NO_IDENTIFIABLE_PRODUCT,
                rejected_hints=[{"text": "Bản đồ Nhân Số Học", "reason": "NON_BOOK"}],
                confidence=0.9,
            )
        ]
    )
    provider = _provider(client=fake)

    raw = provider.extract(request)
    outcome, result = validate_and_gate(raw, request)

    assert outcome == "REVIEW_REQUIRED"
    assert result.candidates == ()


def test_no_identifiable_title_fixture():
    request = _input(cleaned_text="Sách Có Sẵn tại Đức")
    fake = _FakeClient(
        [_fake_parsed_response(post_product_type=NO_IDENTIFIABLE_PRODUCT, confidence=0.3)]
    )
    provider = _provider(client=fake)

    raw = provider.extract(request)
    outcome, result = validate_and_gate(raw, request)

    assert outcome == "REVIEW_REQUIRED"
    assert result.candidates == ()


def test_image_review_required_fixture():
    request = _input(
        cleaned_text="Sách Có Sẵn tại Đức",
        local_image_paths=tuple(f"images/{i}.jpg" for i in range(20)),
    )
    fake = _FakeClient(
        [_fake_parsed_response(post_product_type=NO_IDENTIFIABLE_PRODUCT, confidence=0.3)]
    )
    provider = _provider(client=fake)

    raw = provider.extract(request)
    outcome, result = validate_and_gate(raw, request)

    assert outcome == "REVIEW_REQUIRED"
    assert REASON_IMAGE_REVIEW_REQUIRED in result.review_reason_codes


def test_hallucinated_title_fixture_never_survives_gate():
    """The provider CLAIMS a title that is not actually present in
    cleaned_text -- proves the gate (not the provider) is the real
    backstop against hallucination even if a live model ever
    misbehaves."""
    request = _input(cleaned_text="Em thanh lý vài cuốn sách cũ, ai cần nhắn em.")
    fake = _FakeClient(
        [
            _fake_parsed_response(
                post_product_type=SINGLE_BOOK,
                candidates=[
                    {
                        "title_raw": "Nhà Giả Kim",  # not in cleaned_text
                        "candidate_type": SINGLE_BOOK,
                        "evidence_text": "Em thanh lý vài cuốn sách cũ",
                        "confidence": 0.95,
                    }
                ],
                confidence=0.95,
            )
        ]
    )
    provider = _provider(client=fake)

    raw = provider.extract(request)
    outcome, result = validate_and_gate(raw, request)

    assert outcome == "REVIEW_REQUIRED"
    assert result.candidates == ()


def test_duplicate_titles_fixture_deduped_by_gate():
    request = _input(cleaned_text="Nhà Giả Kim của Paulo Coelho, còn 2 cuốn.")
    fake = _FakeClient(
        [
            _fake_parsed_response(
                post_product_type=SINGLE_BOOK,
                candidates=[
                    {
                        "title_raw": "Nhà Giả Kim",
                        "candidate_type": SINGLE_BOOK,
                        "evidence_text": "Nhà Giả Kim của Paulo Coelho",
                        "confidence": 0.9,
                    },
                    {
                        "title_raw": "Nhà Giả Kim",
                        "candidate_type": SINGLE_BOOK,
                        "evidence_text": "Nhà Giả Kim của Paulo Coelho",
                        "confidence": 0.85,
                    },
                ],
                confidence=0.9,
            )
        ]
    )
    provider = _provider(client=fake)

    raw = provider.extract(request)
    outcome, result = validate_and_gate(raw, request)

    assert outcome == "AUTO_PASS"
    assert len(result.candidates) == 1


def test_confidence_below_threshold_fixture_never_auto_passes():
    request = _input(cleaned_text="Nhà Giả Kim của Paulo Coelho, giá 12€.")
    fake = _FakeClient(
        [
            _fake_parsed_response(
                post_product_type=SINGLE_BOOK,
                candidates=[
                    {
                        "title_raw": "Nhà Giả Kim",
                        "candidate_type": SINGLE_BOOK,
                        "evidence_text": "Nhà Giả Kim của Paulo Coelho",
                        "confidence": 0.5,
                    }
                ],
                confidence=0.5,
            )
        ]
    )
    provider = _provider(client=fake)

    raw = provider.extract(request)
    outcome, result = validate_and_gate(raw, request)

    assert outcome == "REVIEW_REQUIRED"
    assert result.candidates == ()


# --- MockHistoricalCandidateProvider (zero network calls) -------------------


def test_mock_provider_finds_quoted_single_book():
    provider = MockHistoricalCandidateProvider()
    request = _input(cleaned_text='"Doraemon Tap 1" của Fujiko F. Fujio, giá 8€.')

    result = provider.extract(request)

    assert result.post_product_type == SINGLE_BOOK
    assert result.candidates[0].title_raw == "Doraemon Tap 1"


def test_mock_provider_rejects_numerology_as_non_identifiable():
    provider = MockHistoricalCandidateProvider()
    request = _input(cleaned_text="Bản đồ Nhân Số Học của bạn, 200€.")

    result = provider.extract(request)

    assert result.post_product_type == NO_IDENTIFIABLE_PRODUCT
    assert result.candidates == ()


def test_mock_provider_finds_price_bullet_multiple_books():
    provider = MockHistoricalCandidateProvider()
    request = _input(
        cleaned_text="Đắc Nhân Tâm - 10€\nNhà Giả Kim - 12€\nSuối Nguồn - 15€"
    )

    result = provider.extract(request)

    assert result.post_product_type == MULTIPLE_BOOKS
    assert len(result.candidates) == 3


# --- historical hardening pass (2026-08-30): schema capacity/completeness --


def test_candidate_list_complete_is_required_on_new_responses():
    """Production behavior, tested directly against the real schema
    (not the test-fixture helper's convenience default): a response
    missing candidate_list_complete fails Pydantic validation."""
    with pytest.raises(ValidationError):
        _ClaudeCandidateResponseSchema(post_product_type=SINGLE_BOOK, confidence=0.9)


def test_missing_candidate_list_complete_on_new_response_never_auto_passes():
    """End-to-end: a live response omitting the required field raises
    ValidationError inside the provider, which maps it to the existing
    malformed-output path -- REVIEW_REQUIRED via NO_IDENTIFIABLE_PRODUCT,
    never silently defaulted to complete=True."""

    class _RaisingMessages:
        def parse(self, **kwargs):
            _ClaudeCandidateResponseSchema.model_validate(
                {"post_product_type": SINGLE_BOOK, "confidence": 0.9}
            )

    provider = _provider(client=SimpleNamespace(messages=_RaisingMessages()))
    request = _input()

    raw = provider.extract(request)
    outcome, result = validate_and_gate(raw, request)

    assert outcome == "REVIEW_REQUIRED"
    assert result.candidates == ()


def test_schema_max_length_uses_the_centralized_constant():
    from src.domain.rules.historical_candidate_semantic import MAX_CANDIDATES_PER_RECORD

    field_info = _ClaudeCandidateResponseSchema.model_fields["candidates"]
    max_length_constraint = next(
        (m.max_length for m in field_info.metadata if hasattr(m, "max_length")), None
    )
    assert max_length_constraint == MAX_CANDIDATES_PER_RECORD


def test_schema_accepts_exactly_max_candidates_and_rejects_one_more():
    from src.domain.rules.historical_candidate_semantic import MAX_CANDIDATES_PER_RECORD

    card = {
        "title_raw": "X",
        "candidate_type": SINGLE_BOOK,
        "evidence_text": "X của Y",
        "confidence": 0.9,
    }

    # Exactly the cap: accepted.
    schema = _ClaudeCandidateResponseSchema(
        post_product_type=MULTIPLE_BOOKS,
        candidates=[card] * MAX_CANDIDATES_PER_RECORD,
        candidate_list_complete=True,
        confidence=0.9,
    )
    assert len(schema.candidates) == MAX_CANDIDATES_PER_RECORD

    # One more than the cap: rejected server-side-shape validation.
    with pytest.raises(ValidationError):
        _ClaudeCandidateResponseSchema(
            post_product_type=MULTIPLE_BOOKS,
            candidates=[card] * (MAX_CANDIDATES_PER_RECORD + 1),
            candidate_list_complete=True,
            confidence=0.9,
        )
