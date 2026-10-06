"""Regression tests for automatic approved-source reference discovery
(2026-10-02: 5/5 Fast Track candidates stalled at content review because
no reference was ever searched for)."""
from __future__ import annotations

import subprocess
import types
from typing import Any

import pytest

import discover_reference_sources as discoverer
from src.domain.reference_sources import REFERENCE_SOURCE_PRIORITY
from src.domain.rules import identity_rules
from src.domain.rules import reference_discovery_rules as rules

from support.fake_supabase import FakeSupabaseRepository

CANDIDATE_ID = "d15c0000-0000-0000-0000-000000000001"
CODE = "FB-HIST-2026-IMG-004-CAN-0044"


def _candidate(**overrides: Any) -> dict[str, Any]:
    row = {
        "candidate_id": CANDIDATE_ID,
        "candidate_code": CODE,
        "batch_id": "batch-1",
        "candidate_type": "SINGLE_BOOK",
        "identity_status": "IDENTITY_PENDING",
        "extracted_title": "Tia Nắng Bé Con",
        "extracted_author": "ThS. Ngô Nam; Minh họa: Khánh Chi",
        "verified_title": None,
        "verified_author": None,
        "verified_isbn": None,
        "possible_isbn": None,
        "verified_publisher": None,
    }
    row.update(overrides)
    return row


def _page(**overrides: Any) -> dict[str, Any]:
    row = {
        "reference_title": "Bé Làm Quen Với Vật Lý - Tia Nắng Bé Con",
        "reference_author": "Ngô Nam",
        "reference_isbn": None,
        "reference_publisher": "NXB Thanh Niên",
        "reference_description": "Mô tả sách.",
        "reference_image_url": "https://cdn.fahasa.com/x.jpg",
    }
    row.update(overrides)
    return row


# --- eligibility --------------------------------------------------------------


def test_single_book_with_author_is_eligible():
    assert rules.evaluate_discovery_eligibility(_candidate()).is_auto_pass


@pytest.mark.parametrize(
    "overrides, fragment",
    [
        ({"candidate_type": "BOOK_COMBO"}, "not a single book"),
        ({"extracted_author": None}, "no author or valid ISBN"),
        ({"extracted_author": "Nhiều tác giả"}, "no author or valid ISBN"),
        ({"identity_status": "IDENTITY_CONFLICT"}, "IDENTITY_CONFLICT"),
        ({"extracted_title": ""}, "no meaningful title"),
    ],
)
def test_ineligible_candidates(overrides, fragment):
    decision = rules.evaluate_discovery_eligibility(_candidate(**overrides))
    assert not decision.is_auto_pass
    assert fragment in decision.reason


def test_valid_isbn_alone_makes_a_candidate_eligible_but_893_barcode_does_not():
    assert rules.evaluate_discovery_eligibility(
        _candidate(extracted_author=None, possible_isbn="9786041234567")
    ).is_auto_pass
    assert not rules.evaluate_discovery_eligibility(
        _candidate(extracted_author=None, possible_isbn="8935235225555")
    ).is_auto_pass


def test_sites_follow_canonical_source_priority():
    priorities = [REFERENCE_SOURCE_PRIORITY[site.source_type] for site in rules.DISCOVERY_SITES]
    assert priorities == sorted(priorities)
    assert rules.DISCOVERY_SITES[0].source_type == "BOOKSTORE"


# --- title matching -------------------------------------------------------------


@pytest.mark.parametrize(
    "page_title, expected",
    [
        ("Tia Nắng Bé Con", rules.TITLE_MATCH_EXACT),
        ("Tia nắng bé con!", rules.TITLE_MATCH_EXACT),
        ("Tia Nắng Bé Con (Tái Bản 2023)", rules.TITLE_MATCH_EXACT),
        ("Bé Làm Quen Với Vật Lý - Tia Nắng Bé Con", rules.TITLE_MATCH_SERIES_PREFIX),
        ("Kết Nối Yêu Thương: Tia Nắng Bé Con", rules.TITLE_MATCH_SERIES_PREFIX),
        ("Những Tia Nắng Đầu Tiên", None),
        ("Tia Nắng Bé Con Và Bạn Gió", None),
        ("Tia Năng Be Con", None),  # diacritics distinguish real titles
    ],
)
def test_title_match_kind(page_title, expected):
    assert rules.title_match_kind(["Tia Nắng Bé Con"], page_title) == expected


