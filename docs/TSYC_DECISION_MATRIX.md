# TSYC Decision Matrix

This is the canonical, human-readable specification of the TSYC
deterministic decision engine (`src/domain/rules/`). Every production
rule listed here has a matching implementation and at least one offline
test (`tests/test_identity_rules.py`, `tests/test_image_rules.py`,
`tests/test_content_rules.py`, `tests/test_readiness_rules.py`).

**Outcomes** (`src/domain/decisions.py`, `Outcome`):

- **AUTO_PASS** — deterministic evidence supports proceeding automatically.
- **AUTO_REJECT** — deterministic evidence supports a confident negative
  (not ambiguous — a confirmed "no").
- **REVIEW_REQUIRED** — evidence is ambiguous, conflicting, or insufficient;
  a human must decide (CLAUDE.md section 5.2).
- **BLOCKED** — a structural precondition is unmet (e.g. a linked row was
  not found); not a business judgment call.

If a rule's `INPUT CONDITION` is not met, the row's `AUTO ACTION` does
not apply — read each domain's rows top-to-bottom as priority-ordered
where noted.

---

## Identity (`src/domain/rules/identity_rules.py`)

Covers `product_candidates.identity_status` and `product_references.
match_decision`. Reference-source priority must come from
`src/domain/reference_sources.py`, never a locally invented order.
**893-prefixed identifiers are never treated as ISBNs** (CLAUDE.md
section 2.3) — `looks_like_valid_isbn()` enforces this for every rule
below that compares ISBNs.

