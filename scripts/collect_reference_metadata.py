import hashlib
import json
import re
import sys
import unicodedata
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from dotenv import load_dotenv
from playwright.sync_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.repositories.supabase_repository import SupabaseRepository


COLLECTOR_NAME = "reference_metadata_collector"
COLLECTOR_VERSION = "1.4.0"

NAVIGATION_TIMEOUT_MS = 60_000

SUPPORTED_SOURCE_TYPES = {
    "PUBLISHER",
    "AUTHORIZED_SUPPLIER",
    "BOOKSTORE",
    "FAHASA",
}

PAGE_TYPE_BY_SOURCE_TYPE = {
    "PUBLISHER": "PUBLISHER_PAGE",
    "AUTHORIZED_SUPPLIER": "SUPPLIER_PAGE",
    "BOOKSTORE": "BOOKSTORE_PAGE",
    "FAHASA": "FAHASA_PAGE",
}

SOURCE_PRIORITY_BY_TYPE = {
    "PUBLISHER": 1,
    "AUTHORIZED_SUPPLIER": 2,
    "BOOKSTORE": 3,
    "FAHASA": 4,
    "OTHER": 5,
}


def utc_now_iso() -> str:
    """Return the current UTC timestamp in ISO format."""
    return datetime.now(
        timezone.utc
    ).isoformat()


def normalize_whitespace(
    value: str | None,
) -> str | None:
    """Normalize whitespace and return None for empty values."""
    if value is None:
        return None

    normalized = " ".join(
        value.split()
    ).strip()

    return normalized or None


def normalize_search_text(
    value: str | None,
) -> str:
    """Normalize text for comparisons and parser matching."""
    if not value:
        return ""

    normalized = unicodedata.normalize(
        "NFD",
        value,
    )

    without_accents = "".join(
        character
        for character in normalized
        if unicodedata.category(character) != "Mn"
    )

    without_accents = without_accents.lower()

    without_symbols = re.sub(
        r"[^a-z0-9]+",
        " ",
        without_accents,
    )

    return " ".join(
        without_symbols.split()
    )


def parse_integer(
    value: str | None,
) -> int | None:
    """Extract an integer from a text value."""
    if not value:
        return None

    digits = re.sub(
        r"[^\d]",
        "",
        value,
    )

    if not digits:
        return None

    return int(digits)


def parse_vnd_price(
    value: str | None,
) -> Decimal | None:
    """Parse one Vietnamese đồng price without joining discount numbers."""
    if not value:
        return None

    normalized_value = normalize_whitespace(value)

    if not normalized_value:
        return None

    currency_match = re.search(
        r"(?<!\d)"
        r"(?P<price>"
        r"\d{1,3}(?:[.\s,]\d{3})+"
        r"|"
        r"\d{4,9}"
        r")"
        r"(?:[.,]\d{1,2})?"
        r"\s*(?:₫|đ|VND)",
        normalized_value,
        flags=re.IGNORECASE,
    )

    if currency_match:
        digits = re.sub(
            r"\D",
            "",
            currency_match.group("price"),
        )

        if digits:
            try:
                parsed_price = Decimal(digits)
            except InvalidOperation:
                parsed_price = None

            if (
                parsed_price is not None
                and Decimal("1000")
                <= parsed_price
                <= Decimal("2000000")
            ):
                return parsed_price

    isolated_match = re.fullmatch(
        r"\s*(?P<price>\d{4,9})(?:[.,]\d{1,2})?\s*",
        normalized_value,
    )

    if isolated_match:
        try:
            parsed_price = Decimal(
                isolated_match.group("price")
            )
        except InvalidOperation:
            parsed_price = None

        if (
            parsed_price is not None
            and Decimal("1000")
            <= parsed_price
            <= Decimal("2000000")
        ):
            return parsed_price

    grouped_match = re.search(
        r"(?<!\d)"
        r"(?P<price>\d{1,3}(?:[.\s,]\d{3})+)"
        r"(?![\d.,])",
        normalized_value,
    )

    if grouped_match:
        digits = re.sub(
            r"\D",
            "",
            grouped_match.group("price"),
        )

        if digits:
            try:
                parsed_price = Decimal(digits)
            except InvalidOperation:
                parsed_price = None

            if (
                parsed_price is not None
                and Decimal("1000")
                <= parsed_price
                <= Decimal("2000000")
            ):
                return parsed_price

    return None


def parse_decimal_number(
    value: str | None,
) -> Decimal | None:
    """Parse one decimal number from text."""
    if not value:
        return None

    match = re.search(
        r"\d+(?:[.,]\d+)?",
        value,
    )

    if not match:
        return None

    normalized = match.group(0).replace(
        ",",
        ".",
    )

    try:
        return Decimal(normalized)

    except InvalidOperation:
        return None


def decimal_to_json_value(
    value: Decimal | None,
) -> float | None:
    """Convert Decimal to a JSON-compatible numeric value."""
    if value is None:
        return None

    return float(value)


def calculate_content_hash(
    raw_html: str,
) -> str:
    """Calculate a stable SHA-256 hash for downloaded HTML."""
    normalized_html = raw_html.strip().encode(
        "utf-8"
    )

    return hashlib.sha256(
        normalized_html
    ).hexdigest()


def get_meta_content(
    page: Page,
    selector: str,
) -> str | None:
    """Read a meta-tag content attribute safely."""
    locator = page.locator(
        selector
    ).first

    if locator.count() == 0:
        return None

    return normalize_whitespace(
        locator.get_attribute("content")
    )


def get_first_attribute(
    page: Page,
    selectors: list[str],
    attribute_name: str,
) -> str | None:
    """Return the first non-empty attribute from candidate selectors."""
    for selector in selectors:
        locator = page.locator(
            selector
        ).first

        if locator.count() == 0:
            continue

        value = normalize_whitespace(
            locator.get_attribute(
                attribute_name
            )
        )

        if value:
            return value

    return None


