"""Unified catalogue identity index for new-book discovery (no I/O).

Answers one question for an externally discovered listing (e.g. a new
Fahasa product page): is this book already represented anywhere TSYC
sells or has sold it -- WooCommerce (every status, including trash and
owner-removed products), the shop's Facebook posts, internal products,
candidates or registered references?

Identity is compared on verified ISBN (978/979 only, CLAUDE.md 2.3),
normalized title with the edition suffix removed (CLAUDE.md 9.3),
volume number, author and sellable unit -- never on SKU alone, because
a TSYC SKU is derived from the candidate code, not from the book.

Classification (exactly one per listing):

  EXISTING_PRODUCT          certain match to a live representation
  REMOTE_REMOVED            certain match only to a Woo product the owner
                            deleted/trashed -- terminal, never recreate
  DISTINCT_EDITION          same title/volume, but both sides carry a
                            different valid ISBN or a different format
  POSSIBLE_DUPLICATE        similar/containing title, a title-only
                            Facebook mention, or an author disagreement
  FACEBOOK_COVERAGE_UNKNOWN no catalogue match, but no Facebook data was
                            available to check against
  NEW_CONFIRMED             no match anywhere that was checked

Only NEW_CONFIRMED may enter a new-product workflow; everything else is
isolated with its evidence preserved. The classifier is deliberately
conservative: any doubt lands in POSSIBLE_DUPLICATE, never in
NEW_CONFIRMED.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Iterable, Sequence

from src.domain.rules import author_rules, identity_rules

INDEX_RULES_VERSION = "1.0.0"

NEW_CONFIRMED = "NEW_CONFIRMED"
EXISTING_PRODUCT = "EXISTING_PRODUCT"
POSSIBLE_DUPLICATE = "POSSIBLE_DUPLICATE"
DISTINCT_EDITION = "DISTINCT_EDITION"
FACEBOOK_COVERAGE_UNKNOWN = "FACEBOOK_COVERAGE_UNKNOWN"
REMOTE_REMOVED = "REMOTE_REMOVED"

CLASSIFICATIONS = (
    NEW_CONFIRMED,
    EXISTING_PRODUCT,
    POSSIBLE_DUPLICATE,
    DISTINCT_EDITION,
    FACEBOOK_COVERAGE_UNKNOWN,
    REMOTE_REMOVED,
)

# Index entry origins.
ORIGIN_WOO = "WOOCOMMERCE"
ORIGIN_WOO_SYNC = "WOO_SYNC_RECORD"
ORIGIN_INTERNAL_PRODUCT = "INTERNAL_PRODUCT"
ORIGIN_CANDIDATE = "CANDIDATE"
ORIGIN_REFERENCE = "PRODUCT_REFERENCE"
ORIGIN_FACEBOOK_POST = "FACEBOOK_POST"

# Link preference when several certain matches exist: the live shop
# product first, then the local records that lead to it.
_ORIGIN_RANK = {
    ORIGIN_WOO: 0,
    ORIGIN_WOO_SYNC: 1,
    ORIGIN_INTERNAL_PRODUCT: 2,
    ORIGIN_CANDIDATE: 3,
    ORIGIN_REFERENCE: 4,
    ORIGIN_FACEBOOK_POST: 5,
}

# Per-entry match strengths.
MATCH_CERTAIN = "CERTAIN"
MATCH_EDITION = "DISTINCT_EDITION"
MATCH_POSSIBLE = "POSSIBLE"

_SIMILAR_TITLE_THRESHOLD = 0.88
# A containment match needs a substantial shorter title, otherwise
# "Hoàng Tử Bé" would collide with every title that contains it.
_MIN_CONTAINED_TOKENS = 3
# A Facebook post is free text: an exact phrase hit is only treated as a
# certain match for a long, specific title in a book-relevant post.
_FACEBOOK_CERTAIN_MIN_TOKENS = 4
_FACEBOOK_MIN_PHRASE_TOKENS = 2
_FACEBOOK_BOOK_RELEVANCE = frozenset({"HIGH", "MEDIUM"})

_EDITION_SUFFIX_RE = re.compile(
    r"\s*[\(\[](?:tái bản|tb|bản|phiên bản|tái bản lần)[^\)\]]*[\)\]]\s*",
    re.IGNORECASE,
)
_FORMAT_PATTERNS = (
    ("HARDCOVER", re.compile(r"\bbìa cứng\b|\bbia cung\b|\bhardcover\b", re.IGNORECASE)),
    ("BILINGUAL", re.compile(r"\bsong ngữ\b|\bsong ngu\b|\bbilingual\b", re.IGNORECASE)),
    ("SPECIAL_EDITION", re.compile(r"\bbản đặc biệt\b|\bban dac biet\b|\bspecial edition\b", re.IGNORECASE)),
)
_FORMAT_SEGMENT_RE = re.compile(
    r"\s*[-–—]\s*(?:bìa cứng|bìa mềm|bia cung|bia mem|song ngữ[^-–—]*|bản đặc biệt)\s*$",
    re.IGNORECASE,
)


def _nfc(value: str | None) -> str:
    return unicodedata.normalize("NFC", str(value or "")).strip()


def core_title(title: str | None) -> str:
    """Title with edition suffixes and trailing format segments removed
    (diacritics kept) -- edition is not identity, CLAUDE.md 9.3."""
    text = _EDITION_SUFFIX_RE.sub(" ", _nfc(title))
    previous = None
    while previous != text:
        previous = text
        text = _FORMAT_SEGMENT_RE.sub("", text).strip()
    return " ".join(text.split())


def folded_title_key(title: str | None) -> str:
    """Diacritic-folded comparison key of the core title."""
    return identity_rules.normalize_text(core_title(title))


def format_markers(title: str | None) -> frozenset[str]:
    text = _nfc(title)
    return frozenset(name for name, pattern in _FORMAT_PATTERNS if pattern.search(text))


def _author_keys(author: str | None) -> frozenset[str]:
    return frozenset(
        author_rules.person_name_key(name)
        for name in author_rules.split_person_names(author)
        if identity_rules.is_specific_author(name)
    )


def _valid_isbn(value: str | None) -> str | None:
    if value and identity_rules.looks_like_valid_isbn(value):
        return identity_rules.normalize_isbn(value)
    return None


@dataclass(frozen=True)
class IndexEntry:
    """One existing representation of a book (or of a book mention)."""

    origin: str
    ref: str
    title: str
    isbn: str | None = None
    author: str | None = None
    remote_removed: bool = False
    status: str | None = None
    url: str | None = None
    relevance: str | None = None

    @property
    def title_key(self) -> str:
        return folded_title_key(self.title)


@dataclass(frozen=True)
class Listing:
    """One discovered external listing to classify."""

    title: str
    isbn: str | None = None
    author: str | None = None
    publisher: str | None = None
    url: str | None = None


@dataclass(frozen=True)
class EntryMatch:
    strength: str
    reason: str
    entry: IndexEntry


@dataclass(frozen=True)
class Classification:
    classification: str
    reason: str
    linked: IndexEntry | None = None
    matches: tuple[EntryMatch, ...] = ()
    warnings: tuple[str, ...] = field(default_factory=tuple)

    def evidence(self) -> list[dict[str, object]]:
        return [
            {
                "strength": match.strength,
                "reason": match.reason,
                "origin": match.entry.origin,
                "ref": match.entry.ref,
                "title": match.entry.title,
                "isbn": match.entry.isbn,
                "status": match.entry.status,
                "remote_removed": match.entry.remote_removed,
                "url": match.entry.url,
            }
            for match in self.matches
        ]


def _titles_contain(first: str, second: str) -> bool:
    shorter, longer = sorted((first, second), key=len)
    if len(shorter.split()) < _MIN_CONTAINED_TOKENS:
        return False
    return f" {shorter} " in f" {longer} "


_SEGMENT_SEPARATOR_RE = re.compile(r"\s+[-–—:]\s+")
_VOLUME_SEGMENT_RE = re.compile(r"^(?:tập|tap|vol\.?|volume|quyển|quyen|phần|phan|book|band)\s*\d{1,3}$", re.IGNORECASE)


def series_keys(title: str | None) -> tuple[str, ...]:
    """Folded series segments of a "<series> - [<series> -] [Tập N -] <title>"
    listing: every segment before the volume segment (or, without a volume,
    before the last segment), at least _MIN_CONTAINED_TOKENS tokens long.

    A shop that already sells a series may sell a new volume inside an
    existing set or as a variation -- a sellable-unit question, never
    silently "new" (production finding 2026-10-09: Fahasa "Nhật Ký Chú Bé
    Nhút Nhát - Tập 21" vs Woo "Nhật Ký Chú Bé Nhút Nhát (Song ngữ Anh –
    Việt)"; "Miu Miu Tự Lập - Tập 20" vs Woo "Miu Miu Tự Lập 1")."""
    segments = [segment.strip() for segment in _SEGMENT_SEPARATOR_RE.split(core_title(title)) if segment.strip()]
    if len(segments) < 2:
        return ()
    volume_index = next(
        (index for index, segment in enumerate(segments) if _VOLUME_SEGMENT_RE.match(segment)),
        None,
    )
    series = segments[:volume_index] if volume_index is not None else segments[:-1]
    keys = []
    for segment in series:
        key = identity_rules.normalize_text(segment)
        if len(key.split()) >= _MIN_CONTAINED_TOKENS and key not in keys:
            keys.append(key)
    return tuple(keys)


def _series_overlap(listing_title: str, other_key: str) -> str | None:
    padded = f" {other_key} "
    for key in series_keys(listing_title):
        if f" {key} " in padded:
            return key
    return None


def match_entry(listing: Listing, entry: IndexEntry) -> EntryMatch | None:
    """How strongly one structured index entry represents the listing."""
    listing_isbn = _valid_isbn(listing.isbn)
    entry_isbn = _valid_isbn(entry.isbn)

    if listing_isbn and entry_isbn and listing_isbn == entry_isbn:
        return EntryMatch(MATCH_CERTAIN, "Equal valid ISBN.", entry)

    listing_key = folded_title_key(listing.title)
    entry_key = entry.title_key
    if not listing_key or not entry_key:
        return None

    listing_volumes = identity_rules.volume_numbers(listing.title)
    entry_volumes = identity_rules.volume_numbers(entry.title)
    if listing_volumes and entry_volumes and listing_volumes != entry_volumes:
        # A different volume is a different book -- but the series is
        # already represented, so the sellable unit needs a decision.
        series = _series_overlap(listing.title, entry_key)
        if series:
            return EntryMatch(MATCH_POSSIBLE, f"Different volume of a series already represented ({series}).", entry)
        return None

    if listing_key == entry_key:
        if listing_isbn and entry_isbn:
            return EntryMatch(MATCH_EDITION, "Same title, different valid ISBN.", entry)
        if identity_rules.is_multi_volume_listing(listing.title) != identity_rules.is_multi_volume_listing(entry.title):
            return EntryMatch(MATCH_POSSIBLE, "Same title, but combo/set vs single-volume unit differs.", entry)
        listing_formats, entry_formats = format_markers(listing.title), format_markers(entry.title)
        if listing_formats != entry_formats and listing_formats and entry_formats:
            return EntryMatch(MATCH_EDITION, "Same title, different stated format.", entry)
        listing_authors, entry_authors = _author_keys(listing.author), _author_keys(entry.author)
        if listing_authors and entry_authors and not listing_authors & entry_authors:
            return EntryMatch(MATCH_POSSIBLE, "Same title, different author.", entry)
        if listing_formats != entry_formats:
            return EntryMatch(MATCH_POSSIBLE, "Same title; format marker present on one side only.", entry)
        return EntryMatch(MATCH_CERTAIN, "Same normalized title (edition suffix ignored).", entry)

    if _titles_contain(listing_key, entry_key):
        return EntryMatch(MATCH_POSSIBLE, "One title contains the other.", entry)

    if SequenceMatcher(None, listing_key, entry_key).ratio() >= _SIMILAR_TITLE_THRESHOLD:
        return EntryMatch(MATCH_POSSIBLE, "Highly similar title.", entry)

    series = _series_overlap(listing.title, entry_key)
    if series:
        return EntryMatch(MATCH_POSSIBLE, f"Series already represented ({series}).", entry)

    return None


def match_facebook_post(listing: Listing, post: IndexEntry) -> EntryMatch | None:
    """A Facebook post is free text: look for the listing's core title
    (or its ISBN) as a phrase inside the post text (``post.title``)."""
    listing_isbn = _valid_isbn(listing.isbn)
    if listing_isbn and listing_isbn in re.sub(r"[^0-9Xx]", "", post.title or "").upper():
        return EntryMatch(MATCH_CERTAIN, "ISBN appears in the Facebook post.", post)

    listing_key = folded_title_key(listing.title)
    tokens = listing_key.split()
    if len(tokens) < _FACEBOOK_MIN_PHRASE_TOKENS:
        return None
    post_key = identity_rules.normalize_text(post.title)
    post_text = f" {post_key} "
    if f" {listing_key} " not in post_text:
        series = _series_overlap(listing.title, post_key)
        if series:
            return EntryMatch(MATCH_POSSIBLE, f"Series mentioned in a Facebook post ({series}).", post)
        return None
    if len(tokens) >= _FACEBOOK_CERTAIN_MIN_TOKENS and post.relevance in _FACEBOOK_BOOK_RELEVANCE:
        return EntryMatch(MATCH_CERTAIN, "Exact title phrase in a book-relevant Facebook post.", post)
    return EntryMatch(MATCH_POSSIBLE, "Title phrase appears in a Facebook post (short title or low-relevance post).", post)


def _best_link(matches: Sequence[EntryMatch]) -> IndexEntry:
    return sorted(matches, key=lambda match: _ORIGIN_RANK.get(match.entry.origin, 99))[0].entry


def classify_listing(
    listing: Listing,
    entries: Iterable[IndexEntry],
    facebook_posts: Iterable[IndexEntry] | None,
    facebook_coverage_note: str | None = None,
) -> Classification:
    """Classify one listing against the whole index.

    ``facebook_posts=None`` means Facebook data was unavailable (not
    "no posts"): a listing with no other match is then
    FACEBOOK_COVERAGE_UNKNOWN instead of NEW_CONFIRMED.
    """
    if not folded_title_key(listing.title):
        return Classification(POSSIBLE_DUPLICATE, "Listing has no usable title; cannot rule out a duplicate.")

    matches: list[EntryMatch] = []
    for entry in entries:
        match = match_entry(listing, entry)
        if match is not None:
            matches.append(match)
    for post in facebook_posts or ():
        match = match_facebook_post(listing, post)
        if match is not None:
            matches.append(match)

    certain = [match for match in matches if match.strength == MATCH_CERTAIN]
    live_certain = [match for match in certain if not match.entry.remote_removed]
    removed_certain = [match for match in certain if match.entry.remote_removed]
    editions = [match for match in matches if match.strength == MATCH_EDITION]
    possible = [match for match in matches if match.strength == MATCH_POSSIBLE]
    ordered = tuple(certain + editions + possible)

    if removed_certain and not any(m.entry.origin in (ORIGIN_WOO, ORIGIN_WOO_SYNC) for m in live_certain):
        return Classification(
            REMOTE_REMOVED,
            "Matches a WooCommerce product the owner removed; never recreated automatically.",
            _best_link(removed_certain),
            ordered,
        )
    if live_certain:
        link = _best_link(live_certain)
        return Classification(
            EXISTING_PRODUCT,
            f"Already represented ({link.origin} {link.ref}): {live_certain[0].reason}",
            link,
            ordered,
        )
    if editions:
        return Classification(
            DISTINCT_EDITION,
            "Same title as an existing product but a different edition; needs a business decision.",
            _best_link(editions),
            ordered,
        )
    if possible:
        return Classification(
            POSSIBLE_DUPLICATE,
            f"Possible duplicate: {possible[0].reason}",
            _best_link(possible),
            ordered,
        )
    if facebook_posts is None:
        return Classification(
            FACEBOOK_COVERAGE_UNKNOWN,
            "No catalogue match, but Facebook coverage could not be checked.",
        )
    warnings = (facebook_coverage_note,) if facebook_coverage_note else ()
    return Classification(NEW_CONFIRMED, "No match in any checked source.", warnings=warnings)


# --- listing identity strength ---------------------------------------------

IDENTITY_STRONG = "STRONG"
IDENTITY_MODERATE = "MODERATE"
IDENTITY_WEAK = "WEAK"


def listing_identity_strength(listing: Listing) -> str:
    """STRONG: title + valid ISBN. MODERATE: title + specific author +
    publisher. WEAK: anything less (never auto-progressed)."""
    if not core_title(listing.title):
        return IDENTITY_WEAK
    if _valid_isbn(listing.isbn):
        return IDENTITY_STRONG
    if _author_keys(listing.author) and identity_rules.normalize_publisher(listing.publisher):
        return IDENTITY_MODERATE
    return IDENTITY_WEAK


def listing_sellable_unit(listing: Listing) -> str:
    """BOOK_SET for a combo/set/box listing, otherwise SINGLE_BOOK."""
    return "BOOK_SET" if identity_rules.is_multi_volume_listing(listing.title) else "SINGLE_BOOK"
