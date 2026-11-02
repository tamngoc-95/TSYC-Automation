"""Per-language state of one EN/DE create-and-link operation (no network).

The operation ID is deterministic for one book, language and approved content
row, so an interrupted run resumes the same request instead of starting a new
one. The state is written to disk BEFORE the request is sent: a crash after
that point leaves REQUEST_SENT, which can only be left through a remote
reconciliation read (CLAUDE.md 2.6, no blind retry).

  PLANNED ──> REQUEST_SENT ──> CONFIRMED ──> ROLLED_BACK
                  │  ▲
                  ▼  │ only after the store answered "absent" or "partial"
               UNCERTAIN
                  │
  REFUSED <───────┘ (the store gave a deterministic refusal; a person looks)
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

PLANNED = "PLANNED"
REQUEST_SENT = "REQUEST_SENT"
UNCERTAIN = "UNCERTAIN"
CONFIRMED = "CONFIRMED"
REFUSED = "REFUSED"
ROLLED_BACK = "ROLLED_BACK"
STATES = (PLANNED, REQUEST_SENT, UNCERTAIN, CONFIRMED, REFUSED, ROLLED_BACK)

NEEDS_RECONCILE = (REQUEST_SENT, UNCERTAIN)
CHECKPOINT_VERSION = 1


def make_request_id(internal_product_id: str, lang: str, product_content_id: str) -> str:
    """Stable across runs. New approved content gets a new ID; a retry never does."""
    digest = hashlib.sha256(f"{internal_product_id}|{lang}|{product_content_id}".encode("utf-8")).hexdigest()
    return f"tsyc-tr-{lang}-{digest[:32]}"


@dataclass
class LanguageOperation:
    request_id: str
    internal_product_id: str
    product_code: str
    lang: str
    source_id: int
    product_content_id: str
    state: str = PLANNED
    expected_source_modified_gmt: str | None = None
    product_id: int | None = None
    modified_gmt: str | None = None
    baseline_text: dict[str, str] | None = None
    translations: dict[str, int] = field(default_factory=dict)
    reason_code: str | None = None
    reason: str | None = None
    history: list[dict[str, str]] = field(default_factory=list)

    def move(self, state: str, *, code: str | None = None, reason: str | None = None) -> None:
        if state not in STATES:
            raise ValueError(f"Unknown operation state {state!r}.")
        self.state = state
        self.reason_code = code
        self.reason = reason
        self.history.append({"at": datetime.now(timezone.utc).isoformat(), "state": state, "code": code or ""})


class CheckpointError(RuntimeError):
    """The checkpoint is unreadable. Never treated as "nothing was done"."""


class CheckpointStore:
    """One JSON file, replaced atomically on every change."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.operations: dict[str, LanguageOperation] = {}
        if self.path.exists():
            self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if data.get("version") != CHECKPOINT_VERSION:
                raise ValueError(f"version {data.get('version')!r}")
            self.operations = {key: LanguageOperation(**value) for key, value in data["operations"].items()}
        except Exception as error:  # noqa: BLE001 - a damaged checkpoint must stop the run
            raise CheckpointError(f"Unreadable checkpoint {self.path}: {type(error).__name__}: {error}") from error

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": CHECKPOINT_VERSION,
                   "operations": {key: asdict(op) for key, op in sorted(self.operations.items())}}
        handle, temporary = tempfile.mkstemp(dir=self.path.parent, prefix=self.path.name, suffix=".tmp")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, ensure_ascii=False, indent=1)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise

    @staticmethod
    def key(internal_product_id: str, lang: str) -> str:
        return f"{internal_product_id}:{lang}"

    def get(self, internal_product_id: str, lang: str) -> LanguageOperation | None:
        return self.operations.get(self.key(internal_product_id, lang))

    def put(self, operation: LanguageOperation) -> None:
        self.operations[self.key(operation.internal_product_id, operation.lang)] = operation
        self.save()

    def summary(self) -> Mapping[str, Any]:
        counts: dict[str, int] = {}
        for operation in self.operations.values():
            counts[operation.state] = counts.get(operation.state, 0) + 1
        return counts