def get_first_text(
    page: Page,
    selectors: list[str],
) -> str | None:
    """Return the first non-empty visible text from candidate selectors."""
    for selector in selectors:
        locator = page.locator(
            selector
        ).first

        if locator.count() == 0:
            continue

        try:
            value = normalize_whitespace(
                locator.inner_text(
                    timeout=3_000
                )
            )

        except PlaywrightTimeoutError:
            continue

        if value:
            return value

    return None


def extract_json_ld(
    page: Page,
) -> list[Any]:
    """Extract valid JSON-LD objects from the page."""
    objects: list[Any] = []

    scripts = page.locator(
        'script[type="application/ld+json"]'
    )

    for index in range(
        scripts.count()
    ):
        script_text = scripts.nth(
            index
        ).text_content()

        if not script_text:
            continue

        try:
            parsed = json.loads(
                script_text
            )

        except json.JSONDecodeError:
            continue

        if isinstance(parsed, list):
            objects.extend(
                parsed
            )

        else:
            objects.append(
                parsed
            )

    return objects


def iter_json_ld_objects(
    value: Any,
):
    """Yield nested dictionaries from JSON-LD structures."""
    if isinstance(value, dict):
        yield value

        for child_value in value.values():
            yield from iter_json_ld_objects(
                child_value
            )

    elif isinstance(value, list):
        for child_value in value:
            yield from iter_json_ld_objects(
                child_value
            )


def find_product_json_ld(
    json_ld_objects: list[Any],
) -> dict[str, Any] | None:
    """Find the first Product JSON-LD object."""
    for root_object in json_ld_objects:
        for candidate_object in iter_json_ld_objects(
            root_object
        ):
            object_type = candidate_object.get(
                "@type"
            )

            if object_type == "Product":
                return candidate_object

            if (
                isinstance(
                    object_type,
                    list,
                )
                and "Product" in object_type
            ):
                return candidate_object

    return None


def extract_label_value(
    body_text: str,
    labels: list[str],
) -> str | None:
    """
    Extract a short value appearing after one of the supplied labels.

    This supports text such as:
    'Tác giả | Eran Katz'
    or:
    'Tác giả: Eran Katz'
    """
    for label in labels:
        escaped_label = re.escape(
            label
        )

        pattern = (
            rf"(?:^|\n)\s*{escaped_label}"
            rf"\s*(?:\||:)?\s*"
            rf"([^\n|]+)"
        )

        match = re.search(
            pattern,
            body_text,
            flags=re.IGNORECASE,
        )

        if not match:
            continue

        value = normalize_whitespace(
            match.group(1)
        )

        if value:
            return value

    return None


def extract_dimensions(
    body_text: str,
) -> tuple[
    Decimal | None,
    Decimal | None,
    Decimal | None,
    str | None,
]:
    """Extract length, width, and optional height in centimetres."""
    raw_dimensions = extract_label_value(
        body_text=body_text,
        labels=[
            "Kích Thước",
            "Kích thước",
            "Dimensions",
        ],
    )

    if not raw_dimensions:
        return (
            None,
            None,
            None,
            None,
        )

    numbers = re.findall(
        r"\d+(?:[.,]\d+)?",
        raw_dimensions,
    )

    parsed_numbers: list[Decimal] = []

    for number in numbers:
        parsed_value = parse_decimal_number(
            number
        )

        if parsed_value is not None:
            parsed_numbers.append(
                parsed_value
            )

    length_cm = (
        parsed_numbers[0]
        if len(parsed_numbers) >= 1
        else None
    )

    width_cm = (
        parsed_numbers[1]
        if len(parsed_numbers) >= 2
        else None
    )

    height_cm = (
        parsed_numbers[2]
        if len(parsed_numbers) >= 3
        else None
    )

    return (
        length_cm,
        width_cm,
        height_cm,
        raw_dimensions,
    )


def extract_price_values(
    page: Page,
    body_text: str,
    product_json_ld: dict[str, Any] | None,
) -> tuple[
    Decimal | None,
    Decimal | None,
]:
    """
    Return current price and cover/list price.

    For Alpha Books, the struck-through old price is treated as
    the cover price when it exists.
    """
    current_price: Decimal | None = None
    cover_price: Decimal | None = None

    if product_json_ld:
        offers = product_json_ld.get(
            "offers"
        )

        if isinstance(
            offers,
            list,
        ):
            offers = (
                offers[0]
                if offers
                else None
            )

        if isinstance(
            offers,
            dict,
        ):
            current_price = parse_vnd_price(
                str(
                    offers.get("price")
                    or ""
                )
            )

    if current_price is None:
        current_price_text = get_first_text(
            page=page,
            selectors=[
                ".product-price",
                ".special-price",
                ".price-box .price",
                '[itemprop="price"]',
            ],
        )

        current_price = parse_vnd_price(
            current_price_text
        )

    old_price_text = get_first_text(
        page=page,
        selectors=[
            ".old-price",
            ".compare-price",
            ".price-box del",
            "del",
            "s",
        ],
    )

    cover_price = parse_vnd_price(
        old_price_text
    )

    if cover_price is None:
        old_price_match = re.search(
            r"Giá\s*cũ\s*:?\s*"
            r"(?:~~\s*)?"
            r"([\d.]+)\s*₫",
            body_text,
            flags=re.IGNORECASE,
        )

        if old_price_match:
            cover_price = parse_vnd_price(
                old_price_match.group(1)
            )

    if current_price is None:
        current_price_match = re.search(
            r"(?:^|\n)\s*"
            r"([\d.]+)\s*₫",
            body_text,
        )

        if current_price_match:
            current_price = parse_vnd_price(
                current_price_match.group(1)
            )

    if cover_price is None:
        cover_price = current_price

    return (
        current_price,
        cover_price,
    )