def test_one_word_volume_title_never_matches_by_series_form():
    assert rules.title_match_kind(["Đảo"], "Tranh Truyện Việt Nam - Đảo") is None


def test_parenthetical_subtitle_variant():
    candidate = _candidate(extracted_title="Phụ Nữ (The Book of Women)")
    assert rules.candidate_title_variants(candidate) == ["Phụ Nữ (The Book of Women)", "Phụ Nữ"]


def test_title_slug_and_listing_text():
    assert rules.title_slug("Tư Duy Ngược") == "tu-duy-nguoc"
    assert rules.title_slug("Đòn Bẩy Diệu Kì") == "don-bay-dieu-ki"
    assert rules.listing_text_title("Truyện Tranh > Tia Nắng Bé Con") == "Tia Nắng Bé Con"


# --- page evaluation ------------------------------------------------------------


def test_series_volume_page_with_shared_author_is_accepted():
    decision = rules.evaluate_discovered_reference(_candidate(), _page(), "FAHASA")

    assert decision.is_auto_pass
    assert decision.evidence["title_match"] == rules.TITLE_MATCH_SERIES_PREFIX
    assert decision.evidence["shared_authors"] == ["Ngô Nam"]
    assert decision.confidence == pytest.approx(0.88)


def test_same_title_different_author_is_rejected():
    """Real case: netabooks.vn/tu-duy-nguoc is Jonah Sachs' book, not the
    shop's Nguyễn Anh Dũng title of the same name."""
    candidate = _candidate(extracted_title="Tư Duy Ngược", extracted_author="Nguyễn Anh Dũng")
    page = _page(reference_title="Tư Duy Ngược", reference_author="Jonah Sachs")

    decision = rules.evaluate_discovered_reference(candidate, page, "BOOKSTORE")

    assert decision.outcome == "AUTO_REJECT"
    assert decision.rule_code == rules.DISCOVERY_AUTHOR_CONFLICT


def test_title_alone_never_registers_a_reference():
    decision = rules.evaluate_discovered_reference(
        _candidate(), _page(reference_title="Tia Nắng Bé Con", reference_author=None), "FAHASA"
    )
    assert decision.rule_code == rules.DISCOVERY_NO_CORROBORATION


def test_equal_isbn_corroborates_and_differing_isbn_rejects():
    candidate = _candidate(extracted_author=None, possible_isbn="978-604-1-23456-7")
    accepted = rules.evaluate_discovered_reference(
        candidate, _page(reference_title="Tia Nắng Bé Con", reference_author=None, reference_isbn="9786041234567"), "FAHASA"
    )
    rejected = rules.evaluate_discovered_reference(
        candidate, _page(reference_title="Tia Nắng Bé Con", reference_isbn="9786049999999"), "FAHASA"
    )

    assert accepted.is_auto_pass and accepted.confidence == pytest.approx(0.97)
    assert rejected.rule_code == rules.DISCOVERY_ISBN_CONFLICT


@pytest.mark.parametrize(
    "page_title, code",
    [
        ("[Phiên chợ sách cũ] Tia Nắng Bé Con", rules.DISCOVERY_LISTING_REJECTED),
        ("Combo Tia Nắng Bé Con", rules.DISCOVERY_LISTING_REJECTED),
        ("Những Tia Nắng Đầu Tiên - Chờ Một Tí!", rules.DISCOVERY_TITLE_MISMATCH),
    ],
)
def test_wrong_listings_are_rejected(page_title, code):
    decision = rules.evaluate_discovered_reference(_candidate(), _page(reference_title=page_title), "FAHASA")
    assert decision.rule_code == code


def test_shared_conflict_guard_accepts_series_volume_title():
    """The draft-safe content/image guard must not undo a series match."""
    candidate = _candidate()
    assert identity_rules.is_series_volume_title("Tia Nắng Bé Con", "Bé Làm Quen Với Vật Lý - Tia Nắng Bé Con")
    assert identity_rules.reference_business_conflict_reason(candidate, _page()) is None
    assert identity_rules.reference_business_conflict_reason(
        candidate, _page(reference_title="Vũ Trụ Kỳ Thú Của Bé")
    ) is not None


