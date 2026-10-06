"""Deterministic approved-source reference discovery (FB-HIST only).

Root cause it fixes (2026-10-02 Fast Track Batch A): every remaining
FAST_TRACK historical candidate had zero product_references, nothing in
the pipeline ever looked for one, and the candidate therefore always
ended at CONTENT_REVIEW_REQUIRED with the generic metadata-only draft.

This module decides, without I/O:

  - whether a candidate is eligible for automatic discovery,
  - which approved sites to search, in CLAUDE.md 8.1 source priority
    (PUBLISHER > AUTHORIZED_SUPPLIER > BOOKSTORE > FAHASA),
  - whether one fetched, parsed product page is the candidate's book.

A page is accepted ONLY with title agreement AND independent
corroboration -- a similar-looking title is never enough (real example:
netabooks.vn/tu-duy-nguoc is Jonah Sachs' "Tư Duy Ngược", a different
book from the shop's Nguyễn Anh Dũng title of the same name):

  title     normalized exact match, or the page title is
            "<series prefix> - <candidate title>" (CLAUDE.md 9.1 "exact
            volume title with only a known/common series prefix
            omitted"); a trailing edition suffix "(Tái Bản 2019)" is
            ignored (edition is not identity, CLAUDE.md 9.3)
  plus one  a valid, equal ISBN (978/979 only -- 893 barcodes never
            count, CLAUDE.md 2.3), or at least one shared author name
            (roles/honorifics stripped, diacritic-folded comparison)
  and none  of identity_rules.reference_business_conflict_reason()'s
            conflicts (the exact check content/image draft-safe
            selection already applies), no author disagreement, no
            used-book listing, no combo/set listing for a single book.

Accepted pages are registered through the existing
register_reference_source.py writer as an authorized, crawl-selected
source; collection/matching/enrichment then run through the existing
pipeline stages unchanged. Fahasa remains a REFERENCE source only --
nothing here reads or records a price.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from src.domain.decisions import DecisionResult, Outcome
from src.domain.reference_sources import REFERENCE_SOURCE_PRIORITY, SourceType
from src.domain.rules import author_rules, identity_rules

DISCOVERY_RULES_VERSION = "1.0.0"

# process_logs bookkeeping: one row per completed attempt, so the state
# machine can tell "never searched" from "searched, nothing qualified"
# without re-searching on every run.
PROCESS_NAME = "reference_discovery"
STATUS_FOUND = "FOUND"
STATUS_NOT_FOUND = "NOT_FOUND"
# Search/fetch infrastructure failed (network, site down): NOT a
# completed attempt -- the candidate stays eligible for a later retry.
STATUS_ERROR = "ERROR"
COMPLETED_STATUSES = frozenset({STATUS_FOUND, STATUS_NOT_FOUND})

# Rule codes.
DISCOVERY_ELIGIBLE = "DISCOVERY_ELIGIBLE"
DISCOVERY_INELIGIBLE = "DISCOVERY_INELIGIBLE"
DISCOVERY_REFERENCE_ACCEPTED = "DISCOVERY_REFERENCE_ACCEPTED"
DISCOVERY_TITLE_MISMATCH = "DISCOVERY_TITLE_MISMATCH"
DISCOVERY_AUTHOR_CONFLICT = "DISCOVERY_AUTHOR_CONFLICT"
DISCOVERY_ISBN_CONFLICT = "DISCOVERY_ISBN_CONFLICT"
DISCOVERY_NO_CORROBORATION = "DISCOVERY_NO_CORROBORATION"
DISCOVERY_LISTING_REJECTED = "DISCOVERY_LISTING_REJECTED"
DISCOVERY_IDENTITY_CONFLICT = "DISCOVERY_IDENTITY_CONFLICT"

TITLE_MATCH_EXACT = "EXACT"
TITLE_MATCH_SERIES_PREFIX = "SERIES_PREFIX"

_CONFIDENCE_ISBN = 0.97
_CONFIDENCE_EXACT_TITLE_AUTHOR = 0.92
_CONFIDENCE_SERIES_TITLE_AUTHOR = 0.88

# Only single books: a combo/set's identity spans several volumes and a
# single catalogue page cannot corroborate the whole sellable unit
# (CLAUDE.md 11/14.6) -- those stay out of automatic discovery.
_ELIGIBLE_CANDIDATE_TYPES = frozenset({"SINGLE_BOOK"})
_INELIGIBLE_IDENTITY_STATUSES = frozenset({"IDENTITY_CONFLICT", "REJECTED"})


@dataclass(frozen=True)
class DiscoverySite:
    name: str
    source_type: str
    search_url_template: str  # "{query}" is URL-encoded by the caller
    product_url_pattern: str  # regex a product-page URL must fully match
    slug_url_template: str  # "{slug}" -- direct product URL guess

    def is_product_url(self, url: str) -> bool:
        return re.fullmatch(self.product_url_pattern, url) is not None


# Approved, already-registered-in-production reference sites that have a
# metadata parser in collect_reference_metadata.py. Ordered here by
# REFERENCE_SOURCE_PRIORITY at import time (BOOKSTORE before FAHASA).
_SITES = (
    DiscoverySite(
        name="Fahasa",
        source_type=SourceType.FAHASA,
        search_url_template="https://www.fahasa.com/searchengine?q={query}",
        product_url_pattern=r"https://www\.fahasa\.com/[a-z0-9-]+\.html",
        slug_url_template="https://www.fahasa.com/{slug}.html",
    ),
    DiscoverySite(
        name="NetaBooks",
        source_type=SourceType.BOOKSTORE,
        search_url_template="https://www.netabooks.vn/SearchResults.aspx?q={query}",
        product_url_pattern=r"https://www\.netabooks\.vn/[a-z0-9]+(?:-[a-z0-9]+)+",
        slug_url_template="https://www.netabooks.vn/{slug}",
    ),
)


def title_slug(title: str | None) -> str:
    """The URL slug both sites derive from a Vietnamese title
    ("Tư Duy Ngược" -> "tu-duy-nguoc"). Only a URL guess: the fetched
    page must still pass evaluate_discovered_reference()."""
    if not title:
        return ""
    text = str(title).replace("đ", "d").replace("Đ", "D")
    text = unicodedata.normalize("NFD", text)
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Mn").lower()
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-")


def listing_text_title(link_text: str | None) -> str:
    """Search-result link text without a category breadcrumb
    ("Truyện Tranh > Tên Sách" -> "Tên Sách")."""
    return str(link_text or "").split(">")[-1].strip()

DISCOVERY_SITES: tuple[DiscoverySite, ...] = tuple(
    sorted(_SITES, key=lambda site: REFERENCE_SOURCE_PRIORITY[site.source_type])
)

_EDITION_SUFFIX_RE = re.compile(
    r"\s*[\(\[](?:tái bản|tb|bản|phiên bản|tái bản lần)[^\)\]]*[\)\]]\s*$",
    re.IGNORECASE,
)
_TRAILING_PARENTHETICAL_RE = re.compile(r"\s*\([^)]*\)\s*$")
_USED_BOOK_MARKERS = ("phiên chợ sách cũ", "sách cũ")
_MULTI_UNIT_LISTING_RE = re.compile(r"^(?:combo|bộ|trọn bộ|boxset|box set)\b", re.IGNORECASE)

# Comparable title: NFC, case-folded, punctuation removed, Vietnamese
# diacritics PRESERVED (they distinguish real titles).
title_key = identity_rules.exact_title_key


def _strip_edition(title: str) -> str:
    return _EDITION_SUFFIX_RE.sub("", title).strip()


def candidate_title(candidate: Mapping[str, Any]) -> str:
    return str(candidate.get("verified_title") or candidate.get("extracted_title") or "").strip()


def candidate_title_variants(candidate: Mapping[str, Any]) -> list[str]:
    """The candidate title, plus -- when it ends in a parenthetical such
    as an original-language subtitle "(The Book of Women)" -- the title
    without it. Never shortens a title any other way."""
    title = candidate_title(candidate)
    if not title:
        return []
    variants = [title]
    stripped = _TRAILING_PARENTHETICAL_RE.sub("", title).strip()
    if stripped and stripped != title:
        variants.append(stripped)
    return variants


def title_match_kind(candidate_titles: Sequence[str], page_title: str | None) -> str | None:
    """EXACT / SERIES_PREFIX / None for one listing or page title."""
    if not page_title:
        return None
    page = _strip_edition(str(page_title))
    page_key = title_key(page)
    keys = [title_key(_strip_edition(title)) for title in candidate_titles if title]
    keys = [key for key in keys if key]
    if not page_key or not keys:
        return None
    if page_key in keys:
        return TITLE_MATCH_EXACT
    # Same helper the shared draft-safe conflict guard uses, so discovery
    # never accepts a series form that content/image selection rejects.
    if any(identity_rules.is_series_volume_title(title, page) for title in candidate_titles):
        return TITLE_MATCH_SERIES_PREFIX
    return None


def listing_rejection_reason(candidate: Mapping[str, Any], page_title: str | None) -> str | None:
    """A used-book or multi-unit listing is never the single new book."""
    text = unicodedata.normalize("NFC", str(page_title or "")).casefold()
    if any(marker in text for marker in _USED_BOOK_MARKERS):
        return "Used-book listing, not the catalogue product."
    if (
        candidate.get("candidate_type") == "SINGLE_BOOK"
        and _MULTI_UNIT_LISTING_RE.match(text.lstrip("[( "))
    ):
        return "Combo/set listing for a single-book candidate."
    return None


def evaluate_discovery_eligibility(candidate: Mapping[str, Any]) -> DecisionResult:
    """Whether automatic discovery may be attempted for this candidate."""
    reasons: list[str] = []
    if not candidate_title(candidate):
        reasons.append("no meaningful title")
    if candidate.get("candidate_type") not in _ELIGIBLE_CANDIDATE_TYPES:
        reasons.append(
            f"candidate_type {candidate.get('candidate_type')!r} is not a single "
            "book (combo/set identity cannot be corroborated by one page)"
        )
    if candidate.get("identity_status") in _INELIGIBLE_IDENTITY_STATUSES:
        reasons.append(f"identity_status is {candidate.get('identity_status')}")
    if not discovery_corroboration_available(candidate):
        reasons.append(
            "no author or valid ISBN to corroborate a title match (a title "
            "alone never registers a reference)"
        )

    if reasons:
        return DecisionResult(
            outcome=Outcome.BLOCKED,
            rule_code=DISCOVERY_INELIGIBLE,
            reason="Automatic reference discovery not possible: " + "; ".join(reasons) + ".",
            evidence={"reasons": reasons},
        )
    return DecisionResult(
        outcome=Outcome.AUTO_PASS,
        rule_code=DISCOVERY_ELIGIBLE,
        reason="Single book with title and corroborating author/ISBN.",
    )


def _candidate_isbn(candidate: Mapping[str, Any]) -> str | None:
    value = candidate.get("verified_isbn") or candidate.get("possible_isbn")
    return value if identity_rules.looks_like_valid_isbn(value) else None


def _candidate_author_field(candidate: Mapping[str, Any]) -> str | None:
    return candidate.get("verified_author") or candidate.get("extracted_author")


def _specific_names(author_field: str | None) -> list[str]:
    return [
        name
        for name in author_rules.split_person_names(author_field)
        if identity_rules.is_specific_author(name)
    ]


def discovery_corroboration_available(candidate: Mapping[str, Any]) -> bool:
    return bool(_candidate_isbn(candidate) or _specific_names(_candidate_author_field(candidate)))


def search_queries(candidate: Mapping[str, Any]) -> list[str]:
    """Site-search queries, most specific first: each title variant."""
    return candidate_title_variants(candidate)


def latest_completed_attempt(
    attempts: Sequence[Mapping[str, Any]],
) -> Mapping[str, Any] | None:
    """The newest completed (FOUND/NOT_FOUND) attempt under the current
    rules version, from process_logs rows; None means "not yet tried".
    A rules-version bump makes every candidate eligible again."""
    completed = [
        attempt
        for attempt in attempts
        if attempt.get("process_name") == PROCESS_NAME
        and attempt.get("status") in COMPLETED_STATUSES
        and (attempt.get("error_details") or {}).get("rules_version") == DISCOVERY_RULES_VERSION
    ]
    if not completed:
        return None
    return max(completed, key=lambda attempt: str(attempt.get("created_at") or ""))


def evaluate_discovered_reference(
    candidate: Mapping[str, Any],
    page: Mapping[str, Any],
    source_type: str,
) -> DecisionResult:
    """
    Accept or reject one parsed product page (keys as collect_reference_
    metadata.py's parsers return: reference_title, reference_author,
    reference_isbn, reference_publisher, ...) as this candidate's book.
    """
    page_title = page.get("reference_title")
    evidence: dict[str, Any] = {
        "source_type": source_type,
        "reference_title": page_title,
        "reference_author": page.get("reference_author"),
        "reference_isbn": page.get("reference_isbn"),
    }

    listing_reason = listing_rejection_reason(candidate, page_title)
    if listing_reason:
        return DecisionResult(Outcome.AUTO_REJECT, DISCOVERY_LISTING_REJECTED, listing_reason, evidence=evidence)

    match_kind = title_match_kind(candidate_title_variants(candidate), page_title)
    evidence["title_match"] = match_kind
    if match_kind is None:
        return DecisionResult(
            Outcome.AUTO_REJECT,
            DISCOVERY_TITLE_MISMATCH,
            f"Page title {page_title!r} does not match the candidate title "
            f"{candidate_title(candidate)!r} exactly or as a series volume.",
            evidence=evidence,
        )

    candidate_isbn = _candidate_isbn(candidate)
    page_isbn = page.get("reference_isbn")
    isbn_match = False
    if candidate_isbn and identity_rules.looks_like_valid_isbn(page_isbn):
        if identity_rules.normalize_isbn(candidate_isbn) != identity_rules.normalize_isbn(page_isbn):
            return DecisionResult(
                Outcome.AUTO_REJECT,
                DISCOVERY_ISBN_CONFLICT,
                f"Page ISBN {page_isbn!r} differs from the candidate ISBN {candidate_isbn!r}.",
                evidence=evidence,
            )
        isbn_match = True
    evidence["isbn_match"] = isbn_match

    candidate_keys = {
        author_rules.person_name_key(name): name
        for name in _specific_names(_candidate_author_field(candidate))
    }
    page_keys = {
        author_rules.person_name_key(name): name
        for name in _specific_names(page.get("reference_author"))
    }
    shared = sorted(candidate_keys[key] for key in candidate_keys.keys() & page_keys.keys())
    evidence["shared_authors"] = shared

    if candidate_keys and page_keys and not shared:
        return DecisionResult(
            Outcome.AUTO_REJECT,
            DISCOVERY_AUTHOR_CONFLICT,
            f"Page author {page.get('reference_author')!r} shares no name with "
            f"the candidate author {_candidate_author_field(candidate)!r} -- "
            "same title, different book.",
            evidence=evidence,
        )

    if not isbn_match and not shared:
        return DecisionResult(
            Outcome.AUTO_REJECT,
            DISCOVERY_NO_CORROBORATION,
            "Title matches but neither an equal ISBN nor a shared author "
            "corroborates it; a title alone never registers a reference.",
            evidence=evidence,
        )

    conflict = identity_rules.reference_business_conflict_reason(dict(candidate), dict(page))
    if conflict:
        return DecisionResult(Outcome.AUTO_REJECT, DISCOVERY_IDENTITY_CONFLICT, conflict, evidence=evidence)

    if isbn_match:
        confidence = _CONFIDENCE_ISBN
    elif match_kind == TITLE_MATCH_EXACT:
        confidence = _CONFIDENCE_EXACT_TITLE_AUTHOR
    else:
        confidence = _CONFIDENCE_SERIES_TITLE_AUTHOR
    evidence["confidence"] = confidence

    return DecisionResult(
        outcome=Outcome.AUTO_PASS,
        rule_code=DISCOVERY_REFERENCE_ACCEPTED,
        reason=(
            f"{match_kind} title match corroborated by "
            + ("equal ISBN" if isbn_match else f"shared author {', '.join(shared)}")
            + f" on an approved {source_type} page."
        ),
        evidence=evidence,
        confidence=confidence,
    )