def extract_description(
    page: Page,
    product_json_ld: dict[str, Any] | None,
) -> str | None:
    """Extract the primary product description."""
    if product_json_ld:
        description = normalize_whitespace(
            product_json_ld.get(
                "description"
            )
        )

        if description:
            return description

    meta_description = get_meta_content(
        page,
        'meta[name="description"]',
    )

    if meta_description:
        return meta_description

    return get_first_text(
        page=page,
        selectors=[
            ".product-description",
            ".product_getcontent",
            ".rte",
            '[itemprop="description"]',
        ],
    )


def extract_image_url(
    page: Page,
    product_json_ld: dict[str, Any] | None,
) -> str | None:
    """Extract the primary product image URL."""
    if product_json_ld:
        image_value = product_json_ld.get(
            "image"
        )

        if isinstance(
            image_value,
            list,
        ):
            image_value = (
                image_value[0]
                if image_value
                else None
            )

        if isinstance(
            image_value,
            dict,
        ):
            image_value = (
                image_value.get("url")
                or image_value.get(
                    "contentUrl"
                )
            )

        image_url = normalize_whitespace(
            str(image_value)
            if image_value
            else None
        )

        if image_url:
            return image_url

    open_graph_image = get_meta_content(
        page,
        'meta[property="og:image"]',
    )

    if open_graph_image:
        return open_graph_image

    return get_first_attribute(
        page=page,
        selectors=[
            ".product-image img",
            ".product-detail-left img",
            '[itemprop="image"]',
        ],
        attribute_name="src",
    )


def extract_title(
    page: Page,
    product_json_ld: dict[str, Any] | None,
) -> str | None:
    """Extract the reference title."""
    if product_json_ld:
        title = normalize_whitespace(
            product_json_ld.get(
                "name"
            )
        )

        if title:
            return title

    open_graph_title = get_meta_content(
        page,
        'meta[property="og:title"]',
    )

    if open_graph_title:
        return open_graph_title

    return get_first_text(
        page=page,
        selectors=[
            "h1",
            '[itemprop="name"]',
            ".product-title",
        ],
    )


def extract_author(
    body_text: str,
    product_json_ld: dict[str, Any] | None,
) -> str | None:
    """Extract the book author."""
    if product_json_ld:
        author_value = product_json_ld.get(
            "author"
        )

        if isinstance(
            author_value,
            dict,
        ):
            author_name = normalize_whitespace(
                author_value.get(
                    "name"
                )
            )

            if author_name:
                return author_name

        if isinstance(
            author_value,
            str,
        ):
            author_name = normalize_whitespace(
                author_value
            )

            if author_name:
                return author_name

    return extract_label_value(
        body_text=body_text,
        labels=[
            "Tác giả",
            "Tác Giả",
            "Author",
        ],
    )


def extract_isbn(
    body_text: str,
    product_json_ld: dict[str, Any] | None,
) -> str | None:
    """Extract and validate a possible ISBN."""
    possible_values: list[str] = []

    if product_json_ld:
        for key in (
            "isbn",
            "sku",
            "gtin13",
            "gtin",
        ):
            value = product_json_ld.get(
                key
            )

            if value:
                possible_values.append(
                    str(value)
                )

    label_value = extract_label_value(
        body_text=body_text,
        labels=[
            "ISBN",
            "Mã ISBN",
        ],
    )

    if label_value:
        possible_values.append(
            label_value
        )

    for possible_value in possible_values:
        digits = re.sub(
            r"[^\dXx]",
            "",
            possible_value,
        ).upper()

        if len(digits) in {
            10,
            13,
        }:
            return digits

    return None


def extract_publisher(
    body_text: str,
) -> str | None:
    """Extract the stated publishing house."""
    return extract_label_value(
        body_text=body_text,
        labels=[
            "NXB",
            "Nhà xuất bản",
            "Nhà Xuất Bản",
            "Publisher",
        ],
    )


def extract_page_count(
    body_text: str,
) -> int | None:
    """Extract page count."""
    raw_page_count = extract_label_value(
        body_text=body_text,
        labels=[
            "Số trang",
            "Số Trang",
            "Page count",
            "Pages",
        ],
    )

    return parse_integer(
        raw_page_count
    )


def extract_publication_year(
    body_text: str,
) -> int | None:
    """Extract publication year for raw metadata."""
    raw_year = extract_label_value(
        body_text=body_text,
        labels=[
            "Năm XB",
            "Năm xuất bản",
            "Publication year",
        ],
    )

    parsed_year = parse_integer(
        raw_year
    )

    if (
        parsed_year is not None
        and 1800 <= parsed_year <= 2200
    ):
        return parsed_year

    return None


def extract_translator(
    body_text: str,
) -> str | None:
    """Extract translator name for raw metadata."""
    return extract_label_value(
        body_text=body_text,
        labels=[
            "Người Dịch",
            "Người dịch",
            "Translator",
        ],
    )


def extract_book_format(
    body_text: str,
) -> str | None:
    """Extract book binding or format."""
    return extract_label_value(
        body_text=body_text,
        labels=[
            "Hình thức",
            "Định dạng",
            "Format",
        ],
    )


def extract_supplier_name(
    body_text: str,
) -> str | None:
    """Extract supplier name displayed by the product page."""
    return extract_label_value(
        body_text=body_text,
        labels=[
            "Tên Nhà Cung Cấp",
            "Nhà cung cấp",
            "Supplier",
        ],
    )


def extract_weight_grams(
    body_text: str,
) -> int | None:
    """Extract product weight and normalize it to grams."""
    raw_weight = extract_label_value(
        body_text=body_text,
        labels=[
            "Trọng lượng",
            "Trọng Lượng",
            "Weight",
        ],
    )

    if not raw_weight:
        return None

    value = parse_decimal_number(raw_weight)

    if value is None:
        return None

    normalized = raw_weight.lower()

    if "kg" in normalized:
        return int(value * Decimal("1000"))

    return int(value)


