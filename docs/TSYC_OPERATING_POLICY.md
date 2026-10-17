# TSYC Operating Policy

## Data-source policy

### Identity / metadata
Preferred order:
1. Publisher
2. Authorized supplier
3. Reliable bookstore
4. Fahasa
5. Facebook

### Purchase price
Official order:
1. Purchase invoice
2. Confirmed purchase order
3. Supplier quotation
4. Current supplier price list

Public reference sites are never purchase-price sources.

## Identity policy
- `IDENTITY_VERIFIED` requires reliable evidence.
- ISBN conflicts are hard conflicts.
- Missing ISBN is non-blocking.
- Missing weight is non-blocking.
- Existing verified identity must not be silently downgraded or overwritten.
- Refreshing a previously matched reference requires explicit re-verification.

## Content policy
- Metadata-only generic drafts may be saved but not approved.
- Approved content must be preserved.
- Content must reflect verified source material and must not hallucinate plot/topic details.

## Image policy
- Upload does not equal approval.
- Images begin non-publishable.
- Rights must be explicit.
- Exactly one main image may be selected.
- Publishable rights (canonical values, `src/domain/rights_status.py`):
  - STORE_OWNED
  - PUBLISHER_APPROVED
  - SUPPLIER_APPROVED
- REFERENCE_ONLY and RIGHTS_UNKNOWN are not publishable.
- Owner-authorized Fahasa product images (CLAUDE.md 14.8; covers 2026-10-09,
  extended 2026-10-17): TSYC may use the images of the verified book on its
  Fahasa product page -- front/back covers, gallery images, interior pages and
  illustrations, previews/sample pages -- on its own WooCommerce listings,
  pre-order listings included, for FB-HIST-, FAHASA- and other verified
  candidates. Only images of the candidate's own MATCH Fahasa product page
  qualify, after the provenance, same-book/volume/sellable-unit, quality
  (>= 400 px) and price-label/visual-review gates in
  `image_rules.evaluate_fahasa_image_authorization`; they are then
  SUPPLIER_APPROVED without per-image owner approval.
  - The main image is always the verified cover; gallery/preview images are
    gallery-only and each needs a visual review confirming it depicts this
    book with no price label or misleading transformation (Fahasa series
    galleries often show other volumes' covers).
  - Other products' images (configurable-item swatches), other sites and
    other uses are never covered; duplicates are dropped; failing images are
    isolated as REVIEW_REQUIRED with the reason, never edited.
  - Existing Woo products: owner-curated galleries and descriptions are never
    overwritten, published products are never changed automatically, and
    REMOTE_REMOVED products are never recreated.

## WooCommerce policy
- Create drafts only.
- Never publish automatically.
- Do not auto-set selling price.
- Do not create a second draft if a remote product may already exist.
- Reconcile remote/local mismatch before retry.

## Error policy
On any error:
1. stop the current write chain
2. preserve remote IDs/evidence
3. do not retry destructive or create operations blindly
4. run integrity audit
5. explain the mismatch
6. propose one bounded recovery action

## Audit acceptance
`PASS_WITH_WARNINGS` is acceptable when warnings are only known non-blocking metadata gaps.
Any ERROR blocks unattended continuation.
