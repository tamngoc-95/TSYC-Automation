"""
Automatic approved-source reference discovery for ONE historical candidate.

Pipeline position: scripts/run_batch.py dispatches this for derived state
REFERENCE_DISCOVERY_PENDING_HISTORICAL (an FB-HIST single book with no
reference source at all). It:

  1. searches each approved site in CLAUDE.md 8.1 priority order
     (src.domain.rules.reference_discovery_rules.DISCOVERY_SITES) with the
     candidate's title, plus a direct slug-URL guess;
  2. prefilters result links whose listing title matches the candidate
     title (exact or series-volume form) -- at most MAX_PAGES_PER_SITE
     pages per site are opened;
  3. parses each page with collect_reference_metadata.py's own parsers
     and accepts it only through reference_discovery_rules.
     evaluate_discovered_reference() (title + author/ISBN corroboration,
     no identity/sellable-unit conflict);
  4. registers the first accepted page through the existing writer
     register_reference_source.py (authorized, selected for crawl,
     discovery_method=SITE_SEARCH, confidence + evidence persisted);
  5. records the completed attempt (FOUND / NOT_FOUND, every page it
     looked at and why it was accepted/rejected) in process_logs, so the
     state machine never searches the same candidate twice under the
     same rules version.

The existing stages then take over unchanged: REFERENCE_REGISTERED ->
collect_reference_metadata.py -> match_candidate_identity.py -> ...

Never writes product_references, identity, content, images, prices or
anything in WooCommerce. A site/network failure is recorded as ERROR (not
a completed attempt) and exits non-zero; nothing is registered.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Protocol
from urllib.parse import quote_plus, urlsplit, urlunsplit

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv  # noqa: E402

from create_internal_product import is_historical_candidate_code  # noqa: E402
from src.cli_bootstrap import configure_utf8_console  # noqa: E402
from src.domain.rules import reference_discovery_rules as rules  # noqa: E402
from src.repositories.supabase_repository import SupabaseRepository  # noqa: E402

DISCOVERER_NAME = "reference_discoverer"
DISCOVERER_VERSION = "1.0.0"
MAX_PAGES_PER_SITE = 3
PYTHON_EXE = PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"


class PageSource(Protocol):
    """Live-site access, injectable for offline tests."""

    def search(self, url: str) -> list[tuple[str, str]]:
        """(href, link_text) pairs on a rendered search-result page."""

    def fetch_metadata(self, url: str) -> dict[str, Any] | None:
        """Parsed product metadata for one page, or None (404/not a book)."""


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Discover and register an approved-source reference for one FB-HIST candidate.",
    )
    parser.add_argument("--candidate-code", required=True, help="Exact candidate code.")
    parser.add_argument("--non-interactive", action="store_true", help="Disable prompts.")
    parser.add_argument(
        "--confirm-discover",
        action="store_true",
        help="Required with --non-interactive: register an accepted page and log the attempt.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Search and evaluate only; never registers or logs anything.",
    )
    return parser.parse_args()


def canonical_url(href: str) -> str:
    """Drop query string and fragment (?fhs_campaign=SEARCH, #...)."""
    parts = urlsplit(href)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def plan_site_urls(
    candidate: dict[str, Any],
    site: rules.DiscoverySite,
    search_links: list[tuple[str, str]],
) -> list[str]:
    """Product URLs worth opening on one site, in order: search hits whose
    listing title matches the candidate, then the slug guess. Pure."""
    titles = rules.candidate_title_variants(candidate)
    urls: list[str] = []
    for href, text in search_links:
        url = canonical_url(href)
        if (
            site.is_product_url(url)
            and rules.title_match_kind(titles, rules.listing_text_title(text))
            and url not in urls
        ):
            urls.append(url)
    for title in titles:
        slug = rules.title_slug(title)
        guess = site.slug_url_template.format(slug=slug) if slug else ""
        if guess and site.is_product_url(guess) and guess not in urls:
            urls.append(guess)
    return urls[:MAX_PAGES_PER_SITE]


def discover(
    candidate: dict[str, Any],
    source: PageSource,
) -> dict[str, Any]:
    """Run discovery across DISCOVERY_SITES; stop at the first accepted
    page (highest source priority wins). Returns a JSON-safe result."""
    attempts: list[dict[str, Any]] = []
    for site in rules.DISCOVERY_SITES:
        search_links: list[tuple[str, str]] = []
        queries = rules.search_queries(candidate)
        for query in queries:
            search_links.extend(
                source.search(site.search_url_template.format(query=quote_plus(query)))
            )
        for rank, url in enumerate(plan_site_urls(candidate, site, search_links), start=1):
            metadata = source.fetch_metadata(url)
            if not metadata:
                attempts.append({"site": site.name, "url": url, "outcome": "NOT_A_PRODUCT_PAGE"})
                continue
            decision = rules.evaluate_discovered_reference(candidate, metadata, site.source_type)
            attempts.append(
                {
                    "site": site.name,
                    "url": url,
                    "outcome": decision.outcome,
                    "rule_code": decision.rule_code,
                    "reason": decision.reason,
                    "evidence": dict(decision.evidence),
                    # What collection will be able to offer downstream
                    # (content enrichment / price-free image fallback).
                    "description_length": len(str(metadata.get("reference_description") or "")),
                    "has_image_url": bool(metadata.get("reference_image_url")),
                }
            )
            if decision.is_auto_pass:
                return {
                    "status": rules.STATUS_FOUND,
                    "selected": {
                        "site": site.name,
                        "source_type": site.source_type,
                        "url": url,
                        "query": queries[0],
                        "rank": rank,
                        "confidence": decision.confidence,
                        "reason": decision.reason,
                    },
                    "attempts": attempts,
                }
    return {"status": rules.STATUS_NOT_FOUND, "selected": None, "attempts": attempts}


class PlaywrightPageSource:
    """Live implementation reusing collect_reference_metadata.py's browser
    setup and domain parsers (one parser implementation, not two)."""

    def __init__(self) -> None:
        from playwright.sync_api import sync_playwright

        import collect_reference_metadata as collector

        self._collector = collector
        self._playwright = sync_playwright().start()
        self._browser, self._context = collector.create_browser(self._playwright)

    def close(self) -> None:
        self._browser.close()
        self._playwright.stop()

    def search(self, url: str) -> list[tuple[str, str]]:
        page = self._context.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=self._collector.NAVIGATION_TIMEOUT_MS)
            try:
                page.wait_for_load_state("networkidle", timeout=15_000)
            except self._collector.PlaywrightTimeoutError:
                pass
            return [
                (str(href), str(text))
                for href, text in page.eval_on_selector_all(
                    "a[href]",
                    "els => els.map(e => [e.href, (e.innerText || '').trim()])",
                )
            ]
        finally:
            page.close()

    def fetch_metadata(self, url: str) -> dict[str, Any] | None:
        # A missing page (slug guess 404) parses to a "not found" title or
        # no title at all; either way evaluate_discovered_reference()
        # rejects it on the title -- no separate status probe needed.
        page, raw_text, _raw_html, _title = self._collector.collect_page(self._context, url)
        try:
            parser_name = self._collector.select_parser(url)
            metadata = self._collector.parse_reference_page(parser_name, page, raw_text)
        finally:
            page.close()
        return metadata if metadata.get("reference_title") else None


