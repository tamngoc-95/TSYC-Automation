"""Regression tests for the unified catalogue identity index (offline)."""
from src.domain.rules import catalog_identity_index as idx

ISBN_A = "9786041234567"
ISBN_B = "9786049876543"


def _woo(title, isbn=None, removed=False, author=None, ref="4650"):
    return idx.IndexEntry(
        origin=idx.ORIGIN_WOO, ref=ref, title=title, isbn=isbn, author=author,
        remote_removed=removed, status="trash" if removed else "draft",
    )


def _post(text, relevance="HIGH"):
    return idx.IndexEntry(origin=idx.ORIGIN_FACEBOOK_POST, ref="fb-1", title=text, relevance=relevance)


def _classify(listing, entries=(), posts=()):
    return idx.classify_listing(listing, entries, posts)


def test_no_match_anywhere_is_new_confirmed_with_coverage_note():
    result = idx.classify_listing(
        idx.Listing("Đường Về Nhà Của Pitt", isbn=ISBN_A),
        [_woo("Thói Quen Thứ 8")],
        [_post("Sách mới về: Cân Bằng Cảm Xúc")],
        facebook_coverage_note="Facebook checked through 2026-08-25",
    )
    assert result.classification == idx.NEW_CONFIRMED
    assert result.warnings == ("Facebook checked through 2026-08-25",)


def test_equal_isbn_is_existing_product_even_with_different_title():
    result = _classify(idx.Listing("Pitt Về Nhà", isbn=ISBN_A), [_woo("Đường Về Nhà Của Pitt", isbn=ISBN_A)])
    assert result.classification == idx.EXISTING_PRODUCT
    assert result.linked.ref == "4650"


def test_893_barcode_is_never_compared_as_isbn():
    barcode = "8935235241329"
    result = _classify(idx.Listing("Sách A Hoàn Toàn Khác", isbn=barcode), [_woo("Sách B Rất Khác Nhau", isbn=barcode)])
    assert result.classification == idx.NEW_CONFIRMED


def test_reprint_suffix_is_not_identity():
    result = _classify(idx.Listing("Cậu Bé Mặc Váy (Tái Bản 2026)"), [_woo("Cậu Bé Mặc Váy")])
    assert result.classification == idx.EXISTING_PRODUCT


def test_same_title_different_valid_isbn_is_distinct_edition():
    result = _classify(idx.Listing("Hoàng Tử Bé", isbn=ISBN_A), [_woo("Hoàng Tử Bé", isbn=ISBN_B)])
    assert result.classification == idx.DISTINCT_EDITION


def test_hardcover_vs_softcover_marker_on_one_side_is_possible_duplicate():
    result = _classify(idx.Listing("Khám Lớn Sài Gòn - Bìa Cứng"), [_woo("Khám Lớn Sài Gòn")])
    assert result.classification == idx.POSSIBLE_DUPLICATE


def test_different_volume_is_never_a_certain_match():
    result = _classify(
        idx.Listing("Nhật Ký Chú Bé Nhút Nhát - Tập 21 - Chiến Hay Chuồn"),
        [_woo("Nhật Ký Chú Bé Nhút Nhát - Tập 20 - Chiến Hay Chuồn")],
    )
    # Not the same book, but the series is already sold: sellable-unit
    # question, isolated rather than silently NEW.
    assert result.classification == idx.POSSIBLE_DUPLICATE


def test_different_volume_with_short_series_name_is_new():
    result = _classify(idx.Listing("Doraemon - Tập 6"), [_woo("Doraemon - Tập 5")])
    assert result.classification == idx.NEW_CONFIRMED


def test_new_volume_of_series_sold_as_set_is_possible_duplicate():
    # 2026-10-09 pilot: Woo sells "Nhật Ký Chú Bé Nhút Nhát (Song ngữ Anh – Việt)".
    result = _classify(
        idx.Listing("Diary Of A Wimpy Kid - Nhật Ký Chú Bé Nhút Nhát - Tập 21 - Chiến Hay Chuồn - Song Ngữ Việt Anh"),
        [_woo("Nhật Ký Chú Bé Nhút Nhát (Song ngữ Anh – Việt)")],
    )
    assert result.classification == idx.POSSIBLE_DUPLICATE
    assert "nhat ky chu be nhut nhat" in result.reason


def test_new_volume_of_series_with_numbered_set_is_possible_duplicate():
    result = _classify(
        idx.Listing("Ehon Kĩ Năng Sống - Miu Miu Tự Lập - Tập 20 - Thay Răng Sữa (Tái Bản 2026)"),
        [_woo("Miu Miu Tự Lập 1")],
    )
    assert result.classification == idx.POSSIBLE_DUPLICATE


