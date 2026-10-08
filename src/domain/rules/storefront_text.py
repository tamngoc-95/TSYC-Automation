"""Deterministic storefront-text normalization and defect detection.

CLAUDE.md section 15.1 / TSYC_CONTENT_GUIDE.md sections 11-12: customer-facing
product content must never contain internal workflow or provenance notes,
stock/shipping/promotion wording, retailer SEO boilerplate, unresolved HTML
entities, or text cut off by source extraction.

This module is the single shared definition of those defects. It is used by:

  - scripts/collect_reference_metadata.py  (normalize a collected source
    description, and prefer a usable one over a truncated meta snippet)
  - src.domain.rules.content_rules         (automatic-approval gate, and
    draft-safe reference selection)
  - scripts/prepare_product_content.py     (AUTO_REVISE formatting, the
    authorized REPAIR action, and the vi -> en/de translation gate)
  - scripts/update_woocommerce_draft_content.py (refuses to push failing
    content to a WooCommerce draft)

Pure functions only: no I/O, no project imports, directly unit-testable.

Detection is deliberately conservative (a false positive costs one review
cycle; a false negative ships a defect to a customer), but it never flags
ordinary punctuation: an ellipsis is only treated as extraction truncation
when it ends a field or a paragraph, never inside a sentence.
"""
from __future__ import annotations

import html
import re
import unicodedata
from typing import Iterable, Mapping, Sequence

# --- defect codes ---------------------------------------------------

HTML_ENTITY = "HTML_ENTITY"
HTML_MARKUP = "HTML_MARKUP"
TRUNCATED = "TRUNCATED"
PROVENANCE_NOTE = "PROVENANCE_NOTE"
INTERNAL_WORKFLOW = "INTERNAL_WORKFLOW"
SOURCE_BOILERPLATE = "SOURCE_BOILERPLATE"
STOCK_WORDING = "STOCK_WORDING"
SHIPPING_WORDING = "SHIPPING_WORDING"
QUOTED_EXCERPT = "QUOTED_EXCERPT"
# Source-extraction typography artifacts (2026-10-02 production audit:
# CSV-escaped ""Intelligence Quotient"" and "không?." reached APPROVED
# storefront text). Both have a single deterministic repair
# (repair_typography); neither ever changes a word.
DOUBLED_QUOTE = "DOUBLED_QUOTE"
MALFORMED_PUNCTUATION = "MALFORMED_PUNCTUATION"
# Encyclopedia footnote marker glued to the text ("...Việt Nam[3].",
# 2026-10-06 Fast Track Batch 5). Removing it never changes a word.
CITATION_MARKER = "CITATION_MARKER"
# "ISBN: <value>" where the value is not an ISBN (a retailer SKU such as
# NetaBooks "2421762043452", or an 893 barcode) -- CLAUDE.md 2.2/2.3.
INVALID_ISBN = "INVALID_ISBN"
# Content-quality codes (2026-10-08 review of the 30 Fast Track drafts):
# a retailer page's table of contents, praise/review/press section copied
# into the description (repairable: the section is dropped whole);
THIRD_PARTY_SECTION = "THIRD_PARTY_SECTION"
# Approval-gate codes (content_rules.evaluate_description_substance, not
# storefront defects): the author's own first-person preface copied as the
# description, and a description too thin to describe the book. Neither
# is repairable without new text.
FIRST_PERSON_SOURCE = "FIRST_PERSON_SOURCE"
THIN_DESCRIPTION = "THIN_DESCRIPTION"

ALL_DEFECT_CODES = (
    HTML_ENTITY,
    HTML_MARKUP,
    TRUNCATED,
    PROVENANCE_NOTE,
    INTERNAL_WORKFLOW,
    SOURCE_BOILERPLATE,
    STOCK_WORDING,
    SHIPPING_WORDING,
    QUOTED_EXCERPT,
    DOUBLED_QUOTE,
    MALFORMED_PUNCTUATION,
    CITATION_MARKER,
    INVALID_ISBN,
    THIRD_PARTY_SECTION,
)

TYPOGRAPHY_DEFECT_CODES = frozenset({DOUBLED_QUOTE, MALFORMED_PUNCTUATION, CITATION_MARKER})

# --- patterns -------------------------------------------------------

_HTML_ENTITY_RE = re.compile(r"&(?:[a-zA-Z][a-zA-Z0-9]{1,31}|#\d{1,7}|#[xX][0-9a-fA-F]{1,6});")
_HTML_TAG_RE = re.compile(r"</?[a-zA-Z][a-zA-Z0-9]*(?:\s[^<>]*)?/?>")

# An ellipsis (three dots or U+2026) at the very end of a field: the
# signature of a source snippet cut by extraction. Ellipses inside the
# text -- mid-sentence or closing a stylistic paragraph of a full
# description -- are legitimate punctuation.
_TRUNCATION_RE = re.compile(r"(?:\.{3}|…)\s*$")

