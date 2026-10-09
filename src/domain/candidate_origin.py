"""Candidate origin by candidate_code prefix (no I/O).

FB-HIST-...   historical Facebook export (see create_internal_product.
              is_historical_candidate_code; CLAUDE.md 6.2)
FAHASA-...    new book discovered on Fahasa (scripts/import_fahasa_
              candidates.py; CLAUDE.md 14.8). A LIVE-pipeline candidate:
              every live gate applies, including the human-authorized
              WooCommerce draft gate (CLAUDE.md 6/6.1).
anything else live Facebook pipeline (FB-2026-...)
"""
from __future__ import annotations

FAHASA_DISCOVERY_CODE_PREFIX = "FAHASA-"
FAHASA_DISCOVERY_ORIGIN = "FAHASA_DISCOVERY"


def is_fahasa_discovery_candidate_code(candidate_code: str | None) -> bool:
    return str(candidate_code or "").startswith(FAHASA_DISCOVERY_CODE_PREFIX)