def parse_fahasa_metadata(
    page: Page,
    body_text: str,
) -> dict[str, Any]:
    """Parse one Fahasa product page."""
    json_ld_objects = extract_json_ld(page)
    product_json_ld = find_product_json_ld(json_ld_objects)

    title = extract_title(
        page=page,
        product_json_ld=product_json_ld,
    )
    author = extract_author(
        body_text=body_text,
        product_json_ld=product_json_ld,
    )
    isbn = extract_isbn(
        body_text=body_text,
        product_json_ld=product_json_ld,
    )
    publisher = extract_publisher(body_text)
    page_count = extract_page_count(body_text)
    publication_year = extract_publication_year(body_text)
    translator = extract_translator(body_text)
    book_format = extract_book_format(body_text)
    displayed_supplier = extract_supplier_name(body_text)
    weight_grams = extract_weight_grams(body_text)

    (
        length_cm,
        width_cm,
        height_cm,
        raw_dimensions,
    ) = extract_dimensions(body_text)

    (
        current_price_vnd,
        cover_price_vnd,
    ) = extract_price_values(
        page=page,
        body_text=body_text,
        product_json_ld=product_json_ld,
    )

    description = extract_description(
        page=page,
        product_json_ld=product_json_ld,
    )
    image_url = extract_image_url(
        page=page,
        product_json_ld=product_json_ld,
    )

    raw_metadata = {
        "parser_name": "fahasa",
        "parser_version": "1.0.0",
        "publication_year": publication_year,
        "translator": translator,
        "book_format": book_format,
        "displayed_supplier": displayed_supplier,
        "raw_dimensions": raw_dimensions,
        "current_price_vnd": decimal_to_json_value(current_price_vnd),
        "cover_price_vnd": decimal_to_json_value(cover_price_vnd),
        "json_ld_found": product_json_ld is not None,
        "purchase_price_eligible": False,
        "source_usage_note": (
            "Fahasa is used only as a metadata, image, dimensions, "
            "weight, and cover-price reference source."
        ),
    }

    return {
        "reference_title": title,
        "reference_isbn": isbn,
        "reference_author": author,
        "reference_publisher": publisher,
        "reference_page_count": page_count,
        "reference_weight_grams": weight_grams,
        "reference_length_cm": decimal_to_json_value(length_cm),
        "reference_width_cm": decimal_to_json_value(width_cm),
        "reference_height_cm": decimal_to_json_value(height_cm),
        "reference_cover_price_vnd": decimal_to_json_value(cover_price_vnd),
        "reference_description": description,
        "reference_image_url": image_url,
        "raw_metadata": raw_metadata,
    }



def parse_generic_book_metadata(
    page: Page,
    body_text: str,
    parser_label: str,
) -> dict[str, Any]:
    """Parse a public Vietnamese book product page using generic metadata rules."""
    json_ld_objects = extract_json_ld(page)
    product_json_ld = find_product_json_ld(json_ld_objects)

    title = extract_title(
        page=page,
        product_json_ld=product_json_ld,
    )
    author = extract_author(
        body_text=body_text,
        product_json_ld=product_json_ld,
    )
    isbn = extract_isbn(
        body_text=body_text,
        product_json_ld=product_json_ld,
    )
    publisher = extract_publisher(body_text)
    page_count = extract_page_count(body_text)
    publication_year = extract_publication_year(body_text)
    translator = extract_translator(body_text)
    book_format = extract_book_format(body_text)
    displayed_supplier = extract_supplier_name(body_text)
    weight_grams = extract_weight_grams(body_text)

    (
        length_cm,
        width_cm,
        height_cm,
        raw_dimensions,
    ) = extract_dimensions(body_text)

    (
        current_price_vnd,
        cover_price_vnd,
    ) = extract_price_values(
        page=page,
        body_text=body_text,
        product_json_ld=product_json_ld,
    )

    description = extract_description(
        page=page,
        product_json_ld=product_json_ld,
    )
    image_url = extract_image_url(
        page=page,
        product_json_ld=product_json_ld,
    )

    raw_metadata = {
        "parser_name": parser_label,
        "parser_version": "1.0.0",
        "publication_year": publication_year,
        "translator": translator,
        "book_format": book_format,
        "displayed_supplier": displayed_supplier,
        "raw_dimensions": raw_dimensions,
        "current_price_vnd": decimal_to_json_value(current_price_vnd),
        "cover_price_vnd": decimal_to_json_value(cover_price_vnd),
        "json_ld_found": product_json_ld is not None,
        "purchase_price_eligible": False,
        "source_usage_note": (
            "This public website is used only for book identity, metadata, "
            "images, dimensions, weight, and cover-price reference."
        ),
    }

    return {
        "reference_title": title,
        "reference_isbn": isbn,
        "reference_author": author,
        "reference_publisher": publisher,
        "reference_page_count": page_count,
        "reference_weight_grams": weight_grams,
        "reference_length_cm": decimal_to_json_value(length_cm),
        "reference_width_cm": decimal_to_json_value(width_cm),
        "reference_height_cm": decimal_to_json_value(height_cm),
        "reference_cover_price_vnd": decimal_to_json_value(cover_price_vnd),
        "reference_description": description,
        "reference_image_url": image_url,
        "raw_metadata": raw_metadata,
    }