# Two adjacent ASCII double quotes: a CSV/JSON escaping artifact. Real
# prose never needs an empty "" pair in a product description.
_DOUBLED_QUOTE_RE = re.compile(r'""')
# "?." / "!." / ",." -- a terminal mark followed by a stray period. The
# negative lookahead keeps an ellipsis ("?...", ",...") untouched. ".,"
# is deliberately NOT included: "v.v.," (Vietnamese "etc.,") is correct.
_MALFORMED_PUNCTUATION_RE = re.compile(r"[?!,]\.(?!\.)")
_STRAY_PERIOD_AFTER_MARK_RE = re.compile(r"([?!])\.(?!\.)")
_COMMA_PERIOD_RE = re.compile(r",\.(?!\.)")
# "[3]" directly after a word/punctuation mark (no space): a footnote
# reference. "Tập [1]" (space before the bracket) is never matched.
_CITATION_MARKER_RE = re.compile(r"(?<=[^\s\[])\[\d{1,3}\]")

# Labelled ISBN value. Validity mirrors identity_rules.looks_like_valid_
# isbn() (kept local: this module has no project imports; a test pins
# the two together): ISBN-13 starting 978/979, or ISBN-10.
_ISBN_LABEL_RE = re.compile(r"\bISBN(?:-1[03])?\s*:?\s*([0-9Xx][0-9Xx \-]{8,20}[0-9Xx])")


def _is_isbn_value(value: str) -> bool:
    digits = re.sub(r"[\s\-]", "", value).upper()
    if len(digits) == 13 and digits.isdigit():
        return digits.startswith(("978", "979"))
    return len(digits) == 10 and digits[:9].isdigit() and (digits[9].isdigit() or digits[9] == "X")


def has_invalid_isbn(text: str | None) -> bool:
    """True when text labels a non-ISBN value as "ISBN"."""
    if not text:
        return False
    return any(not _is_isbn_value(match.group(1)) for match in _ISBN_LABEL_RE.finditer(str(text)))

# Same word twice in a row on one line ("biết biết"). ADVISORY ONLY:
# Vietnamese reduplication is grammatical and very common ("song song",
# "luôn luôn", "từ từ", "dần dần", "mãi mãi" -- measured on production
# content 2026-10-02), and no deterministic rule separates it from a
# typo. Never a defect code, never repaired.
_DUPLICATED_WORD_RE = re.compile(r"\b(\w{2,})[ \t]+\1\b", re.IGNORECASE)

_PROVENANCE_PATTERNS = (
    re.compile(r"mô tả tham khảo từ nguồn", re.IGNORECASE),
    re.compile(r"nguồn\s+(?:FAHASA|BOOKSTORE|PUBLISHER|AUTHORIZED_SUPPLIER|FACEBOOK_POST|OTHER)\b", re.IGNORECASE),
    re.compile(r"\bđã được xác minh\b", re.IGNORECASE),
    re.compile(r"dữ liệu sản phẩm đã xác minh", re.IGNORECASE),
    re.compile(r"\b(?:verified|reference) source\b", re.IGNORECASE),
)

_INTERNAL_WORKFLOW_PATTERNS = (
    re.compile(r"manager (?:must|should) review", re.IGNORECASE),
    re.compile(r"pending (?:manager|admin|staff) review", re.IGNORECASE),
    re.compile(r"should be completed later", re.IGNORECASE),
    re.compile(r"to be (?:filled|completed|updated) later", re.IGNORECASE),
    re.compile(r"\bTODO\b"),
    re.compile(r"\bFIXME\b"),
    re.compile(r"placeholder text", re.IGNORECASE),
    re.compile(r"người quản lý", re.IGNORECASE),
    re.compile(r"sản phẩm nháp", re.IGNORECASE),
    re.compile(r"(?:cần được|chờ|đang chờ)\s+(?:kiểm tra|duyệt|xét duyệt)", re.IGNORECASE),
    re.compile(r"(?:sẽ|cần) (?:được )?bổ sung (?:sau|thêm)", re.IGNORECASE),
    re.compile(r"trước khi (?:sản phẩm được )?xuất bản", re.IGNORECASE),
)

# A retailer section heading kept as its own line/sentence: "Giới thiệu
# sách <title>" with no sentence period (the title itself may end in "?"
# -- 2026-10-06 Fast Track Batch 5, "... Mẹ Vẫn Yêu Con Chứ?").
_SECTION_HEADING_LINE_RE = re.compile(
    r"(?im)^\s*(?:giới thiệu sách|giới thiệu nội dung|thông tin sản phẩm|mô tả sản phẩm)\b[^.!\n]{0,160}$"
)
# Retailer cross-sell: "Mời các bạn tìm mua trọn bộ:", "Mời quý độc giả
# đón đọc ...", "... tìm mua trọn bộ".
_CROSS_SELL_RE = re.compile(
    r"(?i)\bmời (?:các |quý )?(?:bạn|độc giả|phụ huynh|ba mẹ|bố mẹ)\b[^\n]{0,80}?\b(?:tìm mua|đặt mua|mua|đón đọc|tìm đọc|sưu tầm)\b"
    r"|\b(?:tìm mua|đặt mua|sưu tầm) (?:trọn|cả) bộ\b"
)

