"""TSYC deterministic decision-engine rule modules.

Each submodule (identity_rules, image_rules, content_rules,
readiness_rules) is a set of pure functions: given plain dicts (the same
shape scripts/*.py already reads from Supabase), they return a
src.domain.decisions.DecisionResult with no I/O and no side effects.
Writer scripts remain the only place that persists anything -- see
docs/TSYC_DECISION_MATRIX.md for the full rule specification these
modules implement.
"""
from __future__ import annotations
