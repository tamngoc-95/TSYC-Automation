"""OFFLINE parser for a personal Facebook data-export "posts, check-ins,
photos and videos" HTML file.

Scope -- READ THIS BEFORE CHANGING EITHER SIDE:

    This module turns one already-downloaded Facebook data-export HTML
    file (the kind a user gets from Facebook's own "Download Your
    Information" tool) into a list of plain HistoryRecord values -- one
    per historical post/photo/video/status entry, in the document's own
    order. It never opens a browser, never calls the Facebook API, never
    touches Supabase or WooCommerce, and never writes anything back to
    the source file. It performs local disk I/O only to read the export
    file the caller points it at.

    This is a distinct, earlier stage than scripts/collect_one_facebook_
    post.py / src/services/source_ingestion.py, which deal with *live*,
    already-known-URL Facebook group permalinks reached via an
    authenticated Playwright session. Nothing here ever produces a
    source_urls/raw_pages row -- see
    scripts/classify_facebook_history_export.py's own docstring for the
    full list of things the offline historical-migration screening layer
    this module feeds must never do.

Parsing approach:

    A Facebook data export is not valid XML and is too large/irregular
    for a full DOM parser to be worth the dependency here, so this module
    uses narrowly-scoped regexes matched against the raw HTML text,
    documented at each site with exactly what real markup shape they are
    built from (see the docstrings on parse_facebook_history_export() and
    its helpers). It is intentionally conservative: a shape it does not
    recognize degrades to an empty/absent field rather than raising or
    guessing.

    One quirk this module works around: Facebook's export duplicates a
    photo/video's own caption once per attached-media thumbnail *and*
    once again as the post's outer text block, so a naive whole-section
    text-strip repeats "Sách Có Sẵn tại Đức Sách Có Sẵn tại Đức Sách Có
    Sẵn tại Đức" for a 3-photo post. HistoryRecord.full_text instead
    extracts each leaf-level text block once, drops Facebook's own
    "Cập nhật <date>" boilerplate restatement, and collapses immediately
    -repeated blocks -- see _extract_full_text()'s docstring.
"""

from __future__ import annotations

import html
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

SECTION_MARKER = '<section class="_a6-g"'

_HEADING_RE = re.compile(r"<h2\b[^>]*>(.*?)</h2>", re.DOTALL)
_FOOTER_DATE_RE = re.compile(r'<div class="_a72d">(.*?)</div>', re.DOTALL)

# A "leaf" div: one whose content contains no nested <div ...> or </div>
# of its own. Real Facebook export captions are plain text plus <br/>
# line breaks and, for a shared-link post, one <a href="...">...</a> --
# never a nested block div -- so this reliably finds exactly one entry
# per real text block (a per-photo caption, a shared group's name, the
# post's own outer caption, or the "Cập nhật <date>" filler) without
# matching the surrounding media-grid wrapper divs, which do contain
# nested divs and are correctly skipped.
_LEAF_DIV_RE = re.compile(r"<div\b[^>]*>((?:(?!</?div\b)[\s\S])*?)</div>")

_BR_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RUN_RE = re.compile(r"[ \t\r\f\v]+")
_BLANK_LINES_RE = re.compile(r"\n{2,}")
_UPDATE_BOILERPLATE_RE = re.compile(r"^Cập nhật\b.*$", re.IGNORECASE | re.DOTALL)

# Facebook's own fixed UI label for a video-play placeholder ("Click to
# view video:"), not a user-authored caption -- dropped the same way the
# "Cập nhật <date>" restatement is, so a video post's full_text never
# starts with this boilerplate on every single video record.
_VIDEO_PLACEHOLDER_TEXT = "Nhấp để xem video:"

_IMG_SRC_RE = re.compile(r'<img\b[^>]+\bsrc="([^"]+)"')
_VIDEO_SRC_RE = re.compile(r'<video\b[^>]+\bsrc="([^"]+)"')
_ANCHOR_HREF_RE = re.compile(r'<a\b[^>]+\bhref="([^"]+)"')
_MEDIA_FOLDER_SLUG_RE = re.compile(r"posts/media/([A-Za-z0-9]+?)_\d+/")
_MENTION_RE = re.compile(r"&#0?64;\[(\d+):\d+:([^\]]+)\]")