# --- attempt bookkeeping ----------------------------------------------------------


def test_latest_completed_attempt_respects_status_and_rules_version():
    attempts = [
        {"process_name": rules.PROCESS_NAME, "status": rules.STATUS_ERROR,
         "error_details": {"rules_version": rules.DISCOVERY_RULES_VERSION}, "created_at": "3"},
        {"process_name": rules.PROCESS_NAME, "status": rules.STATUS_NOT_FOUND,
         "error_details": {"rules_version": "0.9.0"}, "created_at": "2"},
    ]
    assert rules.latest_completed_attempt(attempts) is None

    attempts.append(
        {"process_name": rules.PROCESS_NAME, "status": rules.STATUS_NOT_FOUND,
         "error_details": {"rules_version": rules.DISCOVERY_RULES_VERSION}, "created_at": "1"}
    )
    assert rules.latest_completed_attempt(attempts)["created_at"] == "1"


# --- discover_reference_sources.py --------------------------------------------------


class FakeSource:
    def __init__(self, search_results: dict[str, list[tuple[str, str]]], pages: dict[str, dict[str, Any]]):
        self.search_results = search_results
        self.pages = pages
        self.fetched: list[str] = []

    def search(self, url: str) -> list[tuple[str, str]]:
        host = "fahasa" if "fahasa" in url else "netabooks"
        return self.search_results.get(host, [])

    def fetch_metadata(self, url: str) -> dict[str, Any] | None:
        self.fetched.append(url)
        return self.pages.get(url)


FAHASA_URL = "https://www.fahasa.com/be-lam-quen-voi-vat-ly-tia-nang-be-con.html"


def test_plan_site_urls_prefilters_by_listing_title_and_adds_slug_guess():
    site = rules.DISCOVERY_SITES[1]  # Fahasa
    links = [
        (FAHASA_URL + "?fhs_campaign=SEARCH", "Bé Làm Quen Với Vật Lý - Tia Nắng Bé Con"),
        ("https://www.fahasa.com/nhung-tia-nang-dau-tien.html?fhs_campaign=SEARCH", "Những Tia Nắng Đầu Tiên"),
        ("https://www.fahasa.com/sach-trong-nuoc.html", "Sách Trong Nước"),
    ]
    urls = discoverer.plan_site_urls(_candidate(), site, links)

    assert urls == [FAHASA_URL, "https://www.fahasa.com/tia-nang-be-con.html"]


def test_discover_returns_first_accepted_page_with_evidence():
    source = FakeSource(
        {"fahasa": [(FAHASA_URL, "Bé Làm Quen Với Vật Lý - Tia Nắng Bé Con")]},
        {FAHASA_URL: _page()},
    )
    result = discoverer.discover(_candidate(), source)

    assert result["status"] == rules.STATUS_FOUND
    assert result["selected"]["url"] == FAHASA_URL
    assert result["selected"]["source_type"] == "FAHASA"
    assert any(a["outcome"] == "AUTO_PASS" for a in result["attempts"])


def test_discover_not_found_when_only_conflicting_pages_exist():
    source = FakeSource(
        {"fahasa": [(FAHASA_URL, "Bé Làm Quen Với Vật Lý - Tia Nắng Bé Con")]},
        {FAHASA_URL: _page(reference_author="Người Khác Hẳn")},
    )
    result = discoverer.discover(_candidate(), source)

    assert result["status"] == rules.STATUS_NOT_FOUND
    assert result["selected"] is None


def _repository(**extra: list[dict[str, Any]]) -> FakeSupabaseRepository:
    tables = {
        "product_candidates": [_candidate()],
        "batches": [{"batch_id": "batch-1", "batch_code": "FB-HIST-2026-IMG-004"}],
    }
    tables.update(extra)
    return FakeSupabaseRepository(tables=tables)


def _args(**overrides: Any) -> types.SimpleNamespace:
    values = {"candidate_code": CODE, "non_interactive": True, "confirm_discover": True, "dry_run": False}
    values.update(overrides)
    return types.SimpleNamespace(**values)