# The NetaBooks footer "Sách <title> của tác giả <name>, có bán tại ..."
# split at an honorific period ("... của tác giả TS.") leaves an orphaned
# stub line once the store clause is removed (2026-10-06, Gia Đình Tỉnh
# Thức). Only a whole line of exactly that shape matches -- a real
# sentence about the book continues with a verb and is never a match.
_RETAILER_FOOTER_STUB_RE = re.compile(
    r"(?im)^\s*sách\s[^.!?\n]{1,150}\bcủa tác giả\b(?:\s+[^\s.!?]{1,20}){0,4}\.?\s*$"
)

_SOURCE_BOILERPLATE_PATTERNS = (
    re.compile(r"có bán tại", re.IGNORECASE),
    re.compile(r"nhà sách online", re.IGNORECASE),
    re.compile(r"\bnetabooks\b", re.IGNORECASE),
    re.compile(r"\bfahasa\b", re.IGNORECASE),
    re.compile(r"\btiki\b", re.IGNORECASE),
    re.compile(r"\bshopee\b", re.IGNORECASE),
    re.compile(r"gian hàng", re.IGNORECASE),
    re.compile(r"mua ngay", re.IGNORECASE),
    re.compile(r"giá tốt", re.IGNORECASE),
    re.compile(r"ưu đãi", re.IGNORECASE),
    re.compile(r"bao sách miễn phí", re.IGNORECASE),
    re.compile(r"tặng (?:kèm )?bookmark", re.IGNORECASE),
    re.compile(r"✔️|✔|✅"),
    # Publisher-page button text that leaked into extracted metadata.
    re.compile(r"\bĐọc thử\b"),
    _SECTION_HEADING_LINE_RE,
    _CROSS_SELL_RE,
    _RETAILER_FOOTER_STUB_RE,
    # Retailer rating widgets copied with the description: "4,7*/5 với
    # 1,607 lượt đánh giá trên trang Amazon.com" (2026-10-06 Batch 20).
    re.compile(r"\d(?:[.,]\d+)?\s*\*?\s*/\s*5\b[^\n]{0,40}\blượt đánh giá", re.IGNORECASE),
    re.compile(r"\bamazon\.(?:com|de|co\.uk)\b", re.IGNORECASE),
    re.compile(r"\bgoodreads(?:\.com)?\b", re.IGNORECASE),
    # Retailer promotion / unsupported marketing (2026-10-08 review):
    # "cuốn sách hot nhất", "bộ sách mà mọi em bé 5+ đều nhất định cần
    # có", "không nên bỏ qua", "đã chính thức trở lại kệ sách", a
    # subscription-box name, a gift promise ("Mừng tuổi ngay ... sticker").
    re.compile(r"\bhot nhất\b", re.IGNORECASE),
    re.compile(r"\bnhất định (?:phải|cần) (?:có|đọc)\b", re.IGNORECASE),
    re.compile(r"\bkhông (?:nên|thể) bỏ (?:qua|lỡ)\b", re.IGNORECASE),
    re.compile(r"\btrở lại kệ sách\b", re.IGNORECASE),
    re.compile(r"\bhộp háo hức\b", re.IGNORECASE),
    re.compile(r"\bmừng tuổi ngay\b", re.IGNORECASE),
    # Source-page navigation left as its own short line.
    re.compile(r"(?im)^\s*(?:trang chủ|xem thêm|xem tất cả|quay lại|danh mục sản phẩm)\b[^.!?\n]{0,60}$"),
)

# --- third-party sections, first-person sources, thin descriptions ----

# Headings that open a block which is not the shop's description of the
# book: table of contents, praise/reviews, press quotes. The block runs to
# the next author-information heading (kept) or to the end.
_THIRD_PARTY_HEADING_RE = re.compile(
    r"^\s*(?:mục lục(?: sách)?|lời khen(?: tặng| cho cuốn sách[^\n]{0,80})?|nhận xét|"
    r"một số đánh giá[^\n]{0,60}|đánh giá(?: của độc giả)?|người nổi tiếng nói về[^\n]{0,80}|"
    r"báo chí (?:nói|nhắc|viết) gì về[^\n]{0,80}|review(?:s)?)\s*:?\s*$",
    re.IGNORECASE,
)
_SECTION_END_HEADING_RE = re.compile(
    r"^\s*(?:thông tin tác giả|giới thiệu tác giả|về tác giả)\b", re.IGNORECASE
)
_QUOTED_SPAN_FOR_VOICE_RE = re.compile(r"[“\"«„][^“”\"«»„]{0,600}[”\"»“]")
_FIRST_PERSON_RE = re.compile(r"(?<!\w)tôi(?!\w)", re.IGNORECASE)
_FIRST_PERSON_MIN_OCCURRENCES = 3
MIN_DESCRIPTION_LENGTH = 200


