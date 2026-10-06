"""Metadata-only storefront content (built and validated, DISABLED).

Question investigated 2026-10-02: when no approved-source description
exists, can verified structured metadata alone yield a minimal,
customer-appropriate description that may be approved automatically?

What this module provides (pure, deterministic, tested):

  - evaluate_metadata_only_sufficiency(): are the facts verified and
    enough (title + at least one verified author or publisher, every
    author's role unambiguous)?
  - build_metadata_only_content(): fixed VI/EN/DE templates that state
    ONLY those facts -- no plot, benefit, age, award, recommendation or
    any other claim (CLAUDE.md 2.2/15.1, TSYC_CONTENT_GUIDE.md 11), no
    provenance/workflow/stock wording, no "missing field" notes.
  - validate_metadata_only_content(): text must be exactly the template
    output for the verified facts and free of storefront defects.

Why automatic approval is OFF (METADATA_ONLY_AUTO_APPROVAL_ENABLED):
CLAUDE.md 15.3 requires automatically approved content to be "not
generic placeholder-only copy", and the existing approval gate
(prepare_product_content.is_generic_safe_draft) deliberately blocks
metadata-only content "until the book description is enriched from
verified source material". Approving a one-to-two sentence identification
text instead is a change to that business rule -- it is for the shop
owner to decide, not for automation to assume. Flip the flag only on an
explicit owner decision; every caller already consults it.

Facts used are VERIFIED fields only (candidate.verified_*), never the
image-derived extracted_author/extracted publisher of a historical
candidate: a storefront sentence must not turn an unverified reading of
a cover into a stated fact.
"""
from __future__ import annotations

import re
from typing import Any, Mapping

from src.domain.decisions import DecisionResult, Outcome
from src.domain.rules import author_rules, identity_rules, storefront_text

METADATA_ONLY_AUTO_APPROVAL_ENABLED = False

METADATA_ONLY_SUFFICIENT = "METADATA_ONLY_SUFFICIENT"
METADATA_ONLY_INSUFFICIENT = "METADATA_ONLY_INSUFFICIENT"
METADATA_ONLY_DISABLED = "METADATA_ONLY_DISABLED"
METADATA_ONLY_VALID = "METADATA_ONLY_VALID"
METADATA_ONLY_INVALID = "METADATA_ONLY_INVALID"

# Any role annotation means the field lists contributors other than the
# author (illustrator, translator, ...). The template only ever says
# "by <author>", so a field with roles is not usable as-is.
_ROLE_MARKER_RE = re.compile(
    r"\(|:|\b(?:dịch|minh họa|minh hoạ|tranh|biên soạn|biên dịch|hiệu đính|illustrat|translat)",
    re.IGNORECASE,
)


def is_enabled() -> bool:
    return METADATA_ONLY_AUTO_APPROVAL_ENABLED


def verified_facts(candidate: Mapping[str, Any]) -> dict[str, Any]:
    """Only verified, unambiguous fields; anything doubtful is omitted."""
    title = str(candidate.get("verified_title") or candidate.get("extracted_title") or "").strip()
    author_field = str(candidate.get("verified_author") or "").strip()
    authors: list[str] = []
    if author_field and not _ROLE_MARKER_RE.search(author_field):
        authors = [
            name
            for name in author_rules.split_person_names(author_field)
            if identity_rules.is_specific_author(name)
        ]
    publisher = str(candidate.get("verified_publisher") or "").strip()
    return {"title": title, "authors": authors, "publisher": publisher or None}


def evaluate_metadata_only_sufficiency(candidate: Mapping[str, Any]) -> DecisionResult:
    facts = verified_facts(candidate)
    missing = []
    if not facts["title"]:
        missing.append("title")
    if not facts["authors"] and not facts["publisher"]:
        missing.append("verified author (without contributor roles) or verified publisher")
    if missing:
        return DecisionResult(
            outcome=Outcome.BLOCKED,
            rule_code=METADATA_ONLY_INSUFFICIENT,
            reason="Verified metadata is not sufficient for metadata-only content: missing "
            + ", ".join(missing) + ".",
            evidence={"facts": facts},
        )
    return DecisionResult(
        outcome=Outcome.AUTO_PASS,
        rule_code=METADATA_ONLY_SUFFICIENT,
        reason="Verified title plus author/publisher are available.",
        evidence={"facts": facts},
    )