def load_candidate(repository: SupabaseRepository, candidate_code: str) -> dict[str, Any]:
    rows = (
        repository.client.table("product_candidates").select("*")
        .eq("candidate_code", candidate_code).limit(2).execute().data or []
    )
    if len(rows) != 1:
        raise RuntimeError(f"candidate_code did not resolve to exactly one candidate: {candidate_code}")
    return rows[0]


def assert_discovery_allowed(repository: SupabaseRepository, candidate: dict[str, Any]) -> None:
    """Same preconditions pipeline_state.py uses for REFERENCE_DISCOVERY_
    PENDING_HISTORICAL, re-checked fresh before any search or write."""
    code = candidate["candidate_code"]
    if not is_historical_candidate_code(code):
        raise RuntimeError(f"{code} is not an FB-HIST candidate; automatic discovery is historical-only.")
    eligibility = rules.evaluate_discovery_eligibility(candidate)
    if not eligibility.is_auto_pass:
        raise RuntimeError(eligibility.reason)
    candidate_id = candidate["candidate_id"]
    for table in ("product_references", "candidate_reference_sources"):
        existing = (
            repository.client.table(table).select("candidate_id")
            .eq("candidate_id", candidate_id).limit(1).execute().data or []
        )
        if existing:
            raise RuntimeError(f"{code} already has {table} rows; discovery only runs when none exist.")
    attempts = (
        repository.client.table("process_logs")
        .select("process_log_id, candidate_id, process_name, status, error_details, created_at")
        .eq("candidate_id", candidate_id).eq("process_name", rules.PROCESS_NAME)
        .execute().data or []
    )
    if rules.latest_completed_attempt(attempts) is not None:
        raise RuntimeError(f"{code} already has a completed discovery attempt (rules {rules.DISCOVERY_RULES_VERSION}).")


def batch_code_for(repository: SupabaseRepository, batch_id: str) -> str:
    rows = (
        repository.client.table("batches").select("batch_code")
        .eq("batch_id", batch_id).limit(1).execute().data or []
    )
    if not rows:
        raise RuntimeError(f"Batch was not found for batch_id {batch_id}.")
    return rows[0]["batch_code"]