def drop_third_party_sections(text: str | None) -> str:
    """Remove table-of-contents / praise / review / press blocks: the
    heading paragraph and everything after it up to the next author-
    information heading (kept) or the end. Never touches other prose."""
    if not text:
        return ""
    kept: list[str] = []
    skipping = False
    for paragraph in normalize_paragraphs(text).split("\n\n"):
        if _THIRD_PARTY_HEADING_RE.match(paragraph):
            skipping = True
            continue
        if skipping and _SECTION_END_HEADING_RE.match(paragraph):
            skipping = False
        if not skipping:
            kept.append(paragraph)
    return "\n\n".join(kept)


def has_third_party_section(text: str | None) -> bool:
    return bool(text) and any(
        _THIRD_PARTY_HEADING_RE.match(paragraph)
        for paragraph in normalize_paragraphs(text).split("\n\n")
    )


def is_first_person_source(text: str | None) -> bool:
    """True when the description is narrated in the first person outside
    quotation marks ("tôi" at least _FIRST_PERSON_MIN_OCCURRENCES times),
    i.e. an author preface copied as the product description."""
    if not text:
        return False
    unquoted = _QUOTED_SPAN_FOR_VOICE_RE.sub(" ", unicodedata.normalize("NFC", str(text)))
    return len(_FIRST_PERSON_RE.findall(unquoted)) >= _FIRST_PERSON_MIN_OCCURRENCES

_STOCK_PATTERNS = (
    re.compile(r"(?:hiện|đang|sẵn)\s+có\s+(?:bán\s+)?tại\s+Tiệm Sách Yêu Con", re.IGNORECASE),
    re.compile(r"\bcó tại\s+Tiệm Sách Yêu Con", re.IGNORECASE),
    re.compile(r"là ấn phẩm đang có tại", re.IGNORECASE),
    re.compile(r"còn hàng", re.IGNORECASE),
    re.compile(r"hết hàng", re.IGNORECASE),
    re.compile(r"số lượng có hạn", re.IGNORECASE),
    re.compile(r"\bin stock\b", re.IGNORECASE),
    re.compile(r"\bauf lager\b", re.IGNORECASE),
    re.compile(r"\bavailable (?:now )?at Tiệm Sách Yêu Con", re.IGNORECASE),
    re.compile(r"bei Tiệm Sách Yêu Con\s+(?:jetzt\s+|ab sofort\s+)?erhältlich", re.IGNORECASE),
)

_SHIPPING_PATTERNS = (
    re.compile(r"giao (?:hàng|nhanh)", re.IGNORECASE),
    re.compile(r"free\s*ship", re.IGNORECASE),
    re.compile(r"miễn phí vận chuyển", re.IGNORECASE),
    re.compile(r"\bfree shipping\b", re.IGNORECASE),
    re.compile(r"\bversandkostenfrei\b", re.IGNORECASE),
)

_PATTERN_GROUPS: tuple[tuple[str, Sequence[re.Pattern[str]]], ...] = (
    (PROVENANCE_NOTE, _PROVENANCE_PATTERNS),
    (INTERNAL_WORKFLOW, _INTERNAL_WORKFLOW_PATTERNS),
    (SOURCE_BOILERPLATE, _SOURCE_BOILERPLATE_PATTERNS),
    (STOCK_WORDING, _STOCK_PATTERNS),
    (SHIPPING_WORDING, _SHIPPING_PATTERNS),
)

# Sentence-level codes a deterministic repair may remove outright (the
# whole sentence carries no product fact). HTML_ENTITY/HTML_MARKUP are
# fixed by decoding, never by deletion; TRUNCATED cannot be fixed by
# deletion at all -- it needs the full source text.
REMOVABLE_SENTENCE_CODES = frozenset(
    {PROVENANCE_NOTE, INTERNAL_WORKFLOW, SOURCE_BOILERPLATE, STOCK_WORDING, SHIPPING_WORDING}
)

# A source description shorter than this, after normalization, is too
# thin to stand as a product description on its own. Length is only a
# floor -- truncation/boilerplate detection is the real defense.
MIN_USABLE_SOURCE_DESCRIPTION_LENGTH = 80

_MIN_KEPT_SENTENCE_WORDS = 6

_SENTENCE_SPLIT_RE =re.compile(r"(?<=[.!?…])\s+(?=[\"“'(\[]?[A-ZÀ-ỸĐ0-9])")


# --- quoted book excerpts -------------------------------------------
#
# Retailer pages often open with a verbatim passage from the book in
# quotation marks (AUTOIMPORT-CAN-0044: an explicit novel passage became
# the storefront short description). A passage is product content only as
# an author/context/plot summary (TSYC_CONTENT_GUIDE.md section 7), never
# as the summary itself. A quoted *title* ("“Gấu con đi ngủ” là ...") or a
# short quoted line is not an excerpt: the closing quote must come within
# _MAX_QUOTED_TITLE_LENGTH characters.

