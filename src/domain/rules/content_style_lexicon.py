"""Editorial reference lexicon for TSYC storefront descriptions (VI/EN/DE).

Version-controlled style evidence, distilled from
docs/content_style/TSYC_CONTENT_GUIDE.md (sections 3/4/7a/13/14), the one
owner-approved example (docs/content_style/examples/dung-dinh-trang-di.md)
and the recurring defects found in the stored descriptions on 2026-10-17
(the FAHASA-2026-001 batch, the generic safe drafts, retailer blurbs):

  - metadata-template openings ("X là cuốn sách ... của tác giả Y, do Nhà
    xuất bản Z ấn hành") that repeat the structured author/publisher fields;
  - stock closings ("Đây là cuốn sách phù hợp cho ...", "món quà ý nghĩa");
  - generic filler that fits any book ("thế giới lung linh", "điều kỳ
    diệu", "đưa bé bước vào");
  - educational-benefit claims copied from retailer marketing ("giúp bé
    phát huy trí tưởng tượng", "rèn luyện khả năng quan sát").

It is style evidence only -- never a source of facts -- and only
owner-approved text may extend it (docs/content_style/examples/README.md).
Unreviewed generated text is never added here. Pure data + tiny helpers.
"""
from __future__ import annotations

import re

LEXICON_VERSION = "tsyc-editorial-lexicon/1 (2026-10-17)"

# Phrases that fit any book and carry no information about this one.
GENERIC_FILLER = {
    "vi": (
        "thế giới lung linh",
        "thế giới đầy màu sắc",
        "điều kỳ diệu",
        "những điều kì diệu",
        "đưa bé bước vào",
        "đưa bé vào thế giới",
        "mở ra cả một thế giới",
        "cuốn sách thú vị",
        "cuốn sách bổ ích",
        "vô vàn điều thú vị",
        "đầy bất ngờ",
        "không thể bỏ qua",
        "qua từng trang sách",
        "trên từng trang sách",
        "lôi cuốn từ trang đầu",
        "sinh động và hấp dẫn",
    ),
    "en": (
        "a world of wonder",
        "magical world",
        "colourful world",
        "colorful world",
        "page after page",
        "full of surprises",
        "a must-read",
        "an exciting book",
        "a delightful book",
    ),
    "de": (
        "eine zauberhafte welt",
        "wunderbare welt",
        "farbenfrohe welt",
        "seite für seite",
        "voller überraschungen",
        "ein spannendes buch",
        "ein muss",
    ),
}

# Closing clichés: the guide allows at most one reader-suitability ending;
# these stock forms read as templates when every product ends with them.
CLOSING_CLICHES = {
    "vi": ("đây là cuốn sách phù hợp cho", "món quà ý nghĩa", "món quà nhỏ xinh", "món quà tuyệt vời"),
    "en": ("this is the perfect book for", "a perfect gift", "a meaningful gift", "a lovely gift"),
    "de": ("ein ideales geschenk", "ein schönes geschenk", "ein bedeutungsvolles geschenk", "ein hübsches kleines geschenk"),
}

# Educational/developmental benefit claims. Allowed only when a verified
# source supports a benefit (evidence item with supports_benefit=true).
BENEFIT_CLAIMS = {
    "vi": (
        "giúp bé phát triển",
        "giúp trẻ phát triển",
        "phát huy trí tưởng tượng",
        "rèn luyện khả năng",
        "rèn luyện kỹ năng",
        "rèn kỹ năng",
        "nâng cao khả năng",
        "phát triển tư duy",
        "phát triển trí nhớ",
        "kích thích khả năng",
        "phát triển kỹ năng",
        "khéo léo của đôi tay",
        "tăng cường khả năng",
    ),
    "en": ("helps children develop", "develop their", "boost", "improve their", "train their", "builds skills"),
    "de": ("fördert", "schult", "trainiert", "entwickelt die", "stärkt die"),
}

# Words that address the reader / name who the book is for.
READER_CUES = {
    "vi": ("bé", "các em", "trẻ", "cha mẹ", "phụ huynh", "người đọc", "bạn đọc", "độc giả", "đọc cùng", "cả nhà", "gia đình"),
    "en": ("children", "child", "young readers", "readers", "parents", "families", "read aloud", "reading together"),
    "de": ("kinder", "kind", "leser", "eltern", "familien", "vorlesen", "gemeinsam"),
}

# A first sentence that restates structured metadata instead of the book:
# "<title> là cuốn sách ... của tác giả X, do Nhà xuất bản Y ấn hành".
METADATA_OPENING_PATTERNS = {
    "vi": re.compile(r"(?:của tác giả|do nhà xuất bản|nxb\b|ấn hành)", re.IGNORECASE),
    "en": re.compile(r"(?:\bby author\b|published by|\bpublisher\b)", re.IGNORECASE),
    "de": re.compile(r"(?:erschienen bei|verlag\b|herausgegeben von)", re.IGNORECASE),
}

# Function words ignored when comparing text with sources.
STOPWORDS = {
    "vi": frozenset(
        "và của là có cho các những một được với trong khi này đó từ như cũng để thì mà lại đã sẽ đang "
        "rất còn nhưng hay hoặc vào ra lên xuống người cuốn sách bé em con ta cô cậu".split()
    ),
    "en": frozenset("the a an and of to in for with is are was on as by it its this that from at be their".split()),
    "de": frozenset("der die das und ein eine einer einem des den dem zu mit von im in für ist sind auf als wie es sich".split()),
}


# Capitalized words that are common Vietnamese proper nouns rather than
# product facts (countries, festivals, month names after "tháng").
COMMON_PROPER_NOUNS_VI = frozenset(
    "việt nam đức châu âu tết trung thu giêng hai ba tư năm sáu bảy tám chín mười chạp".split()
)


def phrases_in(text: str, phrases: tuple[str, ...]) -> list[str]:
    """Every listed phrase occurring in text (case-insensitive), with repeats."""
    lowered = text.lower()
    found: list[str] = []
    for phrase in phrases:
        found.extend([phrase] * lowered.count(phrase))
    return found
