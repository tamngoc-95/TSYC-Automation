"""Shared Facebook permalink ingestion service.

This is the single entry point every source-ingestion adapter (a direct
CLI call, a bounded batch inbox, or any future automatic adapter) must
use to bring one exact, already-known Facebook group post permalink into
the TSYC pipeline (source_urls, crawl_status=PENDING). Centralizing this
here means validation/normalization/dedupe logic exists in exactly one
place instead of being reimplemented per adapter.

Architectural boundary -- READ THIS BEFORE CHANGING EITHER SIDE:

    EXISTING: scripts/collect_one_facebook_post.py
        Takes one already-registered source_urls row (crawl_status=
        PENDING) and opens that *exact* URL with the authenticated
        Playwright profile to extract the post's content. Unchanged by
        this module. Remains the sole authority for actually opening a
        Facebook page.

    NEW: this module + scripts/ingest_source_urls.py
        Takes an exact permalink URL a caller already has in hand (from
        wherever -- an operator's clipboard, a batch file, or in the
        future a real Meta integration) and turns it into a valid
        source_urls row. Never opens a browser. Never visits Facebook.
        Never crawls or scrolls a group feed to *discover* URLs -- it only
        validates and registers URLs already supplied by the caller.

This module performs no browsing/crawling/discovery of any kind. It does
not import playwright and must never be made to.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse


# The one TSYC group this pipeline is authorized to accept posts from.
# Intentionally duplicated (not imported) from scripts/collect_one_facebook
# _post.py's own AUTHORIZED_GROUP_ID constant: that script is explicitly
# left unmodified by this change, and this module must not import a
# script module (scripts/ is not a package other code should depend on).
# tests/test_source_ingestion.py locks these two literals in sync.
AUTHORIZED_GROUP_ID = "2415122391976246"

_ALLOWED_HOSTNAMES = frozenset({"facebook.com", "www.facebook.com", "m.facebook.com"})

# Matches exactly "/groups/<group_id>/permalink/<post_id>" (optionally with
# a trailing slash and/or a query string, which urlparse already splits
# off into .query so it never reaches this pattern). No other Facebook
# path shape (a profile, a photo viewer, a comment anchor, another
# group, etc.) matches this.
# [0-9], not \d: Python's \d matches any Unicode decimal-digit character
# (full-width, Arabic-Indic, Devanagari, ...), not just ASCII 0-9. A
# non-ASCII "digit" in group_id can never equal the ASCII
# authorized_group_id literal (so that check still fails safe), but a
# non-ASCII post_id would otherwise be embedded verbatim into the
# "canonical" URL this module writes to source_urls -- violating the
# "exact, well-formed permalink, never fabricated" contract documented
# above. [0-9] rules that out entirely.
_PERMALINK_PATH_RE = re.compile(
    r"^/groups/(?P<group_id>[0-9]+)/permalink/(?P<post_id>[0-9]+)/?$"
)


class SourceValidationError(ValueError):
    """Raised when a supplied URL is not an acceptable exact permalink."""


@dataclass(frozen=True)
class IngestOutcome:
    """The result of one ingest_facebook_post_url() call.

    status is one of:
        REGISTERED     -- a new source_urls row was created (PENDING).
        ALREADY_KNOWN  -- this exact normalized URL already had a row;
                          the existing row is returned untouched (its
                          crawl_status is never reset).
        SOURCE_INVALID -- the URL failed validation; nothing was read or
                          written to source_urls.
    """

    status: str
    input_url: str
    canonical_url: str | None
    source_url_id: str | None
    reason: str | None = None
    record: dict[str, Any] | None = None


def normalize_facebook_permalink(
    url: str,
    *,
    authorized_group_id: str = AUTHORIZED_GROUP_ID,
) -> str:
    """Validate one URL and return its canonical permalink form.

    Raises SourceValidationError (never guesses, never fabricates a post
    ID) for anything that is not an exact, well-formed permalink in the
    authorized group -- a foreign group, a profile, a comment anchor, a
    photo-viewer link, a malformed/placeholder URL, or a non-Facebook
    host.
    """
    raw = (url or "").strip()

    if not raw:
        raise SourceValidationError("URL is empty.")

    if raw in {"<URL>", "<url>", "URL", "TODO", "..."}:
        raise SourceValidationError("URL is an unfilled placeholder, not a real URL.")

    try:
        parsed = urlparse(raw)
    except ValueError as error:
        raise SourceValidationError(f"URL could not be parsed: {error}") from error

    if parsed.scheme not in ("https", "http"):
        raise SourceValidationError(
            f"URL scheme must be http(s), got: {parsed.scheme or '(none)'}"
        )

    hostname = (parsed.hostname or "").lower()

    if hostname not in _ALLOWED_HOSTNAMES:
        raise SourceValidationError(
            f"URL host is not an authorized Facebook host: {hostname or '(none)'}"
        )

    match = _PERMALINK_PATH_RE.match(parsed.path)

    if not match:
        raise SourceValidationError(
            "URL is not a supported group permalink form "
            "(/groups/<id>/permalink/<id>/)."
        )

    url_group_id = match.group("group_id")

    if url_group_id != authorized_group_id:
        raise SourceValidationError(
            f"URL belongs to group {url_group_id}, not the authorized "
            f"group {authorized_group_id}."
        )

    post_id = match.group("post_id")

    return f"https://www.facebook.com/groups/{authorized_group_id}/permalink/{post_id}/"


def _find_existing_source(
    repository: Any,
    batch_id: str,
    canonical_url: str,
) -> dict[str, Any] | None:
    """Read-only lookup: does this exact normalized URL already have a
    source_urls row in this batch? Never mutates anything."""
    rows = (
        repository.client.table("source_urls")
        .select("*")
        .eq("batch_id", batch_id)
        .eq("source_type", "FACEBOOK_POST")
        .eq("source_url", canonical_url)
        .limit(1)
        .execute()
        .data
        or []
    )

    return rows[0] if rows else None


def ingest_facebook_post_url(
    repository: Any,
    url: str,
    batch_id: str,
    *,
    authorized_group_id: str = AUTHORIZED_GROUP_ID,
    source_name: str | None = None,
) -> IngestOutcome:
    """Validate, normalize, dedupe, and (if new) register one exact
    Facebook group post permalink as a PENDING source_urls row.

    Idempotent: calling this twice with the same URL returns the
    existing record the second time (status="ALREADY_KNOWN") instead of
    creating a duplicate row or resetting the existing row's
    crawl_status back to PENDING -- SupabaseRepository.save_source_url()
    upserts unconditionally on conflict, which would silently reset an
    already-COLLECTED source; this function's existence check is what
    prevents that from ever being reached for a known URL.
    """
    try:
        canonical_url = normalize_facebook_permalink(
            url, authorized_group_id=authorized_group_id
        )
    except SourceValidationError as error:
        return IngestOutcome(
            status="SOURCE_INVALID",
            input_url=url,
            canonical_url=None,
            source_url_id=None,
            reason=str(error),
        )

    existing = _find_existing_source(repository, batch_id, canonical_url)

    if existing is not None:
        return IngestOutcome(
            status="ALREADY_KNOWN",
            input_url=url,
            canonical_url=canonical_url,
            source_url_id=str(existing["source_url_id"]),
            record=existing,
        )

    record = repository.save_source_url(
        batch_id=batch_id,
        source_url=canonical_url,
        selection_reason=source_name,
        active=True,
        source_type="FACEBOOK_POST",
    )

    return IngestOutcome(
        status="REGISTERED",
        input_url=url,
        canonical_url=canonical_url,
        source_url_id=str(record["source_url_id"]),
        record=record,
    )


def ingest_facebook_post_urls(
    repository: Any,
    urls: list[str],
    batch_id: str,
    *,
    authorized_group_id: str = AUTHORIZED_GROUP_ID,
    max_sources: int | None = None,
) -> list[IngestOutcome]:
    """Ingest a bounded batch of exact URLs, one at a time, in order.

    A duplicate normalized URL *within the same input batch* is only
    ingested once -- the second occurrence is reported ALREADY_KNOWN
    against the first's outcome without a second Supabase read/write.
    """
    if max_sources is not None and len(urls) > max_sources:
        raise ValueError(
            f"{len(urls)} URL(s) supplied exceeds --max-sources ({max_sources}). "
            "Failing before any read or write."
        )

    outcomes: list[IngestOutcome] = []
    seen_canonical: dict[str, IngestOutcome] = {}

    for url in urls:
        try:
            canonical_url = normalize_facebook_permalink(
                url, authorized_group_id=authorized_group_id
            )
        except SourceValidationError as error:
            outcomes.append(
                IngestOutcome(
                    status="SOURCE_INVALID",
                    input_url=url,
                    canonical_url=None,
                    source_url_id=None,
                    reason=str(error),
                )
            )
            continue

        if canonical_url in seen_canonical:
            prior = seen_canonical[canonical_url]
            outcomes.append(
                IngestOutcome(
                    status="ALREADY_KNOWN",
                    input_url=url,
                    canonical_url=canonical_url,
                    source_url_id=prior.source_url_id,
                    reason="Duplicate of another URL earlier in this same batch.",
                    record=prior.record,
                )
            )
            continue

        outcome = ingest_facebook_post_url(
            repository,
            url,
            batch_id,
            authorized_group_id=authorized_group_id,
        )
        seen_canonical[canonical_url] = outcome
        outcomes.append(outcome)

    return outcomes
