# TSYC Production Runbook

## Standard Python runtime
PowerShell: `.\.venv\Scripts\python.exe`
Claude Code Bash: `.venv/Scripts/python.exe`
Never use system Python when `.venv` exists.

## 1. Normal production flow
1. Run preflight.
2. Start a bounded batch (explicit candidate allowlist + `--max-candidates`).
3. Deterministic stages auto-progress to `READY_FOR_DRAFT`.
4. Review any `REVIEW_REQUIRED` / `BLOCKED` / `RECOVERY_REQUIRED` candidates from the grouped result.
5. Resume a reviewed candidate the same way (re-run the batch command for its code).
6. Give one bounded Woo draft approval for the candidates shown as `READY_FOR_DRAFT`.
7. Woo drafts are created serially, reconciled, and audited automatically.
8. Stop. Publishing and final selling price are never automatic.

## 2. Preflight
```
.venv/Scripts/python.exe scripts/preflight_pipeline.py
```
Read-only. Checks Python environment, configuration presence, Supabase read
connectivity, domain/schema drift, the decision engine, the deterministic
pipeline audit, recovery health, WooCommerce read connectivity, orchestrator
safety config, and Git state. Ends with `READY_FOR_BATCH` (exit 0) or
`NOT_READY_FOR_BATCH` (exit 1). `run_batch.py` also runs this in-process
before any non-dry-run batch and refuses to start writes on a blocker.

## 3. Start bounded batch
```
.venv/Scripts/python.exe scripts/run_batch.py ^
  --candidate-code CAN-0012 --candidate-code CAN-0013 --candidate-code CAN-0014 ^
  --max-candidates 5 ^
  --non-interactive
```
`--candidate-codes CAN-0012,CAN-0013,CAN-0014` (comma-separated) works the
same way. No implicit "all"/"newest" mode exists -- the allowlist is always
explicit and bounded by `--max-candidates`. Add `--dry-run` for a read-only
diagnostic pass (skips preflight and never invokes a writer). Add `--verbose`
for the full per-stage trace; the default output is the grouped result below.

## 4. Exception review
The default output groups every candidate into exactly one bucket:
`READY_FOR_DRAFT`, `REVIEW_REQUIRED`, `BLOCKED`, `AUTO_REJECTED`,
`DRAFT_CREATED`, `RECOVERY_REQUIRED`. Each `REVIEW_REQUIRED`/`BLOCKED`
candidate prints its candidate code, derived state, and short reason --
nothing else needs to be read to decide what happened.

## 5. Resume a reviewed candidate
Fix the underlying condition with the normal stage script (e.g. register a
reference source, select/approve an image, resolve an identity conflict),
then re-run `run_batch.py` with the same candidate code -- it re-derives
state and continues from wherever it now stands.

## 6. Woo draft approval
When one or more candidates reach `READY_FOR_DRAFT`, the batch stops and
prints exactly those candidate codes plus one bounded approval request. Add
`--allow-woo-draft` and re-run the same command to authorize exactly that
set:
```
.venv/Scripts/python.exe scripts/run_batch.py ^
  --candidate-code CAN-0012 --candidate-code CAN-0013 ^
  --max-candidates 5 ^
  --non-interactive --allow-woo-draft
```
Never one approval per candidate. Authorization never extends to a
candidate outside the exact codes passed on that invocation.

## 7. Woo reconciliation
`DRAFT_CREATED` candidates automatically reconcile against the remote
product (SKU, status, media) on the next batch pass -- read-only against
WooCommerce, never a duplicate create. An uncertain remote result stops that
candidate as `RECOVERY_REQUIRED`; independent candidates continue.

## 7a. Mandatory catalog identity check (every create)
Most live products have no SKU, so an exact-SKU search never proves that a
book is new. `create_woocommerce_draft.py` runs a full catalog identity check
after the SKU check and before any sync reservation, media upload or POST
(`src/domain/woo_catalog_identity.py`, loader `scripts/catalog_identity_loader.py`).

What it compares:
- every Woo product, all statuses including trash, with X-WP-Total verified;
- TSYC removal records, including legacy remote-loss rows;
- other internal products, candidates and MATCH references;
- 893 publisher barcodes, kept separate from ISBN;
- Polylang groups from the catalog-audit cache, so linked copies count once.

What each result does:

| Result | Effect |
|---|---|
| `NEW_CONFIRMED` | The only result that creates. |
| `EXISTING_PRODUCT` | Terminal `EXISTING_WOO_PRODUCT`; never created. |
| `REMOTE_REMOVED` | Terminal; the owner removed it, so it is never recreated. |
| `POSSIBLE_DUPLICATE`, `DISTINCT_EDITION`, `LINKED_TRANSLATION`, `IDENTITY_CONFLICT` | Recorded in `product_metadata.catalog_identity_check`; human gate `CATALOG_DUPLICATE_REVIEW`. |
| `CATALOG_UNAVAILABLE` | Fail closed, exit 3, nothing recorded. |

