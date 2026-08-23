"""Canonical identity-related vocabularies.

- product_candidates.identity_status: whether a candidate's book identity
  has been verified. Authoritative source:
  migrations/001_initial_schema.sql (identity_status CHECK constraint).
- product_references.match_decision: the outcome of comparing one
  candidate against one external reference. A related but separate
  concept from identity_status -- a candidate's identity_status is
  derived from its references' match_decision values, not identical to
  them. Authoritative source: migrations/001_initial_schema.sql
  (match_decision CHECK constraint).
"""
from __future__ import annotations


class IdentityStatus:
    """product_candidates.identity_status values."""

    IDENTITY_PENDING = "IDENTITY_PENDING"
    IDENTITY_VERIFIED = "IDENTITY_VERIFIED"
    ACCEPTED_WITH_LIMITED_METADATA = "ACCEPTED_WITH_LIMITED_METADATA"
    IDENTITY_CONFLICT = "IDENTITY_CONFLICT"
    REJECTED = "REJECTED"


ALL_IDENTITY_STATUSES = frozenset(
    {
        IdentityStatus.IDENTITY_PENDING,
        IdentityStatus.IDENTITY_VERIFIED,
        IdentityStatus.ACCEPTED_WITH_LIMITED_METADATA,
        IdentityStatus.IDENTITY_CONFLICT,
        IdentityStatus.REJECTED,
    }
)


class MatchDecision:
    """product_references.match_decision values."""

    MATCH = "MATCH"
    POSSIBLE_MATCH = "POSSIBLE_MATCH"
    DIFFERENT_EDITION = "DIFFERENT_EDITION"
    NO_MATCH = "NO_MATCH"
    MANUAL_REVIEW = "MANUAL_REVIEW"


ALL_MATCH_DECISIONS = frozenset(
    {
        MatchDecision.MATCH,
        MatchDecision.POSSIBLE_MATCH,
        MatchDecision.DIFFERENT_EDITION,
        MatchDecision.NO_MATCH,
        MatchDecision.MANUAL_REVIEW,
    }
)