| DOMAIN | RULE_CODE | INPUT CONDITION | AUTO ACTION | WARNING | REVIEW REQUIRED? | BLOCKING? |
|---|---|---|---|---|---|---|
| Identity | `IDENTITY_EXACT_ISBN` | Candidate and reference ISBNs are both valid ISBNs (not barcodes) and identical | AUTO_PASS — `match_decision = MATCH` | — | No | No |
| Identity | `IDENTITY_EXACT_TITLE_AUTHOR` | Title similarity ≥ 0.90 and author similarity ≥ 0.90 (single reference), or ≥2 independent sources agree with a specific author (consensus) | AUTO_PASS — `match_decision = MATCH` | — | No | No |
| Identity | `IDENTITY_EXACT_CANONICAL_TITLE` | ≥2 independent sources agree on title with no material conflict, no specific author differentiator required | AUTO_PASS — `match_decision = MATCH` | — | No | No |
| Identity | `IDENTITY_SERIES_VOLUME_MATCH` | Candidate/reference titles match once a known/common series prefix is stripped from either side (similarity ≥ 0.90) | AUTO_PASS | — | No | No |
| Identity | `IDENTITY_EDITION_METADATA_DIFFERENCE` | Identity is otherwise confirmed; ISBN/page_count/weight/dimensions/publication year differ by edition | AUTO_PASS — differing fields left null/pending, never guessed | one warning per differing field | No | No |
| Identity | `IDENTITY_COMBO_COMPLETE_MATCH` | Every volume/topic in a combo/set has its own confirmed (AUTO_PASS) identity match | AUTO_PASS | — | No | No |
| Identity | `IDENTITY_CONFIRMED_NO_MATCH` | Candidate/reference ISBNs are both valid but differ (single reference); or title similarity < 0.60 | AUTO_REJECT — `match_decision = NO_MATCH` | — | No | No |
| Identity | `IDENTITY_COMBO_SINGLE_AMBIGUITY` | Only some (not all) combo/set members have a confirmed identity match | — | — | **Yes** | No |
| Identity | `IDENTITY_CONFLICTING_CREDIBLE_SOURCES` | Multiple independent references disagree on ISBN, author, or page count (consensus only — a single reference disagreeing with the candidate's own claim is `IDENTITY_CONFIRMED_NO_MATCH`, not this) | — | — | **Yes** | No |
| Identity | `IDENTITY_INSUFFICIENT_EVIDENCE` | Title similarity in the 0.60–0.90 band without a strong author match; author data missing with strong title match; single-source-only consensus; missing title | — | — | **Yes** | No |

---

## Image (`src/domain/rules/image_rules.py`)

Covers `product_images.usage_rights_status` / `image_status` /
`is_selected_main_image` / `is_publish_eligible`. Rights classification
must always cite an established TSYC policy basis (CLAUDE.md sections
14.3/14.4) — **never generalize** every Facebook-collected image to
`STORE_OWNED`, or every bookstore image to `SUPPLIER_APPROVED`, without
one.

| DOMAIN | RULE_CODE | INPUT CONDITION | AUTO ACTION | WARNING | REVIEW REQUIRED? | BLOCKING? |
|---|---|---|---|---|---|---|
| Image | `IMAGE_STORE_OWNED_EXACT` | `usage_rights_status = STORE_OWNED` **and** an established policy basis is confirmed (exact shop-photographed post) | AUTO_PASS | — | No | No |
| Image | `IMAGE_APPROVED_SUPPLIER_EXACT` | `usage_rights_status = SUPPLIER_APPROVED` **and** an established supplier permission is confirmed | AUTO_PASS | — | No | No |
| Image | `IMAGE_APPROVED_PUBLISHER_EXACT` | `usage_rights_status = PUBLISHER_APPROVED` **and** an established publisher permission is confirmed | AUTO_PASS | — | No | No |
| Image | `IMAGE_SINGLE_ELIGIBLE_MAIN` | Exactly one image is `VALIDATED`, publish-eligible-rights, and confirmed to match the product | AUTO_PASS — auto-select as main image | — | No | No |
| Image | `IMAGE_COMBO_FULL_SET` | Exactly one `image_role = COMBO_IMAGE` image is eligible for a combo/set candidate (single-volume covers are never eligible — CLAUDE.md section 14.6) | AUTO_PASS — auto-select as combo main image | — | No | No |
| Image | `IMAGE_MULTIPLE_EQUIVALENT_CANDIDATES` | More than one image is equally eligible for main-image selection | — | — | **Yes** | No |
| Image | `IMAGE_RIGHTS_UNKNOWN` | `usage_rights_status` is `RIGHTS_UNKNOWN`, `REFERENCE_ONLY`, unrecognized, or a would-be-publishable status lacks a confirmed policy basis; or no image is eligible at all | — | — | **Yes** | No |
| Image | `IMAGE_PRODUCT_MISMATCH` | An image is confirmed not to represent the linked candidate | AUTO_REJECT | — | No (see note) | No |

Note: `IMAGE_PRODUCT_MISMATCH` also covers the confirmed-match
(AUTO_PASS) and not-yet-determined (REVIEW_REQUIRED) outcomes of the
same underlying check — see `evaluate_image_product_match()`.

---

## Content (`src/domain/rules/content_rules.py`)

Covers `product_contents.content_status`. Content must be based only on
verified data (CLAUDE.md section 15) and must never contain internal
workflow instructions. `REVISE` is a `prepare_product_content.py`
**action**, not a `content_status` value.

| DOMAIN | RULE_CODE | INPUT CONDITION | AUTO ACTION | WARNING | REVIEW REQUIRED? | BLOCKING? |
|---|---|---|---|---|---|---|
| Content | `CONTENT_VERIFIED_FACTS_ONLY` | Every claimed fact is traceable to verified internal_product/reference data; no reference conflicts affect the content | AUTO_PASS | — | No | No |
| Content | `CONTENT_MISSING_OPTIONAL_METADATA` | Optional fields (ISBN, weight, dimensions, page count, ...) are missing | AUTO_PASS — omit unresolved fields | one warning per missing field | No | No |
| Content | `CONTENT_INTERNAL_BOILERPLATE` | Customer-facing text is free of internal workflow language | AUTO_PASS | — | No | No |
| Content | `CONTENT_INTERNAL_BOILERPLATE` | Customer-facing text contains internal workflow language (e.g. "manager must review this before publishing", "pending manager review", "this description should be completed later", TODO/FIXME markers) | — deterministic REVISE target | — | **Yes** — validate again after REVISE | No |
| Content | `CONTENT_UNSUPPORTED_CLAIM` | Content asserts a fact not traceable to verified data, or that conflicts with a verified value | — | — | **Yes** | No |
| Content | `CONTENT_REFERENCE_CONFLICT` | Verified references disagree on a fact content would need to state as settled | — | — | **Yes** | No |
| Content | `CONTENT_SAFE_APPROVAL` | Content has no prior saved draft at all | — | — | No | **Yes** — save and enrich first |
| Content | `CONTENT_SAFE_APPROVAL` | Content is still the untouched, metadata-only generated draft | — | — | **Yes** | No |
| Content | `CONTENT_SAFE_APPROVAL` | Every other content rule (boilerplate, unsupported claims, reference conflicts) AUTO_PASSed, and the draft has been enriched | AUTO_PASS — `content_status = APPROVED` | warnings from passing checks are still surfaced | No | No |

---

## Readiness (`src/domain/rules/readiness_rules.py`)

Covers `internal_products.woocommerce_status = READY_FOR_DRAFT`
(CLAUDE.md section 16) — the last deterministic stage before the single
required human authorization (section 6). A single all-or-nothing gate:
every blocking condition must hold at once.

| DOMAIN | RULE_CODE | INPUT CONDITION | AUTO ACTION | WARNING | REVIEW REQUIRED? | BLOCKING? |
|---|---|---|---|---|---|---|
| Readiness | `READY_FOR_DRAFT` | Linked candidate not found | — | — | No | **Yes** |
| Readiness | `READY_FOR_DRAFT` | `identity_status = IDENTITY_VERIFIED`; `content_status = APPROVED` with an approved content row present; image aggregate `= APPROVED`; exactly one selected main image that is `VALIDATED`, publish-eligible, and has publishable rights; `recovery_required = false`; no created WooCommerce sync already exists | AUTO_PASS | see below | No | No |
| Readiness | `READY_FOR_DRAFT` | Any of the above conditions fails | — | — | **Yes** | No |

Non-blocking warnings on `READY_FOR_DRAFT` (never withhold AUTO_PASS):
missing ISBN, missing weight, missing dimensions, missing page count,
`pricing_status != APPROVED` (CLAUDE.md sections 2.2/2.5/16).

**Scope note:** "no duplicate remote SKU" (CLAUDE.md section 17,
"search remote by SKU" before POST) is inherently a live WooCommerce API
check, not a local Supabase-readable condition — it is deliberately not
evaluated by this rule. It remains `create_woocommerce_draft.py`'s own
immediate pre-POST responsibility (`find_product_by_sku()`), performed
fresh in addition to this gate, exactly as CLAUDE.md section 17 requires.

---

## The single human gate

Everything above exists to get a candidate to `READY_FOR_DRAFT`
automatically. At that point, and only at that point, the pipeline stops
for the one required business decision: explicit bounded human
authorization to create a WooCommerce **draft** (never `publish`, never
a price) — CLAUDE.md section 6. A bounded approval covers every exact
`READY_FOR_DRAFT` candidate/product code the user names in one request;
the orchestrator does not ask once per product after that.
