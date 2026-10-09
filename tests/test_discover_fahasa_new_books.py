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
