"""Offline tests for the read-only Fahasa new-book discovery script."""
from __future__ import annotations

import discover_fahasa_new_books as discovery
from src.domain.rules import catalog_identity_index as idx


def _metadata(**overrides):
    metadata = {
        "reference_title": "Biệt Đội Chó Bay",
        "reference_author": "Huỳnh Long, Mai Chi",
        "reference_publisher": "Kim Đồng",
        "reference_isbn": "8935244812345",
        "reference_description": "Mô tả.",
        "reference_image_url": "https://cdn1.fahasa.com/media/catalog/product/b/i/biet_doi.jpg",
        "reference_cover_price_vnd": 65000,
        "raw_metadata": {"json_ld_found": True, "current_price_vnd": 52000, "cover_price_vnd": 65000},
    }
    metadata.update(overrides)
    return metadata


def test_listing_links_keep_order_and_drop_navigation_and_duplicates():
    links = [
        ("https://www.fahasa.com/sach-trong-nuoc.html", "Sách Trong Nước"),
        ("https://www.fahasa.com/biet-doi-cho-bay.html?ref=x", "Biệt Đội Chó Bay"),
        ("https://www.fahasa.com/biet-doi-cho-bay.html", "Biệt Đội Chó Bay"),
        ("https://www.fahasa.com/trang-cay-da.html", "Trăng Cây Đa"),
        ("https://www.fahasa.com/customer/account/login", "Đăng nhập"),
        ("https://www.fahasa.com/no-text.html", "  "),
    ]
    assert discovery.product_links_from_listing(links) == [
        "https://www.fahasa.com/biet-doi-cho-bay.html",
        "https://www.fahasa.com/trang-cay-da.html",
    ]


def test_gift_or_category_page_with_json_ld_is_not_a_book():
    assert discovery.is_book_product_page(_metadata())
    assert not discovery.is_book_product_page(_metadata(reference_publisher=None))
    assert not discovery.is_book_product_page(_metadata(raw_metadata={"json_ld_found": False}))
    assert not discovery.is_book_product_page(None)


def test_record_never_carries_a_price_and_never_promotes_a_barcode():
    record = discovery.build_record("https://www.fahasa.com/biet-doi-cho-bay.html", _metadata(), "2026-10-09T00:00:00+00:00")
    flattened = repr(record).lower()
    assert "price" not in flattened
    assert "65000" not in flattened and "52000" not in flattened
    assert record["isbn"] is None
    assert record["isbn_raw_value"] == "8935244812345"


def test_fahasa_image_is_never_auto_rights_approved_for_live_discovery():
    record = discovery.build_record("https://www.fahasa.com/x.html", _metadata(), "2026-10-09T00:00:00+00:00")
    assert record["image"]["rights_status"] == "RIGHTS_UNKNOWN"
    assert record["image"]["source_type"] == "FAHASA"


def test_missing_facebook_export_means_coverage_unknown(tmp_path):
    posts, note = discovery.load_facebook_posts(tmp_path / "missing.csv")
    assert posts is None and note is None
    record = discovery.build_record("https://www.fahasa.com/x.html", _metadata(), "2026-10-09T00:00:00+00:00")
    assert discovery.classify_record(record, [], posts, note)["classification"] == idx.FACEBOOK_COVERAGE_UNKNOWN


def test_facebook_export_coverage_note_uses_latest_post_date(tmp_path):
    path = tmp_path / "fb.csv"
    path.write_text(
        "record_index,date_text,full_text,tsyc_relevance\n"
        "1,\"Tháng 6 12, 2025 8:58:16 sáng\",Sách mới,HIGH\n"
        "2,\"Tháng 8 23, 2026 1:00:00 ch\",Sách khác,MEDIUM\n"
        "3,\"Tháng 1 02, 2024 1:00:00 ch\",,LOW\n",
        encoding="utf-8",
    )
    posts, note = discovery.load_facebook_posts(path)
    assert [post.ref for post in posts] == ["fb-export-1", "fb-export-2"]
    assert "2026-08-23" in note


# --- page cache (discoverer 1.1.0) ---------------------------------------------

from datetime import datetime, timedelta, timezone  # noqa: E402

import pytest  # noqa: E402

_URL = "https://www.fahasa.com/biet-doi-cho-bay.html"
_NOW = datetime(2026, 11, 27, 12, 0, tzinfo=timezone.utc)


class _FakeSource:
    def __init__(self, metadata=None, error=None):
        self.metadata = metadata
        self.error = error
        self.calls = []

    def fetch_metadata(self, url):
        self.calls.append(url)
        if self.error:
            raise self.error
        return self.metadata