def build_register_command(candidate_code: str, batch_code: str, selected: dict[str, Any]) -> list[str]:
    selection_reason = (
        f"Automatic discovery ({DISCOVERER_NAME} {DISCOVERER_VERSION}, rules "
        f"{rules.DISCOVERY_RULES_VERSION}): {selected['reason']}"
    )
    return [
        str(PYTHON_EXE),
        str(PROJECT_ROOT / "scripts" / "register_reference_source.py"),
        "--candidate-code", candidate_code,
        "--batch-code", batch_code,
        "--source-type", selected["source_type"],
        "--source-name", selected["site"],
        "--source-url", selected["url"],
        "--discovery-method", "SITE_SEARCH",
        "--discovery-query", selected["query"],
        "--discovery-rank", str(selected["rank"]),
        "--discovery-confidence", str(selected["confidence"]),
        "--selection-reason", selection_reason[:900],
        "--authorized",
        "--select-for-crawl",
        "--confirm-register",
        "--non-interactive",
    ]


def verify_registered(repository: SupabaseRepository, candidate_id: str) -> None:
    rows = (
        repository.client.table("candidate_reference_sources")
        .select("discovery_id, discovery_status, is_selected_for_crawl")
        .eq("candidate_id", candidate_id).execute().data or []
    )
    if not any(r.get("discovery_status") == "SELECTED" and r.get("is_selected_for_crawl") for r in rows):
        raise RuntimeError("Registration did not produce a SELECTED, crawl-selected discovery row.")


def run(
    repository: SupabaseRepository,
    args: argparse.Namespace,
    source_factory: Callable[[], PageSource],
    runner: Callable[[list[str]], subprocess.CompletedProcess],
) -> dict[str, Any]:
    if args.non_interactive and not args.dry_run and not args.confirm_discover:
        raise RuntimeError("--non-interactive requires --confirm-discover (or --dry-run).")

    candidate = load_candidate(repository, args.candidate_code)
    assert_discovery_allowed(repository, candidate)

    source = source_factory()
    try:
        result = discover(candidate, source)
    except Exception as error:
        if not args.dry_run:
            repository.write_process_log(
                message=f"Reference discovery could not complete: {type(error).__name__}: {error}"[:1000],
                process_name=rules.PROCESS_NAME,
                candidate_id=candidate["candidate_id"],
                process_step="SEARCH",
                log_level="ERROR",
                status=rules.STATUS_ERROR,
                error_details={"rules_version": rules.DISCOVERY_RULES_VERSION},
            )
        raise
    finally:
        close = getattr(source, "close", None)
        if close:
            close()

    result["candidate_code"] = candidate["candidate_code"]
    result["rules_version"] = rules.DISCOVERY_RULES_VERSION
    if args.dry_run:
        result["dry_run"] = True
        return result

    if result["status"] == rules.STATUS_FOUND:
        command = build_register_command(
            candidate["candidate_code"],
            batch_code_for(repository, candidate["batch_id"]),
            result["selected"],
        )
        completed = runner(command)
        if completed.returncode != 0:
            raise RuntimeError(
                "register_reference_source.py failed: "
                + (completed.stdout or "")[-800:] + (completed.stderr or "")[-400:]
            )
        verify_registered(repository, candidate["candidate_id"])

    repository.write_process_log(
        message=(
            f"Reference discovery {result['status']} for {candidate['candidate_code']}"
            + (f": {result['selected']['url']}" if result["selected"] else "")
        ),
        process_name=rules.PROCESS_NAME,
        candidate_id=candidate["candidate_id"],
        process_step="DISCOVER",
        log_level="INFO",
        status=result["status"],
        error_details={
            "rules_version": rules.DISCOVERY_RULES_VERSION,
            "discoverer": f"{DISCOVERER_NAME} {DISCOVERER_VERSION}",
            "selected": result["selected"],
            "attempts": result["attempts"],
        },
    )
    return result


def _subprocess_runner(command: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=PROJECT_ROOT)


def main() -> None:
    configure_utf8_console()
    load_dotenv(PROJECT_ROOT / ".env")
    args = parse_arguments()
    result = run(SupabaseRepository(), args, PlaywrightPageSource, _subprocess_runner)
    print(f"Candidate: {result['candidate_code']}")
    print(f"Discovery status: {result['status']}{' (dry run, nothing written)' if result.get('dry_run') else ''}")
    for attempt in result["attempts"]:
        print(f"  [{attempt.get('outcome')}] {attempt['url']} -- {attempt.get('reason', '')}")
    if result["selected"]:
        print(f"Selected: {result['selected']['source_type']} {result['selected']['url']}")
    print("DISCOVERY_RESULT " + json.dumps(result, ensure_ascii=False, default=str))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"Reference discovery failed: {type(error).__name__}: {error}")
        sys.exit(1)