Two more guards around the POST:
- The create is reserved by a compare-and-set on the sync row. A concurrent creator loses before uploading anything.
- A fresh exact-SKU any+trash re-check runs immediately before the POST.

Resolving a `CATALOG_DUPLICATE_REVIEW` once the evidence is clear (cover,
publisher, translator, language, sellable unit), with the sources in a JSON
file (`{"summary": ..., "sources": [{"source", "finding"}]}`):
```
.venv/Scripts/python.exe scripts/resolve_catalog_duplicate_review.py --product-code TSYC-<code> --decision SAME_AS_EXISTING --woo-id <id> --evidence-file <file> --max-products 1   (dry run)
.venv/Scripts/python.exe scripts/resolve_catalog_duplicate_review.py --product-code TSYC-<code> --decision DISTINCT --distinct-from <ref> --evidence-file <file> --max-products 1 --confirm-apply
```
- `SAME_AS_EXISTING` writes the terminal `EXISTING_PRODUCT` marker; every
  `--woo-id` must be in the stored evidence and is re-read live (not trashed).
- `DISTINCT` records the exact (origin, ref, title) of each reviewed POSSIBLE
  match in `product_metadata.catalog_identity_review`, and a fresh complete
  catalog check must then return `NEW_CONFIRMED`. The create path applies the
  same review before every POST. It never clears a certain, edition, ISBN or
  barcode match, nor a new match or one whose title has changed since.
- If the evidence stays uncertain, leave the gate in place.

## 8. Recovery
`RECOVERY_REQUIRED` means remote state could not be confirmed. Never retry
draft creation blindly. Reconcile manually with
`sync_woocommerce_product_status.py`, confirm the real remote state, then
resume the candidate.

A FAILED sync **without** a Woo ID (the shapes the other tools refuse) goes to
`recover_historical_woo_create.py`. It is GET-only remotely and a dry run unless
`--confirm-apply`:
```
.venv/Scripts/python.exe scripts/recover_historical_woo_create.py --product-code TSYC-... --max-products N [--confirm-apply --non-interactive]
```
It handles two record shapes:
- **Legacy remote loss**: the old Woo ID sits in `remote_loss_confirmed`.
  - Requirement: fresh 404 by that ID, and exact SKU any = 0, trash = 0.
  - Effect: it restores the historical ID and records the standard REMOTE_DELETED marker. The product is never recreated.
- **Interrupted create**: no Woo ID was ever stored.
  - Resolved only when exact SKU any = 0, trash = 0, **and** the catalog check proves `EXISTING_PRODUCT`.
  - The original error stays, and the residual uncertainty is written down.
  - Any other result keeps the recovery state; `NEW_CONFIRMED` is never cleared toward a new create.

## 9. Final audit
```
.venv/Scripts/python.exe scripts/audit_pipeline_state.py
```
Expect `PASS` or `PASS_WITH_WARNINGS` (accepted: `ISBN_MISSING`,
`WEIGHT_MISSING`). Any `ERROR` stops further batch work until investigated.

## 10. Never publish / never auto-price
WooCommerce products are always created `status = draft`, with no
`regular_price`/`sale_price`/`price` field. Publishing and selling-price
entry are shop-owner decisions made later, outside this automation, and are
never performed by any script in this repository.