def _found_source() -> FakeSource:
    return FakeSource(
        {"fahasa": [(FAHASA_URL, "Bé Làm Quen Với Vật Lý - Tia Nắng Bé Con")]},
        {FAHASA_URL: _page()},
    )


def test_run_registers_through_existing_writer_and_logs_attempt():
    repository = _repository()
    calls: list[list[str]] = []

    def runner(command: list[str]) -> subprocess.CompletedProcess:
        calls.append(command)
        repository.client.table("candidate_reference_sources").insert(
            {"candidate_id": CANDIDATE_ID, "discovery_status": "SELECTED", "is_selected_for_crawl": True}
        ).execute()
        return subprocess.CompletedProcess(command, 0, "ok", "")

    result = discoverer.run(repository, _args(), _found_source, runner)

    assert result["status"] == rules.STATUS_FOUND
    command = calls[0]
    assert command[1].endswith("register_reference_source.py")
    for flag in ("--authorized", "--select-for-crawl", "--confirm-register", "--non-interactive"):
        assert flag in command
    assert command[command.index("--source-type") + 1] == "FAHASA"
    assert command[command.index("--discovery-method") + 1] == "SITE_SEARCH"
    assert command[command.index("--batch-code") + 1] == "FB-HIST-2026-IMG-004"
    logs = repository.client.tables["process_logs"]
    assert logs[-1]["status"] == rules.STATUS_FOUND
    assert logs[-1]["error_details"]["rules_version"] == rules.DISCOVERY_RULES_VERSION


def test_run_not_found_logs_completed_attempt_without_registering():
    repository = _repository()
    calls: list[list[str]] = []

    result = discoverer.run(
        repository, _args(), lambda: FakeSource({}, {}), lambda c: calls.append(c)
    )

    assert result["status"] == rules.STATUS_NOT_FOUND
    assert calls == []
    assert repository.client.tables["process_logs"][-1]["status"] == rules.STATUS_NOT_FOUND


def test_dry_run_writes_nothing():
    repository = _repository()
    result = discoverer.run(repository, _args(dry_run=True), _found_source, lambda c: pytest.fail("no write"))

    assert result["dry_run"] is True
    assert repository.client.tables.get("process_logs", []) == []


def test_site_failure_is_logged_as_error_not_as_completed_attempt():
    class Broken(FakeSource):
        def search(self, url: str):
            raise ConnectionError("site down")

    repository = _repository()
    with pytest.raises(ConnectionError):
        discoverer.run(repository, _args(), lambda: Broken({}, {}), lambda c: None)

    log = repository.client.tables["process_logs"][-1]
    assert log["status"] == rules.STATUS_ERROR
    assert rules.latest_completed_attempt(repository.client.tables["process_logs"]) is None


@pytest.mark.parametrize(
    "extra, fragment",
    [
        ({"product_references": [{"candidate_id": CANDIDATE_ID}]}, "product_references"),
        ({"candidate_reference_sources": [{"candidate_id": CANDIDATE_ID}]}, "candidate_reference_sources"),
        (
            {"process_logs": [{"candidate_id": CANDIDATE_ID, "process_name": rules.PROCESS_NAME,
                               "status": rules.STATUS_NOT_FOUND, "created_at": "1",
                               "error_details": {"rules_version": rules.DISCOVERY_RULES_VERSION}}]},
            "completed discovery attempt",
        ),
    ],
)
def test_run_refuses_when_preconditions_fail(extra, fragment):
    with pytest.raises(RuntimeError, match=fragment):
        discoverer.run(_repository(**extra), _args(), _found_source, lambda c: None)


def test_non_interactive_requires_confirmation():
    with pytest.raises(RuntimeError, match="--confirm-discover"):
        discoverer.run(_repository(), _args(confirm_discover=False), _found_source, lambda c: None)


def test_live_candidates_are_refused():
    repository = FakeSupabaseRepository(
        tables={"product_candidates": [_candidate(candidate_code="FB-2026-001-CAN-0001")]}
    )
    with pytest.raises(RuntimeError, match="historical-only"):
        discoverer.run(repository, _args(candidate_code="FB-2026-001-CAN-0001"), _found_source, lambda c: None)