def validate_reference_metadata(
    metadata: dict[str, Any],
    parser_name: str,
) -> list[str]:
    """Validate parsed metadata and remove implausible numeric values."""
    warnings: list[str] = []

    required_fields = (
        ("reference_title", "Reference title was not found."),
        ("reference_author", "Reference author was not found."),
        ("reference_publisher", "Reference publisher was not found."),
        ("reference_isbn", "Reference ISBN was not found."),
    )

    for field_name, warning in required_fields:
        if not metadata.get(field_name):
            warnings.append(warning)

    weight_grams = metadata.get("reference_weight_grams")

    if weight_grams is None:
        warnings.append("Reference weight was not found.")
    elif not 20 <= int(weight_grams) <= 10_000:
        warnings.append(
            "Reference weight was rejected as implausible: "
            f"{weight_grams} grams."
        )
        metadata["reference_weight_grams"] = None
        metadata.setdefault("raw_metadata", {})[
            "rejected_weight_grams"
        ] = weight_grams

    cover_price_vnd = metadata.get("reference_cover_price_vnd")

    if cover_price_vnd is not None:
        try:
            numeric_cover_price = Decimal(str(cover_price_vnd))
        except InvalidOperation:
            numeric_cover_price = None

        if (
            numeric_cover_price is None
            or numeric_cover_price < Decimal("5000")
            or numeric_cover_price > Decimal("2000000")
        ):
            warnings.append(
                "Reference cover price was rejected as implausible: "
                f"{cover_price_vnd} VND."
            )
            metadata["reference_cover_price_vnd"] = None
            metadata.setdefault("raw_metadata", {})[
                "rejected_cover_price_vnd"
            ] = cover_price_vnd

    page_count = metadata.get("reference_page_count")

    if (
        page_count is not None
        and not 1 <= int(page_count) <= 5_000
    ):
        warnings.append(
            "Reference page count was rejected as implausible: "
            f"{page_count}."
        )
        metadata["reference_page_count"] = None
        metadata.setdefault("raw_metadata", {})[
            "rejected_page_count"
        ] = page_count

    if parser_name == "FAHASA_METADATA_PARSER":
        if not metadata.get("reference_image_url"):
            warnings.append("Reference image URL was not found.")
        if not metadata.get("reference_description"):
            warnings.append("Reference description was not found.")

    return warnings


def parse_alpha_books_metadata(
    page: Page,
    body_text: str,
) -> dict[str, Any]:
    """Parse one Alpha Books product page."""
    json_ld_objects = extract_json_ld(
        page
    )

    product_json_ld = find_product_json_ld(
        json_ld_objects
    )

    title = extract_title(
        page=page,
        product_json_ld=product_json_ld,
    )

    author = extract_author(
        body_text=body_text,
        product_json_ld=product_json_ld,
    )

    isbn = extract_isbn(
        body_text=body_text,
        product_json_ld=product_json_ld,
    )

    publisher = extract_publisher(
        body_text
    )

    page_count = extract_page_count(
        body_text
    )

    publication_year = extract_publication_year(
        body_text
    )

    translator = extract_translator(
        body_text
    )

    book_format = extract_book_format(
        body_text
    )

    displayed_supplier = extract_supplier_name(
        body_text
    )

    (
        length_cm,
        width_cm,
        height_cm,
        raw_dimensions,
    ) = extract_dimensions(
        body_text
    )

    (
        current_price_vnd,
        cover_price_vnd,
    ) = extract_price_values(
        page=page,
        body_text=body_text,
        product_json_ld=product_json_ld,
    )

    description = extract_description(
        page=page,
        product_json_ld=product_json_ld,
    )

    image_url = extract_image_url(
        page=page,
        product_json_ld=product_json_ld,
    )

    raw_metadata = {
        "parser_name": "alpha_books",
        "parser_version": "1.0.0",
        "publication_year": publication_year,
        "translator": translator,
        "book_format": book_format,
        "displayed_supplier": displayed_supplier,
        "raw_dimensions": raw_dimensions,
        "current_price_vnd": decimal_to_json_value(
            current_price_vnd
        ),
        "cover_price_vnd": decimal_to_json_value(
            cover_price_vnd
        ),
        "json_ld_found": (
            product_json_ld is not None
        ),
    }

    return {
        "reference_title": title,
        "reference_isbn": isbn,
        "reference_author": author,
        "reference_publisher": publisher,
        "reference_page_count": page_count,
        "reference_weight_grams": None,
        "reference_length_cm": decimal_to_json_value(
            length_cm
        ),
        "reference_width_cm": decimal_to_json_value(
            width_cm
        ),
        "reference_height_cm": decimal_to_json_value(
            height_cm
        ),
        "reference_cover_price_vnd": (
            decimal_to_json_value(
                cover_price_vnd
            )
        ),
        "reference_description": description,
        "reference_image_url": image_url,
        "raw_metadata": raw_metadata,
    }


def validate_alpha_books_metadata(
    metadata: dict[str, Any],
) -> list[str]:
    """Return parser validation warnings."""
    warnings: list[str] = []

    if not metadata.get(
        "reference_title"
    ):
        warnings.append(
            "Reference title was not found."
        )

    if not metadata.get(
        "reference_author"
    ):
        warnings.append(
            "Reference author was not found."
        )

    if not metadata.get(
        "reference_publisher"
    ):
        warnings.append(
            "Reference publisher was not found."
        )

    if not metadata.get(
        "reference_isbn"
    ):
        warnings.append(
            "Reference ISBN was not found."
        )

    if not metadata.get(
        "reference_weight_grams"
    ):
        warnings.append(
            "Reference weight was not found."
        )

    return warnings


def select_next_reference_queue_item(
    repository: SupabaseRepository,
) -> dict[str, Any] | None:
    """Return the oldest selected reference URL waiting for collection."""
    discovery_response = (
        repository.client
        .table(
            "candidate_reference_sources"
        )
        .select(
            "discovery_id, "
            "candidate_id, "
            "source_url_id, "
            "discovery_status, "
            "is_selected_for_crawl, "
            "created_at"
        )
        .eq(
            "discovery_status",
            "SELECTED",
        )
        .eq(
            "is_selected_for_crawl",
            True,
        )
        .order(
            "created_at",
            desc=False,
        )
        .execute()
    )

    discovery_records = (
        discovery_response.data
        or []
    )

    for discovery in discovery_records:
        source_response = (
            repository.client
            .table("source_urls")
            .select(
                "source_url_id, "
                "batch_id, "
                "source_type, "
                "source_url, "
                "source_name, "
                "is_authorized, "
                "crawl_status"
            )
            .eq(
                "source_url_id",
                discovery["source_url_id"],
            )
            .eq(
                "crawl_status",
                "PENDING",
            )
            .limit(1)
            .execute()
        )

        source_records = (
            source_response.data
            or []
        )

        if not source_records:
            continue

        source = source_records[0]

        if source.get(
            "source_type"
        ) not in SUPPORTED_SOURCE_TYPES:
            continue

        candidate_response = (
            repository.client
            .table("product_candidates")
            .select(
                "candidate_id, "
                "candidate_code, "
                "extracted_title, "
                "extracted_author, "
                "possible_isbn"
            )
            .eq(
                "candidate_id",
                discovery["candidate_id"],
            )
            .limit(1)
            .execute()
        )

        candidate_records = (
            candidate_response.data
            or []
        )

        if not candidate_records:
            continue

        return {
            "discovery": discovery,
            "source": source,
            "candidate": candidate_records[0],
        }

    return None