def test_series_keys():
    assert idx.series_keys("Ehon Kĩ Năng Sống - Miu Miu Tự Lập - Tập 20 - Thay Răng Sữa (Tái Bản 2026)") == (
        "ehon ki nang song",
        "miu miu tu lap",
    )
    assert idx.series_keys("Tủ Sách Vàng Cho Con - Hoàng Tử Bé") == ("tu sach vang cho con",)
    assert idx.series_keys("Biệt Đội Chó Bay") == ()


def test_owner_removed_product_is_remote_removed_never_new():
    result = _classify(idx.Listing("Thói Quen Thứ 8"), [_woo("Thói Quen Thứ 8 (Tái Bản)", removed=True)])
    assert result.classification == idx.REMOTE_REMOVED


def test_live_woo_match_wins_over_removed_match():
    result = _classify(
        idx.Listing("Thói Quen Thứ 8"),
        [_woo("Thói Quen Thứ 8", removed=True, ref="1"), _woo("Thói Quen Thứ 8", ref="2")],
    )
    assert result.classification == idx.EXISTING_PRODUCT
    assert result.linked.ref == "2"


def test_same_title_different_author_is_possible_duplicate():
    result = _classify(
        idx.Listing("Tư Duy Ngược", author="Jonah Sachs"),
        [_woo("Tư Duy Ngược", author="Nguyễn Anh Dũng")],
    )
    assert result.classification == idx.POSSIBLE_DUPLICATE


def test_combo_vs_single_volume_is_possible_duplicate():
    result = _classify(idx.Listing("Combo Hoàng Tử Bé Và Cáo"), [_woo("Hoàng Tử Bé Và Cáo")])
    assert result.classification == idx.POSSIBLE_DUPLICATE


def test_containing_title_is_possible_duplicate():
    result = _classify(
        idx.Listing("Ehon Kĩ Năng Sống - Miu Bé Nhỏ - Học Phòng Tránh Xâm Hại"),
        [_woo("Học Phòng Tránh Xâm Hại")],
    )
    assert result.classification == idx.POSSIBLE_DUPLICATE


def test_long_title_phrase_in_book_relevant_facebook_post_is_existing():
    result = _classify(
        idx.Listing("Địa Ngục Du Ký Giải Mã Địa Ngục"),
        posts=[_post("Em mới về cuốn Địa Ngục Du Ký - Giải Mã Địa Ngục, ai cần inbox nhé")],
    )
    assert result.classification == idx.EXISTING_PRODUCT
    assert result.linked.origin == idx.ORIGIN_FACEBOOK_POST


def test_short_title_facebook_mention_is_only_possible():
    result = _classify(idx.Listing("Nàng Gù"), posts=[_post("Nàng gù nhà em hôm nay đi học")])
    assert result.classification == idx.POSSIBLE_DUPLICATE


def test_long_title_in_low_relevance_post_is_only_possible():
    result = _classify(
        idx.Listing("Bản Ngã Càng Lớn Khổ Đau Càng Nhiều"),
        posts=[_post("bản ngã càng lớn khổ đau càng nhiều, nghĩ mà xem", relevance="LOW")],
    )
    assert result.classification == idx.POSSIBLE_DUPLICATE


def test_isbn_in_facebook_post_is_existing():
    result = _classify(idx.Listing("Bất Kỳ", isbn=ISBN_A), posts=[_post(f"ISBN: 978-604-123-456-7")])
    assert result.classification == idx.EXISTING_PRODUCT


def test_unavailable_facebook_data_is_coverage_unknown_not_new():
    result = idx.classify_listing(idx.Listing("Đường Về Nhà Của Pitt"), [], None)
    assert result.classification == idx.FACEBOOK_COVERAGE_UNKNOWN


def test_blank_title_is_never_new_confirmed():
    assert _classify(idx.Listing("  ")).classification == idx.POSSIBLE_DUPLICATE


def test_identity_strength_and_sellable_unit():
    assert idx.listing_identity_strength(idx.Listing("A B", isbn=ISBN_A)) == idx.IDENTITY_STRONG
    assert idx.listing_identity_strength(idx.Listing("A B", author="Roald Dahl", publisher="NXB Kim Đồng")) == idx.IDENTITY_MODERATE
    assert idx.listing_identity_strength(idx.Listing("A B", isbn="8935235241329")) == idx.IDENTITY_WEAK
    assert idx.listing_sellable_unit(idx.Listing("Bộ Sách Câu Chuyện Nhỏ Bài Học Lớn (Túi 4 Cuốn)")) == "BOOK_SET"
    assert idx.listing_sellable_unit(idx.Listing("Trăng Cây Đa")) == "SINGLE_BOOK"