TEXT_PREVIEW_LENGTH = 160


def _unescape_and_normalize(value: str) -> str:
    """HTML-unescape entities, NFC-normalize, and collapse whitespace
    runs (but keep already-present newlines) for one extracted text
    fragment."""
    unescaped = html.unescape(value)
    normalized = unicodedata.normalize("NFC", unescaped)
    return _WHITESPACE_RUN_RE.sub(" ", normalized)


def _strip_tags(value: str) -> str:
    return _TAG_RE.sub("", value)


def _dedupe_preserve_order(values: list[str]) -> tuple[str, ...]:
    seen: dict[str, None] = {}
    for value in values:
        seen.setdefault(value, None)
    return tuple(seen.keys())


def _extract_heading(section_html: str) -> tuple[str, int]:
    """Return (heading_text, position right after the </h2> close tag).

    The returned position is where body content starts; callers must
    slice from there so heading text can never leak into full_text/
    marker extraction (project requirement: headings are never business
    relevance evidence)."""
    match = _HEADING_RE.search(section_html)

    if not match:
        return "", 0

    heading = _unescape_and_normalize(_strip_tags(match.group(1))).strip()
    return heading, match.end()


def _extract_footer_date(footer_html: str) -> str:
    match = _FOOTER_DATE_RE.search(footer_html)

    if not match:
        return ""

    return _unescape_and_normalize(_strip_tags(match.group(1))).strip()


def _extract_full_text(body_html: str) -> str:
    """Reconstruct the post's own text, once, in document order.

    Every leaf <div> in the body is a candidate text block (a per-photo
    caption, a shared group's name, the post's own outer caption, or
    Facebook's "Cập nhật <date>" filler). This function:

      1. Converts <br/> to a real newline and strips any remaining tags
         (e.g. the <a> wrapper around a shared external link) from each
         leaf block, keeping the link's own visible text.
      2. Replaces each structural "&#064;[id:offset:Name]" @mention
         token with its plain display Name, so a mentioned Page/person
         reads as ordinary text instead of leaking a raw Facebook object
         id into human-reviewed output (mention ids/names are still
         separately available via HistoryRecord.mention_ids/
         mention_names for structural matching).
      3. Drops blocks that are empty, that are exactly Facebook's own
         "Cập nhật <date>" boilerplate restatement, or that are exactly
         the fixed video-placeholder label (none is real content).
      4. Collapses a block that is identical to the block immediately
         before it -- this is what removes the "same caption N times"
         artifact from an N-photo post without ever discarding a
         genuinely different second block (e.g. a photo album's caption
         followed by a distinct real post caption both survive, in
         order, exactly once each).
    """
    body_html = _MENTION_RE.sub(lambda match: match.group(2), body_html)
    blocks: list[str] = []

    for match in _LEAF_DIV_RE.finditer(body_html):
        raw_block = match.group(1)
        with_newlines = _BR_RE.sub("\n", raw_block)
        text_only = _strip_tags(with_newlines)
        cleaned = _unescape_and_normalize(text_only).strip()

        if not cleaned:
            continue

        if _UPDATE_BOILERPLATE_RE.match(cleaned):
            continue

        if cleaned == _VIDEO_PLACEHOLDER_TEXT:
            continue

        if blocks and blocks[-1] == cleaned:
            continue

        blocks.append(cleaned)

    return _BLANK_LINES_RE.sub("\n", "\n".join(blocks)).strip()


def _build_text_preview(full_text: str, heading: str) -> str:
    """First TEXT_PREVIEW_LENGTH characters for human review, collapsed
    to one line. Falls back to the heading only for display purposes
    when there is no real caption -- classify() itself never sees or
    uses the heading (see facebook_history_classification.classify()'s
    docstring)."""
    source = full_text or heading
    single_line = " ".join(source.split())
    return single_line[:TEXT_PREVIEW_LENGTH]


def _extract_local_and_external(body_html: str) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """Return (local_image_paths, local_video_paths, external_links),
    each de-duplicated in first-seen order."""
    image_srcs = _IMG_SRC_RE.findall(body_html)
    video_srcs = _VIDEO_SRC_RE.findall(body_html)
    anchor_hrefs = _ANCHOR_HREF_RE.findall(body_html)

    local_images = [src for src in image_srcs if not src.lower().startswith("http")]
    local_videos = [src for src in video_srcs if not src.lower().startswith("http")]
    external_links = [
        href for href in anchor_hrefs if href.lower().startswith(("http://", "https://"))
    ]

    return (
        _dedupe_preserve_order(local_images),
        _dedupe_preserve_order(local_videos),
        _dedupe_preserve_order(external_links),
    )