def update_source_status(
    repository: SupabaseRepository,
    source_url_id: str,
    crawl_status: str,
    last_error: str | None,
    set_last_crawled_at: bool,
) -> None:
    """Update technical crawl status for a source URL."""
    now = utc_now_iso()

    payload: dict[str, Any] = {
        "crawl_status": crawl_status,
        "last_error": last_error,
        "updated_at": now,
    }

    if set_last_crawled_at:
        payload["last_crawled_at"] = now

    (
        repository.client
        .table("source_urls")
        .update(payload)
        .eq(
            "source_url_id",
            source_url_id,
        )
        .execute()
    )


def update_discovery_status(
    repository: SupabaseRepository,
    discovery_id: str,
    discovery_status: str,
) -> None:
    """Update the candidate-to-source discovery status."""
    now = utc_now_iso()

    payload: dict[str, Any] = {
        "discovery_status": discovery_status,
        "updated_at": now,
    }

    if discovery_status in {
        "CRAWLED",
        "MATCHED",
        "REJECTED",
        "FAILED",
    }:
        payload["reviewed_at"] = now

    (
        repository.client
        .table(
            "candidate_reference_sources"
        )
        .update(payload)
        .eq(
            "discovery_id",
            discovery_id,
        )
        .execute()
    )


def find_existing_raw_page(
    repository: SupabaseRepository,
    source_url_id: str,
    content_hash: str,
) -> dict[str, Any] | None:
    """Find the same stored content snapshot."""
    response = (
        repository.client
        .table("raw_pages")
        .select(
            "raw_page_id, "
            "source_url_id, "
            "content_hash, "
            "collected_at"
        )
        .eq(
            "source_url_id",
            source_url_id,
        )
        .eq(
            "content_hash",
            content_hash,
        )
        .limit(1)
        .execute()
    )

    records = response.data or []

    if not records:
        return None

    return records[0]


def save_raw_page(
    repository: SupabaseRepository,
    queue_item: dict[str, Any],
    page_title: str | None,
    raw_text: str,
    raw_html: str,
    content_hash: str,
    parser_name: str,
) -> tuple[
    dict[str, Any],
    bool,
]:
    """Create or reuse one raw reference page snapshot."""
    source = queue_item[
        "source"
    ]

    existing = find_existing_raw_page(
        repository=repository,
        source_url_id=source[
            "source_url_id"
        ],
        content_hash=content_hash,
    )

    if existing is not None:
        return existing, False

    page_type = PAGE_TYPE_BY_SOURCE_TYPE.get(
        source["source_type"],
        "OTHER",
    )

    now = utc_now_iso()

    payload = {
        "batch_id": source["batch_id"],
        "source_url_id": source[
            "source_url_id"
        ],
        "page_type": page_type,
        "page_url": source["source_url"],
        "raw_title": page_title,
        "raw_text": raw_text,
        "raw_html": raw_html,
        "content_hash": content_hash,
        "collected_at": now,
        "collector_name": COLLECTOR_NAME,
        "collector_version": COLLECTOR_VERSION,
        "cleaned_text": raw_text,
        "cleaning_status": "CLEANED",
        "cleaning_method": parser_name,
        "cleaned_at": now,
    }

    response = (
        repository.client
        .table("raw_pages")
        .insert(payload)
        .execute()
    )

    records = response.data or []

    if not records:
        raise RuntimeError(
            "Raw page insert returned no record."
        )

    return records[0], True


def find_existing_product_reference(
    repository: SupabaseRepository,
    candidate_id: str,
    source_url_id: str,
) -> dict[str, Any] | None:
    """Find one existing candidate/source product reference."""
    response = (
        repository.client
        .table("product_references")
        .select(
            "reference_id, "
            "candidate_id, "
            "source_url_id"
        )
        .eq(
            "candidate_id",
            candidate_id,
        )
        .eq(
            "source_url_id",
            source_url_id,
        )
        .limit(1)
        .execute()
    )

    records = response.data or []

    if not records:
        return None

    return records[0]