_OPENING_QUOTES = "“\"«„‘'"
_CLOSING_QUOTES = "”\"»“’'"
_MAX_QUOTED_TITLE_LENGTH = 80
# A leading quoted paragraph at least this long is an excerpt, not an
# epigraph-like one-line quote (which a long description may keep).
_MIN_LEADING_EXCERPT_PARAGRAPH_LENGTH = 200


def starts_with_quoted_excerpt(
    text: str | None,
    titles: Sequence[str | None] = (),
) -> bool:
    """True when text opens with a quotation that is not closed within
    _MAX_QUOTED_TITLE_LENGTH characters (a passage, not a title). A
    quotation that opens with one of `titles` (the product's own name,
    however long) is never an excerpt."""
    if not text:
        return False
    stripped = str(text).lstrip()
    if not stripped or stripped[0] not in _OPENING_QUOTES:
        return False
    quoted = stripped[1:].lower()
    if any(
        title and _normalize_paragraph(title) and quoted.startswith(_normalize_paragraph(title).lower())
        for title in titles
    ):
        return False
    window = stripped[1:_MAX_QUOTED_TITLE_LENGTH + 1]
    return not any(ch in _CLOSING_QUOTES for ch in window)


def _is_leading_excerpt_paragraph(paragraph: str) -> bool:
    stripped = paragraph.strip()
    return (
        len(stripped) >= _MIN_LEADING_EXCERPT_PARAGRAPH_LENGTH
        and starts_with_quoted_excerpt(stripped)
        and stripped.rstrip(".!?… ")[-1:] in _CLOSING_QUOTES
    )


def drop_leading_quoted_excerpts(text: str | None) -> str:
    """Remove leading paragraphs that are long quoted book passages; keep
    every other paragraph verbatim."""
    if not text:
        return ""
    paragraphs = normalize_paragraphs(text).split("\n\n")
    while paragraphs and _is_leading_excerpt_paragraph(paragraphs[0]):
        paragraphs.pop(0)
    return "\n\n".join(paragraphs)


def has_leading_quoted_excerpt(text: str | None) -> bool:
    """True when the description opens with a long quoted book passage
    that is followed by real description prose (so removing the passage
    is deterministic). An excerpt-only description is not flagged here --
    there is nothing to replace it with without inventing text; its
    summary fields are still flagged (SUMMARY_FIELDS)."""
    if not text:
        return False
    if not _is_leading_excerpt_paragraph(normalize_paragraphs(text).split("\n\n")[0]):
        return False
    return bool(drop_leading_quoted_excerpts(text).strip())


# --- normalization --------------------------------------------------


def decode_html_entities(text: str) -> str:
    """Decode HTML entities, including double-encoded ones (&amp;aacute;)."""
    decoded = text
    for _ in range(3):
        next_value = html.unescape(decoded)
        if next_value == decoded:
            break
        decoded = next_value
    return decoded


def _normalize_paragraph(paragraph: str) -> str:
    return " ".join(paragraph.replace("\xa0", " ").split()).strip()


def normalize_paragraphs(text: str | None) -> str:
    """Collapse whitespace inside paragraphs, keep blank-line breaks."""
    if not text:
        return ""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    paragraphs: list[str] = []
    for line in lines:
        cleaned = _normalize_paragraph(line)
        if cleaned:
            paragraphs.append(cleaned)
    return "\n\n".join(paragraphs)


def split_sentences(paragraph: str) -> list[str]:
    """Split one paragraph into sentences at terminal punctuation."""
    parts = _SENTENCE_SPLIT_RE.split(paragraph.strip())
    return [part.strip() for part in parts if part.strip()]


def _strip_duplicated_title_prefix(paragraph: str, titles: Sequence[str]) -> str:
    """
    Fahasa JSON-LD descriptions start with "<page title>, <title> <text>"
    (e.g. "Tư Duy Ngược, Tư Duy Ngược Chúng ta ..."). Remove that exact
    comma-led repetition only. A title that is the natural subject of a
    sentence ("Tư Duy Ngược là ...") is never touched.
    """
    stripped = paragraph.lstrip()
    for first in titles:
        first_norm = _normalize_paragraph(first)
        if not first_norm or not stripped.lower().startswith(first_norm.lower() + ","):
            continue
        remainder = stripped[len(first_norm) + 1:].lstrip()
        for second in titles:
            second_norm = _normalize_paragraph(second)
            if second_norm and remainder.lower().startswith(second_norm.lower() + " "):
                after = remainder[len(second_norm):].lstrip()
                if after[:1].isupper() or after[:1] in {"“", '"'}:
                    return after
        return remainder
    return paragraph