def _extract_folder_slugs(paths: tuple[str, ...]) -> tuple[str, ...]:
    slugs: list[str] = []
    for path in paths:
        slugs.extend(_MEDIA_FOLDER_SLUG_RE.findall(path))
    return _dedupe_preserve_order(slugs)


def _extract_mentions(section_html: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    pairs = _MENTION_RE.findall(section_html)
    mention_ids = _dedupe_preserve_order([pair[0] for pair in pairs])
    mention_names = _dedupe_preserve_order(
        [_unescape_and_normalize(pair[1]).strip() for pair in pairs]
    )
    return mention_ids, mention_names


@dataclass(frozen=True)
class HistoryRecord:
    """One parsed historical Facebook record, exactly as it appears in
    the export -- no classification, no interpretation. See
    src.domain.rules.facebook_history_classification.classify() for the
    classification stage that consumes full_text/folder_slugs/
    mention_ids from this record.

    record_index is the record's stable 1-based position in the export's
    own document order -- stable across repeated parses of the same
    unmodified file (this module performs no sorting, filtering, or
    randomization), so it is safe to use as a durable local reference
    when reviewing output before any database row exists for a record.
    """

    record_index: int
    date_text: str
    heading: str
    full_text: str
    text_preview: str
    external_links: tuple[str, ...] = field(default_factory=tuple)
    local_image_paths: tuple[str, ...] = field(default_factory=tuple)
    local_video_paths: tuple[str, ...] = field(default_factory=tuple)
    folder_slugs: tuple[str, ...] = field(default_factory=tuple)
    mention_ids: tuple[str, ...] = field(default_factory=tuple)
    mention_names: tuple[str, ...] = field(default_factory=tuple)

    @property
    def media_count(self) -> int:
        return len(self.local_image_paths) + len(self.local_video_paths)


def _parse_one_section(section_html: str, record_index: int) -> HistoryRecord:
    heading, body_start = _extract_heading(section_html)

    footer_index = section_html.find("<footer")
    body_html = section_html[body_start:footer_index] if footer_index != -1 else section_html[body_start:]
    footer_html = section_html[footer_index:] if footer_index != -1 else ""

    date_text = _extract_footer_date(footer_html)
    full_text = _extract_full_text(body_html)
    text_preview = _build_text_preview(full_text, heading)

    local_images, local_videos, external_links = _extract_local_and_external(body_html)
    folder_slugs = _extract_folder_slugs(local_images + local_videos)
    mention_ids, mention_names = _extract_mentions(section_html)

    return HistoryRecord(
        record_index=record_index,
        date_text=date_text,
        heading=heading,
        full_text=full_text,
        text_preview=text_preview,
        external_links=external_links,
        local_image_paths=local_images,
        local_video_paths=local_videos,
        folder_slugs=folder_slugs,
        mention_ids=mention_ids,
        mention_names=mention_names,
    )


def parse_facebook_history_export(html_text: str) -> list[HistoryRecord]:
    """Parse a full export HTML document's text into one HistoryRecord
    per <section class="_a6-g"> record, in document order.

    Pure function: no I/O. Calling this twice with the same html_text
    always returns an equal list of records (see
    tests/test_facebook_history_parser.py's idempotency test).
    """
    raw_sections = html_text.split(SECTION_MARKER)[1:]

    return [
        _parse_one_section(section_html, record_index)
        for record_index, section_html in enumerate(raw_sections, start=1)
    ]


def load_facebook_history_export(path: str | Path) -> list[HistoryRecord]:
    """Read one Facebook export HTML file from local disk and parse it.

    The only I/O in this module. Read-only: the source file is never
    modified. Raises FileNotFoundError/OSError as normal for a missing
    or unreadable path -- never silently returns an empty list for a
    bad path."""
    file_path = Path(path)
    html_text = file_path.read_text(encoding="utf-8")
    return parse_facebook_history_export(html_text)