def save_product_reference(
    repository: SupabaseRepository,
    queue_item: dict[str, Any],
    metadata: dict[str, Any],
) -> tuple[
    dict[str, Any],
    bool,
]:
    """Insert or update one normalized product reference."""
    source = queue_item[
        "source"
    ]

    candidate = queue_item[
        "candidate"
    ]

    now = utc_now_iso()

    payload = {
        "candidate_id": candidate[
            "candidate_id"
        ],
        "source_url_id": source[
            "source_url_id"
        ],
        "source_type": source[
            "source_type"
        ],
        "source_name": source.get(
            "source_name"
        ),
        "source_url": source.get(
            "source_url"
        ),
        "reference_title": metadata.get(
            "reference_title"
        ),
        "reference_isbn": metadata.get(
            "reference_isbn"
        ),
        "reference_author": metadata.get(
            "reference_author"
        ),
        "reference_publisher": metadata.get(
            "reference_publisher"
        ),
        "reference_page_count": metadata.get(
            "reference_page_count"
        ),
        "reference_weight_grams": metadata.get(
            "reference_weight_grams"
        ),
        "reference_length_cm": metadata.get(
            "reference_length_cm"
        ),
        "reference_width_cm": metadata.get(
            "reference_width_cm"
        ),
        "reference_height_cm": metadata.get(
            "reference_height_cm"
        ),
        "reference_cover_price_vnd": metadata.get(
            "reference_cover_price_vnd"
        ),
        "reference_description": metadata.get(
            "reference_description"
        ),
        "reference_image_url": metadata.get(
            "reference_image_url"
        ),
        "match_decision": None,
        "match_confidence": None,
        "source_priority": SOURCE_PRIORITY_BY_TYPE.get(
            source["source_type"],
            5,
        ),
        "raw_metadata": metadata.get(
            "raw_metadata",
            {},
        ),
        "collected_at": now,
        "updated_at": now,
    }

    existing = find_existing_product_reference(
        repository=repository,
        candidate_id=candidate[
            "candidate_id"
        ],
        source_url_id=source[
            "source_url_id"
        ],
    )

    if existing is not None:
        response = (
            repository.client
            .table("product_references")
            .update(payload)
            .eq(
                "reference_id",
                existing["reference_id"],
            )
            .execute()
        )

        records = response.data or []

        if not records:
            raise RuntimeError(
                "Product reference update returned no record."
            )

        return records[0], False

    response = (
        repository.client
        .table("product_references")
        .insert(payload)
        .execute()
    )

    records = response.data or []

    if not records:
        raise RuntimeError(
            "Product reference insert returned no record."
        )

    return records[0], True


def create_browser(
    playwright: Playwright,
) -> tuple[
    Browser,
    BrowserContext,
]:
    """Create a browser for public product-page collection."""
    browser = playwright.chromium.launch(
        headless=True,
    )

    context = browser.new_context(
        locale="vi-VN",
        viewport={
            "width": 1440,
            "height": 1200,
        },
        user_agent=(
            "Mozilla/5.0 "
            "(Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 "
            "(KHTML, like Gecko) "
            "Chrome/150.0.0.0 "
            "Safari/537.36"
        ),
    )

    context.set_default_timeout(
        NAVIGATION_TIMEOUT_MS
    )

    return (
        browser,
        context,
    )


def collect_page(
    context: BrowserContext,
    source_url: str,
) -> tuple[
    Page,
    str,
    str,
    str | None,
]:
    """Load one product page and return text, HTML, and title."""
    page = context.new_page()

    page.goto(
        source_url,
        wait_until="domcontentloaded",
        timeout=NAVIGATION_TIMEOUT_MS,
    )

    try:
        page.wait_for_load_state(
            "networkidle",
            timeout=15_000,
        )

    except PlaywrightTimeoutError:
        print(
            "Network idle was not reached; "
            "continuing with the loaded document."
        )

    body_locator = page.locator(
        "body"
    )

    raw_text = body_locator.inner_text(
        timeout=NAVIGATION_TIMEOUT_MS
    )

    raw_html = page.content()

    page_title = normalize_whitespace(
        page.title()
    )

    return (
        page,
        raw_text,
        raw_html,
        page_title,
    )


def select_parser(
    source_url: str,
) -> str:
    """Select a parser based on the source domain."""
    hostname = (
        urlsplit(
            source_url
        ).hostname
        or ""
    ).lower()

    if (
        hostname == "alphabooks.vn"
        or hostname.endswith(
            ".alphabooks.vn"
        )
    ):
        return "ALPHA_BOOKS_METADATA_PARSER"

    if (
        hostname == "fahasa.com"
        or hostname.endswith(
            ".fahasa.com"
        )
    ):
        return "FAHASA_METADATA_PARSER"

    generic_domains = {
        "netabooks.vn": "NETABOOKS_GENERIC_PARSER",
        "nhanam.vn": "NHANAM_GENERIC_PARSER",
        "dinhtibooks.com.vn": "DINHTI_GENERIC_PARSER",
        "firstnews.vn": "FIRSTNEWS_GENERIC_PARSER",
    }

    for domain, parser_name in generic_domains.items():
        if hostname == domain or hostname.endswith(f".{domain}"):
            return parser_name

    raise RuntimeError(
        "No metadata parser is available for domain: "
        f"{hostname}"
    )


def parse_reference_page(
    parser_name: str,
    page: Page,
    raw_text: str,
) -> dict[str, Any]:
    """Parse metadata with the selected domain parser."""
    if parser_name == "ALPHA_BOOKS_METADATA_PARSER":
        return parse_alpha_books_metadata(
            page=page,
            body_text=raw_text,
        )

    if parser_name == "FAHASA_METADATA_PARSER":
        return parse_fahasa_metadata(
            page=page,
            body_text=raw_text,
        )

    generic_parser_labels = {
        "NETABOOKS_GENERIC_PARSER": "netabooks_generic",
        "NHANAM_GENERIC_PARSER": "nhanam_generic",
        "DINHTI_GENERIC_PARSER": "dinhti_generic",
        "FIRSTNEWS_GENERIC_PARSER": "firstnews_generic",
    }

    if parser_name in generic_parser_labels:
        return parse_generic_book_metadata(
            page=page,
            body_text=raw_text,
            parser_label=generic_parser_labels[parser_name],
        )

    raise RuntimeError(
        f"Unsupported parser: {parser_name}"
    )


def print_queue_item(
    queue_item: dict[str, Any],
) -> None:
    """Print the selected queue item."""
    candidate = queue_item[
        "candidate"
    ]

    source = queue_item[
        "source"
    ]

    print()
    print("=" * 78)
    print("REFERENCE COLLECTION QUEUE ITEM")
    print("=" * 78)
    print(
        "Candidate code: "
        f"{candidate.get('candidate_code')}"
    )
    print(
        "Candidate title: "
        f"{candidate.get('extracted_title')}"
    )
    print(
        "Candidate author: "
        f"{candidate.get('extracted_author') or '[not found]'}"
    )
    print(
        "Source type: "
        f"{source.get('source_type')}"
    )
    print(
        "Source name: "
        f"{source.get('source_name')}"
    )
    print(
        "Source URL: "
        f"{source.get('source_url')}"
    )