# Stock wording that appears as a trailing clause of an otherwise factual
# sentence ("..., 228 trang - có tại Tiệm Sách Yêu Con."): remove the
# clause only, keep the sentence.
_STOCK_CLAUSE_RES = (
    # "..., 228 trang - có tại Tiệm Sách Yêu Con." / "..., hiện có tại
    # Tiệm Sách Yêu Con, gồm ..." -- delimiter-led clause, end or mid.
    re.compile(r"\s*[-–—,]\s*(?:hiện\s+|đang\s+)?có\s+(?:bán\s+)?tại\s+Tiệm Sách Yêu Con(?=\s*(?:[.,!]|$))", re.IGNORECASE),
    # "... 6 cuốn hiện có tại Tiệm Sách Yêu Con, gồm ..." -- bare clause.
    re.compile(r"\s+(?:hiện|đang)\s+có\s+(?:bán\s+)?tại\s+Tiệm Sách Yêu Con(?=\s*(?:[.,!]|$))", re.IGNORECASE),
)


def strip_stock_clauses(sentence: str) -> str:
    """Remove a trailing stock clause from one sentence (see above)."""
    result = sentence
    for pattern in _STOCK_CLAUSE_RES:
        result = pattern.sub("", result)
    if result != sentence:
        result = result.rstrip(" ,-–—")
        if result and result[-1] not in ".!?…\"”":
            result += "."
    return result


def sentence_defects(sentence: str) -> set[str]:
    """Defect codes (pattern-based only) present in one sentence."""
    found: set[str] = set()
    for code, patterns in _PATTERN_GROUPS:
        if any(pattern.search(sentence) for pattern in patterns):
            found.add(code)
    return found


def remove_defective_sentences(
    text: str | None,
    codes: Iterable[str] = REMOVABLE_SENTENCE_CODES,
) -> str:
    """Drop every sentence that carries one of `codes`; keep everything
    else verbatim (paragraph structure preserved)."""
    if not text:
        return ""
    removable = set(codes)
    kept_paragraphs: list[str] = []
    for paragraph in normalize_paragraphs(text).split("\n\n"):
        kept: list[str] = []
        for sentence in split_sentences(paragraph):
            if not (sentence_defects(sentence) & removable):
                kept.append(sentence)
                continue
            if STOCK_WORDING in removable:
                # Keep the factual part of a sentence whose only defect
                # is a trailing stock clause -- but never a stub.
                trimmed = strip_stock_clauses(sentence)
                if (
                    trimmed != sentence
                    and len(trimmed.split()) >= _MIN_KEPT_SENTENCE_WORDS
                    and not (sentence_defects(trimmed) & removable)
                ):
                    kept.append(trimmed)
        if kept:
            kept_paragraphs.append(" ".join(kept))
    return "\n\n".join(kept_paragraphs)


def repair_typography(text: str | None) -> str:
    """Deterministic fix for TYPOGRAPHY_DEFECT_CODES only: collapse a
    doubled "" to ", drop the stray period in "?." / "!.", turn ",."
    into ".", and drop a glued footnote marker "[3]". Never adds,
    removes, or changes a word."""
    if not text:
        return ""
    repaired = _DOUBLED_QUOTE_RE.sub('"', str(text))
    repaired = _STRAY_PERIOD_AFTER_MARK_RE.sub(r"\1", repaired)
    repaired = _CITATION_MARKER_RE.sub("", repaired)
    return _COMMA_PERIOD_RE.sub(".", repaired)


def drop_cross_sell_tail(text: str | None) -> str:
    """
    Remove a retailer cross-sell paragraph ("Mời các bạn tìm mua trọn
    bộ:") together with the list of other titles after it -- but only
    when every following paragraph is a short list item (no longer than
    _TRAILING_CHROME_MAX_LENGTH). Otherwise only the cross-sell sentence
    itself is removed later (SOURCE_BOILERPLATE); real prose after it is
    never dropped.
    """
    if not text:
        return ""
    paragraphs = normalize_paragraphs(text).split("\n\n")
    for index, paragraph in enumerate(paragraphs):
        if _CROSS_SELL_RE.search(paragraph) and all(
            len(rest) <= _TRAILING_CHROME_MAX_LENGTH for rest in paragraphs[index + 1:]
        ):
            return "\n\n".join(paragraphs[:index])
    return "\n\n".join(paragraphs)


def find_duplicated_words(text: str | None) -> list[str]:
    """Advisory: adjacent repeated words, for operator review notes only
    (see _DUPLICATED_WORD_RE -- never a defect, never repaired)."""
    if not text:
        return []
    return [match.group(0) for match in _DUPLICATED_WORD_RE.finditer(str(text))]


def clean_source_text(text: str | None) -> str:
    """Decode entities, drop HTML tags, normalize whitespace (paragraph
    breaks kept). Removes nothing else -- this is what a collector stores
    as the faithful, readable record of what the source said."""
    if not text:
        return ""
    decoded = decode_html_entities(str(text))
    decoded = re.sub(r"(?i)<\s*br\s*/?>|</\s*p\s*>", "\n", decoded)
    decoded = _HTML_TAG_RE.sub(" ", decoded)
    return normalize_paragraphs(decoded)


