"""Multilingual content package (CLAUDE_AUTOMATION.md section 9.2).

A typed, JSON-serializable view of one candidate's customer-facing content
across vi/en/de. The Vietnamese side is taken ONLY from the APPROVED 'vi'
product_contents row (section 9.1: Vietnamese is the semantic source of
truth); en/de are localizations of it, never independent descriptions.

Pure: no I/O. Callers pass rows they already read. This module never
persists anything -- en/de rows are written only through
scripts/prepare_product_content.py's existing translation SAVE/APPROVE
path.

Field mapping to product_contents columns:

    product_title / product_title_{en,de}  -> product_name
    description_{vi,en,de}                 -> long_description
    short_description_{vi,en,de}           -> short_description

product_title_en/product_title_de and verified_facts/name_transliterations
are additions to the section 9.2 shape: a translated row needs its own
product_name, and the cross-language consistency validator
(src.domain.rules.multilingual_consistency) needs the verified facts the
translation may restate.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, replace
from typing import Any, Mapping

from src.domain.rules import lane_rules, translation_rules

VI_CONTENT_NOT_APPROVED = "VI_CONTENT_NOT_APPROVED"

# multilingual_status values (CLAUDE_AUTOMATION.md section 9.2 "Suggested
# states").
CONTENT_VI_READY = "CONTENT_VI_READY"
CONTENT_EN_READY = "CONTENT_EN_READY"
CONTENT_DE_READY = "CONTENT_DE_READY"
MULTILINGUAL_CONTENT_READY = "MULTILINGUAL_CONTENT_READY"

TRANSLATION_LANGUAGES = translation_rules.TRANSLATION_LANGUAGES

_APPROVED = "APPROVED"


class ContentPackageRefused(RuntimeError):
    """Candidate-specific refusal to build a package. Never a global stop:
    it only means this one candidate is not ready for localization."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


@dataclass(frozen=True)
class ContentPackage:
    candidate_code: str
    product_title: str
    sellable_unit: str | None
    description_vi: str
    short_description_vi: str
    content_source: str
    facts_used: list[str]
    content_status_vi: str
    description_en: str | None = None
    description_de: str | None = None
    short_description_en: str | None = None
    short_description_de: str | None = None
    product_title_en: str | None = None
    product_title_de: str | None = None
    content_status_en: str | None = None
    content_status_de: str | None = None
    multilingual_status: str = CONTENT_VI_READY
    verified_facts: dict[str, Any] = field(default_factory=dict)
    # Accepted alternative spellings of a verified name in en/de text
    # (e.g. a Vietnamese author name written without diacritics). Empty by
    # default: only an explicit, supplied mapping can relax name checks.
    name_transliterations: dict[str, list[str]] = field(default_factory=dict)

    def translation(self, language: str) -> dict[str, str | None]:
        """en/de side in product_contents column names."""
        _require_translation_language(language)
        return {
            "product_name": getattr(self, f"product_title_{language}"),
            "short_description": getattr(self, f"short_description_{language}"),
            "long_description": getattr(self, f"description_{language}"),
        }

    def vietnamese(self) -> dict[str, str]:
        return {
            "product_name": self.product_title,
            "short_description": self.short_description_vi,
            "long_description": self.description_vi,
        }

    def with_translation(
        self,
        language: str,
        *,
        product_title: str | None,
        short_description: str | None,
        description: str | None,
    ) -> "ContentPackage":
        _require_translation_language(language)
        return replace(
            self,
            **{
                f"product_title_{language}": product_title,
                f"short_description_{language}": short_description,
                f"description_{language}": description,
            },
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ContentPackage":
        known = set(cls.__dataclass_fields__)
        unknown = sorted(set(payload) - known)
        if unknown:
            raise ValueError(
                "Content package has unsupported field(s): " + ", ".join(unknown)
            )
        return cls(**dict(payload))


def _require_translation_language(language: str) -> None:
    if language not in TRANSLATION_LANGUAGES:
        raise ValueError(f"Unsupported translation language: {language!r}.")


def _row(contents: list[Mapping[str, Any]], language: str) -> Mapping[str, Any] | None:
    return next(
        (content for content in contents if content.get("content_language") == language),
        None,
    )


def multilingual_status(contents: list[Mapping[str, Any]]) -> str:
    """Derived from the same rows lane_rules/run_batch read, so the
    package, the lane classifier, and the Woo dispatch guard can never
    disagree about MULTILINGUAL_CONTENT_READY."""
    if lane_rules.has_multilingual_content(list(contents)):
        return MULTILINGUAL_CONTENT_READY

    approved = {
        content.get("content_language")
        for content in contents
        if content.get("content_status") == _APPROVED
    }
    if "en" in approved:
        return CONTENT_EN_READY
    if "de" in approved:
        return CONTENT_DE_READY
    return CONTENT_VI_READY


def build_content_package(
    *,
    candidate: Mapping[str, Any],
    product: Mapping[str, Any],
    contents: list[Mapping[str, Any]],
) -> ContentPackage:
    """
    Build the package for one candidate from rows already read.

    Refuses (ContentPackageRefused, code VI_CONTENT_NOT_APPROVED) when no
    'vi' row exists or it is not APPROVED with review_required=false --
    the same condition prepare_product_content.py's
    require_approved_vietnamese_content() enforces before any en/de write.
    """
    candidate_code = candidate.get("candidate_code") or ""
    vi_content = _row(contents, "vi")

    if vi_content is None:
        raise ContentPackageRefused(
            VI_CONTENT_NOT_APPROVED,
            f"{candidate_code} has no Vietnamese product_contents row.",
        )

    if (
        vi_content.get("content_status") != _APPROVED
        or vi_content.get("review_required") is not False
    ):
        raise ContentPackageRefused(
            VI_CONTENT_NOT_APPROVED,
            f"{candidate_code} Vietnamese content is "
            f"{vi_content.get('content_status')!r} (review_required="
            f"{vi_content.get('review_required')!r}); approve it first.",
        )

    en_row = _row(contents, "en") or {}
    de_row = _row(contents, "de") or {}

    return ContentPackage(
        candidate_code=candidate_code,
        product_title=vi_content.get("product_name") or "",
        sellable_unit=candidate.get("candidate_type"),
        description_vi=vi_content.get("long_description") or "",
        short_description_vi=vi_content.get("short_description") or "",
        content_source=(
            f"product_contents:{vi_content.get('product_content_id')} (vi APPROVED)"
        ),
        facts_used=translation_rules.facts_used(vi_content, product),
        content_status_vi=_APPROVED,
        description_en=en_row.get("long_description"),
        description_de=de_row.get("long_description"),
        short_description_en=en_row.get("short_description"),
        short_description_de=de_row.get("short_description"),
        product_title_en=en_row.get("product_name"),
        product_title_de=de_row.get("product_name"),
        content_status_en=en_row.get("content_status"),
        content_status_de=de_row.get("content_status"),
        multilingual_status=multilingual_status(contents),
        verified_facts=translation_rules.verified_fact_values(product),
    )
