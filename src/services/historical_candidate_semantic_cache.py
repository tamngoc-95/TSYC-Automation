"""OFFLINE local cache for SECONDARY (semantic) historical Facebook
CANDIDATE-EXTRACTION results.

Scope -- READ THIS BEFORE CHANGING EITHER SIDE:

    Deliberately separate from src/services/facebook_history_semantic_
    cache.py (the earlier migration-relevance classification cache) --
    this task's own Phase 6 requirement: different task, different
    prompt, different output schema, so a different cache under a
    different directory, never sharing keys or files with the
    classification cache. Same on-disk shape and cache-miss-safety
    guarantees as that module (one JSON file per cache key; a
    missing/corrupt file is always treated as a miss, never a crash);
    not otherwise copy-pasted logic-for-logic since the payload shape
    genuinely differs.

    Cache key = sha256(provider name | model | extraction prompt
    version | output schema version | normalized cleaned-input hash) --
    changing ANY of those five things yields a different key, so a
    stale entry is simply never looked up again rather than reused
    incorrectly. Lives under data/processed/ (already covered by this
    repo's `data/processed/*` .gitignore rule) -- nothing written here
    is ever committed.

    Only a SUCCESSFUL, already-schema-validated raw
    CandidateExtractionResult is ever written here (before
    validate_and_gate() runs) -- src.services.historical_candidate_
    semantic_provider.ClaudeHistoricalCandidateProvider only calls
    set() on its success path, never for a malformed-output or failed-
    API-call result. Caching the pre-gate raw result (not the post-gate
    sanitized one) means a later re-run with the same input, model,
    prompt and schema version reuses the cached provider answer and
    still re-derives the final AUTO_PASS/REVIEW_REQUIRED decision by
    re-running validate_and_gate() fresh every time -- so a
    threshold/gate-rule change (which never touches the cache key) is
    reflected immediately without invalidating anything.

    Backward compatibility (historical hardening pass, 2026-08-30):
    CandidateExtractionResult gained a candidate_list_complete field
    after this cache already held 65 entries written by an older
    provider version whose response schema had no such field (and
    whose candidate list was capped at 20, not the current
    MAX_CANDIDATES_PER_RECORD). Rather than invalidate/rebill those
    entries, get() infers a safe value for any entry whose JSON
    predates the field -- see _infer_legacy_candidate_list_complete()'s
    own docstring for the exact, conservative rule. This is the
    "provider cache stores the raw response; validation/gate runs
    afterward" architecture working as intended: the gate's new rule 7
    (completeness) applies to every cache read, old or new, without
    needing a single additional API call.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from pathlib import Path

from src.domain.rules.historical_candidate_semantic import (
    CandidateExtractionInput,
    CandidateExtractionResult,
    ExtractedCandidateCard,
    RejectedHint,
)

CACHE_KEY_ALGORITHM = "sha256"

# The candidates cap in effect before the 2026-08-30 hardening pass
# (see src.domain.rules.historical_candidate_semantic.
# MAX_CANDIDATES_PER_RECORD for the current value). Used ONLY by
# _infer_legacy_candidate_list_complete() below to interpret a cache
# entry written before candidate_list_complete existed -- never used
# for anything else, and never re-used as a live bound.
_LEGACY_MAX_CANDIDATES_PER_RECORD = 20

# Substrings that, if present in an old entry's review_reason_codes,
# independently indicate the provider itself flagged a truncated/
# partial list even without a dedicated field (e.g. record #1483's
# real cached reason "MANY_TITLES_TRUNCATED_AT_20"). Checked case-
# insensitively, in addition to (never instead of) the count-based
# rule.
_LEGACY_TRUNCATION_INDICATOR_SUBSTRINGS = ("TRUNCAT", "PARTIAL", "OMITTED", "INCOMPLETE")

# Auditability marker (per this hardening pass's own requirement: old-
# vs-new completeness must be distinguishable downstream, not silently
# folded into the same boolean as an explicit provider attestation).
# Appended to review_reason_codes -- never to candidate_list_complete
# itself, which stays a plain bool the gate can check uniformly -- so
# a human reading a REVIEW_REQUIRED/AUTO_PASS record's reasons can
# always tell whether completeness was INFERRED from a pre-hardening
# cache entry or actually stated by the provider.
LEGACY_COMPLETENESS_INFERRED_MARKER = "LEGACY_CACHE_COMPLETENESS_INFERRED"


def _infer_legacy_candidate_list_complete(payload: dict) -> bool:
    """Conservative backward-compatible inference for a cache entry
    written before candidate_list_complete existed (no key present in
    its JSON at all).

    Rule: complete UNLESS there is a concrete reason to doubt it.
    Under the OLD schema, no response could EVER exceed
    _LEGACY_MAX_CANDIDATES_PER_RECORD (Pydantic's max_length enforced
    this server-side) -- so an entry with FEWER candidates than that
    ceiling is provably complete (the model stopped naturally, nothing
    was cut off). An entry that lands EXACTLY on the old ceiling is
    indeterminate -- it may be genuinely complete, or it may have been
    silently truncated there, and the old schema gives no way to tell
    them apart -- so it is treated as NOT complete, matching this
    module's "never silently claim completeness after truncation"
    principle (this is exactly record #1483's case: 20 candidates,
    plus its own "MANY_TITLES_TRUNCATED_AT_20" reason code, doubly
    confirming truncation). review_reason_codes is also scanned for an
    explicit truncation-style disclosure as an independent signal,
    regardless of count."""
    candidate_count = len(payload.get("candidates", ()))
    reason_codes = payload.get("review_reason_codes", ())

    if any(
        indicator in str(code).upper()
        for code in reason_codes
        for indicator in _LEGACY_TRUNCATION_INDICATOR_SUBSTRINGS
    ):
        return False

    return candidate_count < _LEGACY_MAX_CANDIDATES_PER_RECORD


def compute_input_hash(request: CandidateExtractionInput) -> str:
    """A stable hash of everything about one record that could change a
    candidate extractor's answer -- every field of
    CandidateExtractionInput."""
    canonical = json.dumps(asdict(request), ensure_ascii=False, sort_keys=True)
    return hashlib.new(CACHE_KEY_ALGORITHM, canonical.encode("utf-8")).hexdigest()


def compute_cache_key(
    *,
    provider: str,
    model: str,
    prompt_version: str,
    schema_version: str,
    input_hash: str,
) -> str:
    """Combine provider, model, extraction prompt version, output
    schema version, and the input hash. Every component is part of the
    key -- bumping only schema_version (e.g. after adding a new field
    to CandidateExtractionResult) yields a different key even for the
    exact same record/model/prompt, so an old cached answer under a
    stale schema is never silently reused."""
    raw = f"{provider}|{model}|{prompt_version}|{schema_version}|{input_hash}"
    return hashlib.new(CACHE_KEY_ALGORITHM, raw.encode("utf-8")).hexdigest()


class CandidateResultCache:
    """A minimal on-disk cache: one JSON file per cache key. Never
    raises on a missing/corrupt entry -- get() returns None (a cache
    miss), costing at most one extra (re-cached) API call."""

    def __init__(self, cache_dir: str | Path) -> None:
        self._cache_dir = Path(cache_dir)

    def _path_for(self, cache_key: str) -> Path:
        return self._cache_dir / f"{cache_key}.json"

    def get(self, cache_key: str) -> CandidateExtractionResult | None:
        path = self._path_for(cache_key)

        if not path.is_file():
            return None

        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

        try:
            review_reason_codes = list(payload.get("review_reason_codes", ()))

            if "candidate_list_complete" in payload:
                # New-format entry -- the provider's own explicit
                # attestation, used as-is.
                candidate_list_complete = bool(payload["candidate_list_complete"])
            else:
                # Pre-2026-08-30 entry -- infer conservatively rather
                # than assume complete (never rely on the dataclass's
                # own True default for this). See _infer_legacy_
                # candidate_list_complete()'s own docstring. The
                # inference is recorded explicitly in the returned
                # reason codes so old-vs-new completeness stays
                # auditable downstream, never silently indistinguishable
                # from a real provider attestation.
                candidate_list_complete = _infer_legacy_candidate_list_complete(payload)
                review_reason_codes.append(LEGACY_COMPLETENESS_INFERRED_MARKER)

            return CandidateExtractionResult(
                post_product_type=payload["post_product_type"],
                candidates=tuple(
                    ExtractedCandidateCard(
                        title_raw=c["title_raw"],
                        candidate_type=c["candidate_type"],
                        evidence_text=c["evidence_text"],
                        confidence=c["confidence"],
                    )
                    for c in payload.get("candidates", ())
                ),
                rejected_hints=tuple(
                    RejectedHint(text=h["text"], reason=h["reason"])
                    for h in payload.get("rejected_hints", ())
                ),
                confidence=payload.get("confidence", 0.0),
                review_reason_codes=tuple(review_reason_codes),
                candidate_list_complete=candidate_list_complete,
            )
        except (KeyError, TypeError, ValueError):
            return None

    def set(self, cache_key: str, result: CandidateExtractionResult) -> None:
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "post_product_type": result.post_product_type,
            "candidates": [
                {
                    "title_raw": c.title_raw,
                    "candidate_type": c.candidate_type,
                    "evidence_text": c.evidence_text,
                    "confidence": c.confidence,
                }
                for c in result.candidates
            ],
            "rejected_hints": [
                {"text": h.text, "reason": h.reason} for h in result.rejected_hints
            ],
            "confidence": result.confidence,
            "review_reason_codes": list(result.review_reason_codes),
            "candidate_list_complete": result.candidate_list_complete,
        }
        self._path_for(cache_key).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