# Product-page chrome that means a container captured more than the
# description: customer reviews, rating widgets, buy box. A source text
# containing any of these is rejected outright (never "cleaned"), because
# review prose cannot be told apart from product prose deterministically.
_PAGE_CHROME_PATTERNS = (
    re.compile(r"\b\d+\s+đánh giá\b", re.IGNORECASE),
    re.compile(r"gửi đánh giá", re.IGNORECASE),
    re.compile(r"viết đánh giá", re.IGNORECASE),
    re.compile(r"\btrả lời\s+\d+\s+(?:năm|tháng|tuần|ngày|giờ|phút)\s+trước", re.IGNORECASE),
    re.compile(r"\bchọn mua\b", re.IGNORECASE),
    re.compile(r"thêm vào giỏ", re.IGNORECASE),
    re.compile(r"\btiết kiệm:", re.IGNORECASE),
    re.compile(r"\d[\d.,]*\s*₫"),
)


def contains_page_chrome(text: str | None) -> bool:
    """True when extracted text includes reviews/rating/buy-box chrome."""
    if not text:
        return False
    return any(pattern.search(text) for pattern in _PAGE_CHROME_PATTERNS)


# Trailing page chrome after the description body ("Xem tất cả sách của
# tác giả X", "Xem thêm", a table-of-contents tail): short paragraphs that
# do not end a sentence. Trimmed from the end only.
_TRAILING_CHROME_MAX_LENGTH = 150
_SENTENCE_END_CHARACTERS = ".!?…\"”’»)"


def _trim_trailing_chrome(paragraphs: list[str]) -> list[str]:
    trimmed = list(paragraphs)
    while trimmed:
        last = trimmed[-1].rstrip()
        if last.endswith(":") or (
            len(last) <= _TRAILING_CHROME_MAX_LENGTH
            and last[-1:] not in _SENTENCE_END_CHARACTERS
        ):
            trimmed.pop()
            continue
        break
    return trimmed


# Section headings product pages put above the description body
# ("THÔNG TIN SẢN PHẨM", "Giới thiệu sách <title>", "Mô tả sản phẩm").
_HEADING_PARAGRAPH_RE = re.compile(
    r"^(?:thông tin sản phẩm|mô tả sản phẩm|giới thiệu sách|giới thiệu nội dung|nội dung sách)\b[^.!?]{0,150}$",
    re.IGNORECASE,
)


def normalize_source_description(
    text: str | None,
    titles: Sequence[str | None] = (),
) -> str:
    """
    Turn a collected source description into clean plain text:
    decode HTML entities, drop HTML tags, normalize whitespace (keeping
    paragraph breaks), remove a leading duplicated title, and drop
    retailer/stock/shipping boilerplate sentences.

    Never adds words. The result may still be TRUNCATED -- callers must
    check find_text_defects() before using it as storefront content.
    """
    if not text:
        return ""
    paragraphs = clean_source_text(text).split("\n\n")
    title_values = [
        _normalize_paragraph(decode_html_entities(value))
        for value in titles
        if value and _normalize_paragraph(value)
    ]
    title_keys = {value.lower() for value in title_values}

    cleaned: list[str] = []
    for paragraph in paragraphs:
        # Empty, or only punctuation/symbols ("…", "-", "+"): UI residue.
        if not paragraph or not re.search(r"\w", paragraph):
            continue
        # A paragraph that is exactly a title, or a section heading, is
        # page chrome, not content.
        if paragraph.lower() in title_keys or _HEADING_PARAGRAPH_RE.match(paragraph):
            continue
        if not cleaned:
            paragraph = _strip_duplicated_title_prefix(paragraph, title_values)
        if paragraph:
            cleaned.append(paragraph)

    cleaned = [p for p in drop_third_party_sections("\n\n".join(cleaned)).split("\n\n") if p]
    cleaned = [p for p in drop_cross_sell_tail("\n\n".join(cleaned)).split("\n\n") if p]
    cleaned = _trim_trailing_chrome(cleaned)
    # A leading verbatim book passage is not product description.
    cleaned = [p for p in drop_leading_quoted_excerpts("\n\n".join(cleaned)).split("\n\n") if p]

    without_boilerplate = remove_defective_sentences(
        "\n\n".join(cleaned),
        codes={SOURCE_BOILERPLATE, STOCK_WORDING, SHIPPING_WORDING},
    )
    # Removing boilerplate can expose more trailing chrome; trim again.
    remaining = [paragraph for paragraph in without_boilerplate.split("\n\n") if paragraph.strip()]
    return repair_typography("\n\n".join(_trim_trailing_chrome(remaining)).strip())


# --- detection ------------------------------------------------------


