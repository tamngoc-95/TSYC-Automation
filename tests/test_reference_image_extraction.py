"""Regression tests for collect_reference_metadata.extract_image_url().

Covers the minhkhai.com.vn domain-scoped fallback added because that site
exposes no Product JSON-LD, no og:image meta tag, and no class/id/itemprop
hookable markup around its cover image -- the fallback derives the
full-size cover URL from the "isbn" query parameter already present on the
registered product page URL, and must never fire for any other domain or
until every generic extraction tier above it has failed.

Pure/offline: no live browser, no network calls, no live Supabase. Page
interactions are simulated with a minimal fake Playwright Page.
"""

from __future__ import annotations

import collect_reference_metadata as reference_metadata


class FakeLocator:
    """Minimal stand-in for a Playwright Locator.

    `attributes` is None when the selector matches nothing (count() == 0,
    mirroring a real Playwright locator against absent markup); otherwise
    it is a dict of attribute name -> value (e.g. {"content": "..."} for a
    meta tag, or {"src": "..."} for an <img>).
    """

    def __init__(self, attributes: dict[str, str] | None):
        self._attributes = attributes

    @property
    def first(self):
        return self

    def count(self) -> int:
        return 1 if self._attributes else 0

    def get_attribute(self, name: str):
        if not self._attributes:
            return None
        return self._attributes.get(name)


class FakePage:
    """Minimal stand-in for a Playwright Page.

    `selector_map` maps a CSS selector string to the attributes a matching
    element would expose. Any selector not present in the map behaves as
    "no element found", matching real markup that lacks it.
    """

    def __init__(self, url: str, selector_map: dict[str, dict[str, str]] | None = None):
        self.url = url
        self._selector_map = selector_map or {}

    def locator(self, selector: str) -> FakeLocator:
        return FakeLocator(self._selector_map.get(selector))


MINHKHAI_URL = "https://minhkhai.com.vn/store2/index.aspx?isbn=8935210222336&q=view"


# ---------------------------------------------------------------------------
# 1. Valid Minh Khai source URL -> expected /hinhlon/{identifier}.jpg
# ---------------------------------------------------------------------------


def test_minhkhai_fallback_derives_expected_image_url():
    page = FakePage(url=MINHKHAI_URL)

    result = reference_metadata.extract_minhkhai_image_url(page)

    assert result == "https://minhkhai.com.vn/hinhlon/8935210222336.jpg"


def test_extract_image_url_uses_minhkhai_fallback_when_nothing_else_matches():
    page = FakePage(url=MINHKHAI_URL, selector_map={})

    result = reference_metadata.extract_image_url(
        page=page,
        product_json_ld=None,
    )

    assert result == "https://minhkhai.com.vn/hinhlon/8935210222336.jpg"


# ---------------------------------------------------------------------------
# 2. Malformed identifier -> no fallback / fail safely
# ---------------------------------------------------------------------------


def test_minhkhai_fallback_rejects_non_numeric_identifier():
    page = FakePage(
        url="https://minhkhai.com.vn/store2/index.aspx?isbn=abc1234567890&q=view"
    )

    assert reference_metadata.extract_minhkhai_image_url(page) is None


def test_minhkhai_fallback_rejects_wrong_length_identifier():
    page = FakePage(
        url="https://minhkhai.com.vn/store2/index.aspx?isbn=12345&q=view"
    )

    assert reference_metadata.extract_minhkhai_image_url(page) is None


# ---------------------------------------------------------------------------
# 3. Missing identifier -> no fallback
# ---------------------------------------------------------------------------


def test_minhkhai_fallback_returns_none_when_isbn_param_absent():
    page = FakePage(url="https://minhkhai.com.vn/store2/index.aspx?q=view")

    assert reference_metadata.extract_minhkhai_image_url(page) is None


def test_extract_image_url_returns_none_when_all_tiers_fail_on_minhkhai():
    page = FakePage(url="https://minhkhai.com.vn/store2/index.aspx?q=view")

    result = reference_metadata.extract_image_url(
        page=page,
        product_json_ld=None,
    )

    assert result is None


# ---------------------------------------------------------------------------
# 4. Non-Minh-Khai hostname -> fallback never activates
# ---------------------------------------------------------------------------


def test_minhkhai_fallback_never_activates_for_other_hostnames():
    page = FakePage(
        url="https://example.com/store2/index.aspx?isbn=8935210222336&q=view"
    )

    assert reference_metadata.extract_minhkhai_image_url(page) is None


def test_minhkhai_fallback_never_activates_for_lookalike_subdomain():
    page = FakePage(
        url="https://evil.minhkhai.com.vn/store2/index.aspx?isbn=8935210222336&q=view"
    )

    assert reference_metadata.extract_minhkhai_image_url(page) is None


def test_minhkhai_fallback_uses_first_isbn_when_param_repeated():
    page = FakePage(
        url="https://minhkhai.com.vn/store2/index.aspx?isbn=8935210222336&isbn=9999999999999"
    )

    result = reference_metadata.extract_minhkhai_image_url(page)

    assert result == "https://minhkhai.com.vn/hinhlon/8935210222336.jpg"


def test_minhkhai_fallback_handles_url_encoded_isbn():
    page = FakePage(
        url="https://minhkhai.com.vn/store2/index.aspx?isbn=893521022%32336&q=view"
    )

    result = reference_metadata.extract_minhkhai_image_url(page)

    assert result == "https://minhkhai.com.vn/hinhlon/8935210222336.jpg"


# ---------------------------------------------------------------------------
# 5. Existing JSON-LD extraction still wins
# ---------------------------------------------------------------------------


def test_json_ld_image_wins_over_minhkhai_fallback():
    page = FakePage(url=MINHKHAI_URL, selector_map={})

    result = reference_metadata.extract_image_url(
        page=page,
        product_json_ld={"image": "https://jsonld.example.com/cover.jpg"},
    )

    assert result == "https://jsonld.example.com/cover.jpg"


# ---------------------------------------------------------------------------
# 6. Existing og:image extraction still wins
# ---------------------------------------------------------------------------


def test_og_image_wins_over_minhkhai_fallback():
    page = FakePage(
        url=MINHKHAI_URL,
        selector_map={
            'meta[property="og:image"]': {
                "content": "https://og.example.com/cover.jpg",
            },
        },
    )

    result = reference_metadata.extract_image_url(
        page=page,
        product_json_ld=None,
    )

    assert result == "https://og.example.com/cover.jpg"


# ---------------------------------------------------------------------------
# 7. Generic CSS extraction still wins before domain fallback
# ---------------------------------------------------------------------------


def test_generic_css_fallback_wins_over_minhkhai_fallback():
    page = FakePage(
        url=MINHKHAI_URL,
        selector_map={
            ".product-image img": {
                "src": "https://cdn.example.com/generic-cover.jpg",
            },
        },
    )

    result = reference_metadata.extract_image_url(
        page=page,
        product_json_ld=None,
    )

    assert result == "https://cdn.example.com/generic-cover.jpg"


# ---------------------------------------------------------------------------
# 8. Vietnamese/product title content is unaffected
# ---------------------------------------------------------------------------


def test_vietnamese_title_extraction_unaffected_by_image_fallback_change():
    page = FakePage(url=MINHKHAI_URL, selector_map={})

    title = reference_metadata.extract_title(
        page=page,
        product_json_ld={
            "name": "Giáo Dục Giới Tính - Không Phải Lỗi Của Con",
        },
    )

    assert title == "Giáo Dục Giới Tính - Không Phải Lỗi Của Con"