def metadata_only_path_available(candidate: Mapping[str, Any]) -> DecisionResult:
    """The single question the state machine asks: may this candidate rely
    on metadata-only content instead of a reference description?"""
    sufficiency = evaluate_metadata_only_sufficiency(candidate)
    if not sufficiency.is_auto_pass:
        return sufficiency
    if not is_enabled():
        return DecisionResult(
            outcome=Outcome.BLOCKED,
            rule_code=METADATA_ONLY_DISABLED,
            reason=(
                "Verified metadata would suffice, but metadata-only automatic "
                "approval is disabled pending the shop owner's decision "
                "(CLAUDE.md 15.3 'not generic placeholder-only copy')."
            ),
            evidence=dict(sufficiency.evidence),
        )
    return sufficiency


def _join(names: list[str], conjunction: str) -> str:
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + f" {conjunction} " + names[-1]


def build_metadata_only_content(facts: Mapping[str, Any]) -> dict[str, dict[str, str]]:
    """{language: {product_name, short_description, long_description}}."""
    title = facts["title"]
    authors = list(facts.get("authors") or [])
    publisher = facts.get("publisher")

    # Never a claim beyond the facts: no language, age, genre or format.
    if authors:
        vi = [f"“{title}” là cuốn sách của tác giả {_join(authors, 'và')}."]
        en = [f"“{title}” is a book by {_join(authors, 'and')}."]
        de = [f"„{title}“ ist ein Buch von {_join(authors, 'und')}."]
        if publisher:
            vi.append(f"Sách do {publisher} phát hành.")
            en.append(f"It is published by {publisher}.")
            de.append(f"Es erscheint bei {publisher}.")
    else:
        vi = [f"“{title}” là cuốn sách do {publisher} phát hành."]
        en = [f"“{title}” is a book published by {publisher}."]
        de = [f"„{title}“ ist ein Buch aus dem Verlag {publisher}."]

    return {
        language: {
            "product_name": title,
            "short_description": sentences[0],
            "long_description": " ".join(sentences),
        }
        for language, sentences in (("vi", vi), ("en", en), ("de", de))
    }


def validate_metadata_only_content(
    candidate: Mapping[str, Any],
    language: str,
    content: Mapping[str, Any],
) -> DecisionResult:
    """Exactly the template text for the verified facts, defect-free."""
    sufficiency = evaluate_metadata_only_sufficiency(candidate)
    if not sufficiency.is_auto_pass:
        return DecisionResult(
            outcome=Outcome.REVIEW_REQUIRED,
            rule_code=METADATA_ONLY_INVALID,
            reason=sufficiency.reason,
        )
    expected = build_metadata_only_content(sufficiency.evidence["facts"]).get(language)
    if expected is None:
        return DecisionResult(
            outcome=Outcome.REVIEW_REQUIRED,
            rule_code=METADATA_ONLY_INVALID,
            reason=f"Unsupported language {language!r}.",
        )
    mismatched = sorted(
        field for field, value in expected.items()
        if " ".join(str(content.get(field) or "").split()) != value
    )
    defects = storefront_text.find_content_defects(content, storefront_text.PROSE_FIELDS)
    if mismatched or defects:
        return DecisionResult(
            outcome=Outcome.REVIEW_REQUIRED,
            rule_code=METADATA_ONLY_INVALID,
            reason=(
                "Metadata-only content differs from the verified-facts template "
                f"({', '.join(mismatched) or 'none'}) or has storefront defects "
                f"({defects or 'none'})."
            ),
            evidence={"mismatched_fields": mismatched, "defects": defects},
        )
    return DecisionResult(
        outcome=Outcome.AUTO_PASS,
        rule_code=METADATA_ONLY_VALID,
        reason="Content states only verified facts, exactly per template.",
    )