def find_text_defects(text: str | None) -> set[str]:
    """Every defect code present in one customer-facing text value."""
    if not text:
        return set()
    value = str(text)
    found: set[str] = set()
    if _HTML_ENTITY_RE.search(value):
        found.add(HTML_ENTITY)
    if _HTML_TAG_RE.search(value):
        found.add(HTML_MARKUP)
    if _TRUNCATION_RE.search(value):
        found.add(TRUNCATED)
    if _DOUBLED_QUOTE_RE.search(value):
        found.add(DOUBLED_QUOTE)
    if _MALFORMED_PUNCTUATION_RE.search(value):
        found.add(MALFORMED_PUNCTUATION)
    if _CITATION_MARKER_RE.search(value):
        found.add(CITATION_MARKER)
    if has_invalid_isbn(value):
        found.add(INVALID_ISBN)
    for code, patterns in _PATTERN_GROUPS:
        if any(pattern.search(value) for pattern in patterns):
            found.add(code)
    return found


# Customer-facing fields that are running prose and must end a sentence.
# Titles, SEO titles/descriptions, author lines and detail lists are not
# required to end with terminal punctuation.
PROSE_FIELDS = ("short_description", "long_description")

# Summary fields must never open with a quoted book passage.
SUMMARY_FIELDS = ("short_description", "seo_description")

# ":" and ";" are deliberately absent: prose that stops on a colon is the
# lead-in of a list that is not there ("Hãy sẳn sàng để:" was approved as
# a whole short_description, 2026-10-06 Fast Track Batch 20).
_TERMINAL_CHARACTERS = ".!?…\"”’»)]"
# German/French closing quotes („…“, »…«) that are also opening quotes
# elsewhere: terminal only right after sentence punctuation ("…lieb?“").
_CLOSING_QUOTE_AFTER_PUNCTUATION = "“«"


def ends_with_complete_sentence(text: str | None) -> bool:
    """True when prose ends with terminal punctuation. A source snippet cut
    at a character limit without an ellipsis ("...một cuộc chơi hoàn
    toàn") fails this -- it is extraction truncation, not a sentence."""
    if not text:
        return False
    stripped = text.rstrip()
    if stripped[-1:] in _CLOSING_QUOTE_AFTER_PUNCTUATION:
        return stripped[-2:-1] in ".!?…"
    return bool(stripped) and stripped[-1] in _TERMINAL_CHARACTERS


def find_content_defects(
    content: Mapping[str, object],
    fields: Sequence[str],
) -> dict[str, list[str]]:
    """{field: sorted defect codes} for every field with a defect."""
    findings: dict[str, list[str]] = {}
    for field in fields:
        value = content.get(field)
        defects = find_text_defects(value)  # type: ignore[arg-type]
        if field in PROSE_FIELDS and value and not ends_with_complete_sentence(str(value)):
            defects.add(TRUNCATED)
        if field in SUMMARY_FIELDS and starts_with_quoted_excerpt(
            value, titles=[content.get("product_name")]  # type: ignore[arg-type, list-item]
        ):
            defects.add(QUOTED_EXCERPT)
        if field == "long_description" and has_leading_quoted_excerpt(value):  # type: ignore[arg-type]
            defects.add(QUOTED_EXCERPT)
        if field == "long_description" and value and has_third_party_section(str(value)):
            defects.add(THIRD_PARTY_SECTION)
        if defects:
            findings[field] = sorted(defects)
    return findings


def is_usable_source_description(text: str | None) -> bool:
    """True when a normalized source description can stand as storefront
    prose: long enough, no defect of any kind, and not cut off (it must
    end a sentence)."""
    if not text:
        return False
    stripped = text.strip()
    if len(stripped) < MIN_USABLE_SOURCE_DESCRIPTION_LENGTH:
        return False
    if is_first_person_source(stripped) or has_third_party_section(stripped):
        return False
    if contains_page_chrome(stripped):
        return False
    if not ends_with_complete_sentence(stripped):
        return False
    return not find_text_defects(stripped)


def leading_sentences(text: str, max_length: int) -> str:
    """
    Whole leading sentences of the first prose paragraph (the first one
    that ends a sentence -- a subtitle/heading line is skipped), up to
    max_length. Never cuts inside a sentence and never adds an ellipsis:
    returns "" when even the first sentence is longer than max_length.
    """
    paragraphs = normalize_paragraphs(text).split("\n\n") if text else []
    first_paragraph = next(
        (
            paragraph
            for paragraph in paragraphs
            if ends_with_complete_sentence(paragraph)
            and not starts_with_quoted_excerpt(paragraph)
        ),
        "",
    )
    selected: list[str] = []
    length = 0
    for sentence in split_sentences(first_paragraph):
        extra = len(sentence) + (1 if selected else 0)
        if length + extra > max_length:
            break
        selected.append(sentence)
        length += extra
    # A summary must not end on an ellipsis sentence: as the end of a field
    # it reads as (and is detected as) extraction truncation.
    while selected and _TRUNCATION_RE.search(selected[-1]):
        selected.pop()
    return " ".join(selected)
