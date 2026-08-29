"""OFFLINE local cache for SECONDARY (semantic) historical Facebook
classification results.

Scope -- READ THIS BEFORE CHANGING EITHER SIDE:

    Exists solely so re-classifying the same historical record with the
    same provider/model/prompt version never pays for (or waits on) a
    second live API call. This module has no Anthropic/network
    dependency itself -- it only reads/writes small local JSON files
    under a directory that lives inside data/processed/, which is
    already covered by this repo's `data/processed/*` .gitignore rule
    (see .gitignore) -- nothing written here is ever committed.

    Cache key = sha256(provider name | model | prompt/schema version |
    normalized semantic-input hash) -- see compute_input_hash() and
    compute_cache_key(). Changing ANY of those four things (a different
    provider, a different model, a bumped PROMPT_VERSION, or different
    record content/evidence) produces a different key, so a stale cache
    entry is simply never looked up again rather than being reused
    incorrectly.

    Only a SUCCESSFUL, already-validated SemanticClassificationResult is
    ever written here -- src.services.facebook_history_semantic_provider.
    ClaudeHistoricalSemanticProvider only calls set() on its success
    path, never for a malformed-output or failed-API-call result
    (project requirement: "Do not cache malformed/failed API responses
    as successful results"). A corrupt/unreadable cache file on read is
    treated as a cache miss, never as a crash.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from pathlib import Path

from src.domain.rules.facebook_history_semantic import SemanticClassificationInput, SemanticClassificationResult

CACHE_KEY_ALGORITHM = "sha256"


def compute_input_hash(request: SemanticClassificationInput) -> str:
    """A stable hash of everything about one record that could change a
    semantic classifier's answer -- i.e. every field of
    SemanticClassificationInput. Two requests with the same field values
    (any field order, since we serialize a fixed dict) always hash
    identically; changing any single field value changes the hash."""
    canonical = json.dumps(asdict(request), ensure_ascii=False, sort_keys=True)
    return hashlib.new(CACHE_KEY_ALGORITHM, canonical.encode("utf-8")).hexdigest()


def compute_cache_key(*, provider: str, model: str, prompt_version: str, input_hash: str) -> str:
    """Combine provider name, model, prompt/schema version, and the
    input hash into one cache key. Every one of the four components is
    part of the key -- changing only the prompt_version (e.g. after
    editing the system prompt) yields a different key even for the exact
    same record and model, so an old cached answer is never silently
    reused after the prompt contract changes."""
    raw = f"{provider}|{model}|{prompt_version}|{input_hash}"
    return hashlib.new(CACHE_KEY_ALGORITHM, raw.encode("utf-8")).hexdigest()


class SemanticResultCache:
    """A minimal on-disk cache: one JSON file per cache key.

    Never raises on a missing/corrupt entry -- get() returns None
    (a cache miss) so a damaged cache file can never crash a batch run;
    it just costs one extra (re-cached) API call for that record.
    """

    def __init__(self, cache_dir: str | Path) -> None:
        self._cache_dir = Path(cache_dir)

    def _path_for(self, cache_key: str) -> Path:
        return self._cache_dir / f"{cache_key}.json"

    def get(self, cache_key: str) -> SemanticClassificationResult | None:
        path = self._path_for(cache_key)

        if not path.is_file():
            return None

        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

        try:
            return SemanticClassificationResult(
                semantic_post_type=payload["semantic_post_type"],
                product_migration_relevant=payload["product_migration_relevant"],
                confidence=payload["confidence"],
                reason_codes=tuple(payload.get("reason_codes", ())),
                extracted_product_hints=tuple(payload.get("extracted_product_hints", ())),
            )
        except (KeyError, TypeError, ValueError):
            return None

    def set(self, cache_key: str, result: SemanticClassificationResult) -> None:
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "semantic_post_type": result.semantic_post_type,
            "product_migration_relevant": result.product_migration_relevant,
            "confidence": result.confidence,
            "reason_codes": list(result.reason_codes),
            "extracted_product_hints": list(result.extracted_product_hints),
        }
        self._path_for(cache_key).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