def _entry(hours_old, metadata):
    return {"fetched_at": (_NOW - timedelta(hours=hours_old)).isoformat(), "metadata": metadata}


def test_fresh_cache_entry_is_reused_without_contacting_fahasa_or_sleeping():
    source, sleeps = _FakeSource(_metadata()), []
    cache = {_URL: _entry(10, _metadata())}
    metadata, fetched_at, from_cache = discovery.acquire_page(source, _URL, cache, 72, now=_NOW, sleep=sleeps.append)
    assert from_cache and metadata == _metadata()
    assert fetched_at == cache[_URL]["fetched_at"]
    assert source.calls == [] and sleeps == []


@pytest.mark.parametrize("hours_old", [73, -1])
def test_stale_or_future_cache_entry_is_fetched_again(hours_old):
    source, sleeps = _FakeSource(_metadata(reference_title="Mới")), []
    cache = {_URL: _entry(hours_old, _metadata())}
    metadata, _, from_cache = discovery.acquire_page(source, _URL, cache, 72, now=_NOW, sleep=sleeps.append)
    assert not from_cache and metadata["reference_title"] == "Mới"
    assert source.calls == [_URL] and sleeps == [discovery.PAGE_DELAY_SECONDS]
    assert cache[_URL]["metadata"]["reference_title"] == "Mới"


def test_zero_max_age_always_fetches():
    source = _FakeSource(_metadata())
    cache = {_URL: _entry(0, _metadata())}
    _, _, from_cache = discovery.acquire_page(source, _URL, cache, 0, now=_NOW, sleep=lambda _s: None)
    assert not from_cache and source.calls == [_URL]


def test_non_book_page_is_cached_as_none_and_reused():
    cache = {}
    discovery.acquire_page(_FakeSource(None), _URL, cache, 72, sleep=lambda _s: None)
    assert cache[_URL]["metadata"] is None
    source = _FakeSource(_metadata())
    metadata, _, from_cache = discovery.acquire_page(source, _URL, cache, 72, sleep=lambda _s: None)
    assert from_cache and metadata is None and source.calls == []


def test_fetch_error_propagates_and_is_never_cached():
    cache = {}
    with pytest.raises(RuntimeError):
        discovery.acquire_page(_FakeSource(error=RuntimeError("boom")), _URL, cache, 72, sleep=lambda _s: None)
    assert cache == {}


@pytest.mark.parametrize(
    "entry",
    [
        {"metadata": {}},
        {"fetched_at": "not-a-date", "metadata": {}},
        {"fetched_at": "2026-11-27T11:00:00", "metadata": {}},  # naive timestamp
        {"fetched_at": "2026-11-27T11:00:00+00:00"},
        "garbage",
    ],
)
def test_malformed_cache_entry_is_a_miss(entry):
    assert discovery.cached_page({_URL: entry}, _URL, _NOW, 72) == (False, None)


def test_metadata_that_does_not_round_trip_is_not_cached():
    cache = {_URL: _entry(1, _metadata())}
    discovery.remember_page(cache, _URL, _metadata(raw_metadata={"when": datetime(2026, 1, 1)}), "x")
    assert _URL not in cache
    discovery.remember_page(cache, _URL, _metadata(reference_page_count=(1, 2)), "x")  # tuple -> list changes it
    assert _URL not in cache


def test_cached_record_classifies_like_a_live_one(tmp_path):
    path = tmp_path / "cache.json"
    cache = {}
    discovery.remember_page(cache, _URL, _metadata(), _NOW.isoformat())
    discovery.save_page_cache(cache, path)
    restored = discovery.load_page_cache(path)
    hit, metadata = discovery.cached_page(restored, _URL, _NOW, 72)
    assert hit
    live = discovery.build_record(_URL, _metadata(), "2026-10-09T00:00:00+00:00")
    cached = discovery.build_record(_URL, metadata, "2026-10-09T00:00:00+00:00")
    assert live == cached
    assert discovery.classify_record(cached, [], None, None) == discovery.classify_record(live, [], None, None)


def test_unreadable_cache_file_is_empty(tmp_path):
    path = tmp_path / "cache.json"
    assert discovery.load_page_cache(path) == {}
    path.write_text("{not json", encoding="utf-8")
    assert discovery.load_page_cache(path) == {}
    path.write_text("[1, 2]", encoding="utf-8")
    assert discovery.load_page_cache(path) == {}