def print_metadata_result(
    metadata: dict[str, Any],
    warnings: list[str],
) -> None:
    """Print normalized metadata and parser warnings."""
    print()
    print("=" * 78)
    print("PARSED REFERENCE METADATA")
    print("=" * 78)
    print(
        "Title: "
        f"{metadata.get('reference_title') or '[not found]'}"
    )
    print(
        "Author: "
        f"{metadata.get('reference_author') or '[not found]'}"
    )
    print(
        "ISBN: "
        f"{metadata.get('reference_isbn') or '[not found]'}"
    )
    print(
        "Publisher: "
        f"{metadata.get('reference_publisher') or '[not found]'}"
    )
    print(
        "Page count: "
        f"{metadata.get('reference_page_count') or '[not found]'}"
    )
    print(
        "Weight grams: "
        f"{metadata.get('reference_weight_grams') or '[not found]'}"
    )
    print(
        "Length cm: "
        f"{metadata.get('reference_length_cm') or '[not found]'}"
    )
    print(
        "Width cm: "
        f"{metadata.get('reference_width_cm') or '[not found]'}"
    )
    print(
        "Height cm: "
        f"{metadata.get('reference_height_cm') or '[not found]'}"
    )
    print(
        "Cover price VND: "
        f"{metadata.get('reference_cover_price_vnd') or '[not found]'}"
    )
    print(
        "Image URL: "
        f"{metadata.get('reference_image_url') or '[not found]'}"
    )

    if warnings:
        print()
        print("Parser warnings:")

        for warning in warnings:
            print(
                f"- {warning}"
            )


def main() -> None:
    """Collect one selected reference source."""
    load_dotenv()

    print(
        "Reference metadata collector started."
    )
    print(
        f"Version: {COLLECTOR_VERSION}"
    )

    repository = SupabaseRepository()

    queue_item = select_next_reference_queue_item(
        repository
    )

    if queue_item is None:
        print(
            "No selected pending reference URL was found."
        )
        return

    print_queue_item(
        queue_item
    )

    source = queue_item[
        "source"
    ]

    discovery = queue_item[
        "discovery"
    ]

    source_url_id = str(
        source["source_url_id"]
    )

    discovery_id = str(
        discovery["discovery_id"]
    )

    parser_name = select_parser(
        source["source_url"]
    )

    update_source_status(
        repository=repository,
        source_url_id=source_url_id,
        crawl_status="IN_PROGRESS",
        last_error=None,
        set_last_crawled_at=False,
    )

    try:
        with sync_playwright() as playwright:
            browser, context = create_browser(
                playwright
            )

            try:
                (
                    page,
                    raw_text,
                    raw_html,
                    page_title,
                ) = collect_page(
                    context=context,
                    source_url=source[
                        "source_url"
                    ],
                )

                content_hash = calculate_content_hash(
                    raw_html
                )

                metadata = parse_reference_page(
                    parser_name=parser_name,
                    page=page,
                    raw_text=raw_text,
                )

                warnings = validate_reference_metadata(
                    metadata=metadata,
                    parser_name=parser_name,
                )

                metadata["raw_metadata"][
                    "validation_warnings"
                ] = warnings

                metadata["raw_metadata"][
                    "content_hash"
                ] = content_hash

                print_metadata_result(
                    metadata=metadata,
                    warnings=warnings,
                )

                raw_page, raw_page_created = (
                    save_raw_page(
                        repository=repository,
                        queue_item=queue_item,
                        page_title=page_title,
                        raw_text=raw_text,
                        raw_html=raw_html,
                        content_hash=content_hash,
                        parser_name=parser_name,
                    )
                )

                (
                    product_reference,
                    reference_created,
                ) = save_product_reference(
                    repository=repository,
                    queue_item=queue_item,
                    metadata=metadata,
                )

            finally:
                context.close()
                browser.close()

        update_source_status(
            repository=repository,
            source_url_id=source_url_id,
            crawl_status="COLLECTED",
            last_error=None,
            set_last_crawled_at=True,
        )

        update_discovery_status(
            repository=repository,
            discovery_id=discovery_id,
            discovery_status="CRAWLED",
        )

    except Exception as error:
        error_message = (
            f"{type(error).__name__}: {error}"
        )

        update_source_status(
            repository=repository,
            source_url_id=source_url_id,
            crawl_status="FAILED",
            last_error=error_message[
                :4000
            ],
            set_last_crawled_at=True,
        )

        update_discovery_status(
            repository=repository,
            discovery_id=discovery_id,
            discovery_status="FAILED",
        )

        raise

    print()
    print("=" * 78)
    print("REFERENCE COLLECTION RESULT")
    print("=" * 78)
    print(
        "Raw page: "
        + (
            "CREATED"
            if raw_page_created
            else "REUSED"
        )
    )
    print(
        "Raw page ID: "
        f"{raw_page.get('raw_page_id')}"
    )
    print(
        "Product reference: "
        + (
            "CREATED"
            if reference_created
            else "UPDATED"
        )
    )
    print(
        "Reference ID: "
        f"{product_reference.get('reference_id')}"
    )
    print(
        "Source crawl status: COLLECTED"
    )
    print(
        "Discovery status: CRAWLED"
    )

    print()
    print(
        "Reference metadata collector finished."
    )


if __name__ == "__main__":
    try:
        main()

    except KeyboardInterrupt:
        print()
        print(
            "Reference metadata collection was cancelled."
        )
        sys.exit(130)

    except Exception as error:
        print()
        print(
            "Reference metadata collection failed."
        )
        print(
            f"Error type: {type(error).__name__}"
        )
        print(
            f"Error details: {error}"
        )
        sys.exit(1)