## 11. Fahasa new-book discovery (read-only)
```
.venv/Scripts/python.exe scripts/discover_fahasa_new_books.py --max-listings 30
.venv/Scripts/python.exe scripts/discover_fahasa_new_books.py --max-listings 30 --reclassify data/output/fahasa_discovery/<report>.json
```
Reads one newest-first Fahasa listing (default: children's books), parses
each product page with the shared Fahasa parser, and classifies every book
against the unified identity index (all Woo statuses incl. trash, Woo sync
records incl. owner-removed products, internal products, candidates, MATCH
references, the Facebook history export): `NEW_CONFIRMED`,
`EXISTING_PRODUCT`, `POSSIBLE_DUPLICATE`, `DISTINCT_EDITION`,
`FACEBOOK_COVERAGE_UNKNOWN`, `REMOTE_REMOVED`. A new volume of a series the
shop already sells is `POSSIBLE_DUPLICATE` (sellable-unit decision).
Writes only `data/output/fahasa_discovery/discovery_<UTC>.json` and the
first-seen ledger; never Supabase, Woo, images or prices. Discovery records
Fahasa images as `RIGHTS_UNKNOWN`; rights are decided later, per candidate, by
the owner-authorized Fahasa product-image policy (CLAUDE.md 14.8,
`image_rules.evaluate_fahasa_image_authorization`) once each image is stored
against the candidate's MATCH Fahasa reference. The cover is the main image;
a gallery/preview image joins the Woo gallery only with a stored visual review
(see "Fahasa gallery and preview images" below).
`--reclassify` re-applies the current rules to an earlier report without
contacting Fahasa. A live run takes ~20 s per book; run it in the background.
Discovery takes the newest book pages first, so to reach books past an
earlier run raise `--max-listings` (<= 60) rather than re-reading the same
head of the listing.

Then, for the exact NEW_CONFIRMED indexes chosen:
```
.venv/Scripts/python.exe scripts/import_fahasa_candidates.py --discovery-report <report>.json --cover-reviews <reviews>.json --discovery-index N ... --max-candidates K --batch-code FAHASA-2026-NNN --execute
.venv/Scripts/python.exe scripts/run_batch.py --batch-code FAHASA-2026-NNN --candidate-codes <codes> --max-candidates K --non-interactive
```
FAHASA- candidates are live-pipeline candidates: Woo drafts need the owner's
bounded authorization on exact codes (`--allow-woo-draft`).

## Editorial content pipeline (VI/EN/DE)
Read `docs/content_style/TSYC_CONTENT_GUIDE.md` and `docs/content_style/EDITORIAL_REFERENCE.md` first. Then write
`data/processed/editorial_packages/<product_code>.json`, which holds:
- the evidence ledger (`src/domain/content_evidence.py`);
- the Vietnamese DRAFT plus at most 3 EDIT revisions;
- the EN/DE localizations.

Evaluate first (dry run, no write), then apply:
```
.venv/Scripts/python.exe scripts/run_content_editorial.py --product-code <code> ... --max-products K --non-interactive
.venv/Scripts/python.exe scripts/run_content_editorial.py --product-code <code> ... --max-products K --non-interactive --confirm-apply
```
What the runner checks and does:
- **Versions:** every version is scored (`content_quality_score`, target 85) and kept in `data/processed/editorial_audit/` and process_logs. The last version must pass every hard gate.
- **Translations:** the EN/DE texts must pass the same `translation_rules` gate TRANSLATE applies, plus `multilingual_consistency`.
- **Writes:** it writes only through `prepare_product_content.py`: REVISE (AI_ASSISTED) and APPROVE, then EXPORT_PACKAGE (or a refill of the existing package) and TRANSLATE.
- **Never written:** APPROVED Vietnamese text (VI_ALREADY_APPROVED), published products, Woo, prices.
- **Isolation:** INSUFFICIENT evidence, a failing final version, or a failing localization isolates that product with its defects. Revise the package and re-run.

## Fahasa gallery and preview images (CLAUDE.md 14.8)
Every writer below takes exact codes plus a max, and is a dry run unless
confirmed. Run them serially:
```
.venv/Scripts/python.exe scripts/collect_fahasa_gallery_images.py --candidate-code <code> ... --max-candidates K --non-interactive --confirm-collect
.venv/Scripts/python.exe scripts/review_fahasa_gallery_images.py --candidate-code <code> ... --max-candidates K --visual-reviews data/output/fahasa_gallery/visual_reviews_<date>.json --non-interactive --confirm-apply
.venv/Scripts/python.exe scripts/apply_fahasa_gallery_to_woo_drafts.py --product-code TSYC-<code> ... --max-products K --non-interactive --confirm-update
```
1. **Collect** reads the candidate's single FAHASA MATCH page. Its roles are
   COVER, SUPPLEMENTARY, PREVIEW (the "Đọc thử" pages under
   `/media/flashmagazine/`, full resolution despite the `-390x510` name) and
   OTHER_PRODUCT (another volume's cover).
   - OTHER_PRODUCT images are recorded but never downloaded.
   - Every other image is downloaded, SHA-256 deduplicated and registered
     PENDING / RIGHTS_UNKNOWN. A hash already registered for another
     candidate is never relinked.
   - It writes the per-image manifest to
     `product_references.raw_metadata.page_images`. A failed download fails
     only that image.
2. **Visual review** is done by looking at each image (a Claude Code visual
   review). It writes one verdict per image to a JSON list:
   `candidate_code, image_url, depicts_candidate, edition_matches,
   price_label_visible, misleading_transformation, relevant_preview,
   visual_duplicate, image_kind, reviewed_by, reviewed_at, notes`.
3. **Review** stores the verdicts in `source_evidence.fahasa_image_reviews`.
   It writes a record for every image to
   `source_evidence.fahasa_image_review_records` (`image_rules.review_fahasa_image`):
   - APPROVED images become VALIDATED, SUPPLIER_APPROVED gallery images.
   - REJECTED images (another product, wrong book or edition, price label,
     overlay, duplicate, irrelevant preview, too small) become REJECTED.
   - REVIEW_REQUIRED images stay PENDING. This covers any unknown verdict
     field.
   - The main cover is never touched. A rejected main cover means
     MAIN_COVER_FAILED, and no gallery change is made.
   - A preview or interior page is never main-eligible.
4. **Woo enrichment** applies only to a still-DRAFT_CREATED Fahasa pre-order
   draft whose remote images are exactly what TSYC last applied. An
   owner-curated gallery or a published, trashed or foreign product is
   isolated.
   - It sends only `images`, and the re-read proves status, prices, inventory
     and text are unchanged.
   - An uncertain PUT is never retried.
   - New drafts need no enrichment: `create_woocommerce_draft.py` already
     includes every VALIDATED, publish-eligible gallery image.
5. **Main-cover review record.** A COVER_TEXT evidence item in an editorial
   package needs the cover's review record. If a candidate has no gallery
   images to review, run `review_fahasa_gallery_images.py` once without
   `--visual-reviews` so that the record exists.

## Contributor roles (author, reteller, illustrator, translator)
Fahasa's author field often merges distinct roles. For example,
"Hiếu Minh, Cloud Pillow Studio" stands for a cover that reads
"Hiếu Minh kể lại | Cloud Pillow Studio minh họa". Never present a merged
field as one "Tác giả" list. Read the roles from the cover or the
reference, then record them before the Woo draft is created:
```
.venv/Scripts/python.exe scripts/record_contributor_roles.py --product-code TSYC-<code> \
    --contributor "RETELLER=Hiếu Minh" --contributor "ILLUSTRATOR=Cloud Pillow Studio" \
    --evidence-url <registered reference URL> --evidence-image-url <cover URL> \
    --credit-text "Hiếu Minh kể lại | Cloud Pillow Studio minh họa"            (dry run)
... --confirm-record
```
- The script writes only `internal_products.product_metadata.contributor_roles`.
  The verified `author` column is never rewritten.
- Woo attributes become one entry per role: Tác giả / Nguyên tác /
  Người kể lại / Người dịch / Minh họa (`woo_presentation.build_attributes`).
- Every name must appear in the recorded credit text.
  - AUTHOR and RETELLER must already be in the author field.
  - TRANSLATOR, ILLUSTRATOR and ORIGINAL_AUTHOR may come from outside the
    author field. A TRANSLATOR must match the reference's recorded
    translator.
- The script never drops an author-field name and never overwrites an
  existing different record.
- Add an original author (e.g. Andersen) only when the edition itself
  credits them. One Vietnamese title can name different tales.
- Co-authors whose roles the cover does not distinguish stay AUTHOR. Never
  guess an illustrator.

## Published-product repair packages (owner-authorized only)
Published products are owner-maintained. A repair package
(`data/processed/orchestration/published_repair_package_<date>/package_vN.json`,
gitignored because it holds live store text) is built read-only. It is applied
only after the owner authorizes the exact Woo IDs:
```
.venv/Scripts/python.exe scripts/apply_published_repair_package.py --package <package_vN.json> \
    --woo-id <id> [--woo-id ...] --max-products <n>                          (dry run)
... --non-interactive --confirm-apply
... --rollback --confirm-apply                                               (restore one product)
```
- Before writing, the script checks that the product still matches the
  package baseline: exact id, SKU, name, status `publish`, the rendered
  description's sha256, `date_modified` and the attributes. Any owner edit
  since the baseline isolates the product.
- The package fragments are *rendered* HTML (wpautop). The raw content has no
  `<p>` wrapper and uses CRLF and indentation, so the script matches whole
  raw lines, and each fragment must match exactly once.
- A versioned rollback record (`rollback/<id>_<utc>.json`) is written before
  every PUT. The PUT carries only `description` and `attributes`.
- After the write, the script reads the product back and verifies three
  things: the raw text is exactly as expected, the rendered sha256 equals
  `after_sha256`, and status, prices, stock, images, categories, SKU, slug and
  short description are unchanged.
- `apply_checkpoint_vN.json` makes the batch resumable. An uncertain PUT is
  re-read and never retried. `--rollback` refuses to run if the product was
  edited after the repair.
- 2026-10-19: package v2 was applied to the 12 published FAHASA-2026-001
  products (#4765–#4789 odd, excluding the trashed #4773).

## Git discipline
`git status`, `git diff --check`, review the exact diff, run relevant tests
before every commit.
