# Tiệm Sách Yêu Con — CLAUDE.md

## 1. Project Purpose

This repository powers the automation pipeline for Tiệm Sách Yêu Con (TSYC).

The system processes Vietnamese book-product information from approved sources, verifies identity and provenance, prepares internal product data, prepares product content and images, validates readiness, and creates WooCommerce DRAFT products.

The intended stable operating model is:

Facebook / approved source
→ collect
→ clean
→ candidate extraction
→ duplicate protection
→ reference registration
→ identity verification
→ internal product
→ image collection
→ image validation
→ content drafting
→ content revision/validation
→ content approval
→ readiness
→ READY_FOR_DRAFT
→ HUMAN APPROVAL
→ WooCommerce DRAFT creation
→ reconciliation
→ audit
→ STOP

The pipeline must automate normal deterministic work.

The user should NOT need to approve ordinary database reads/writes or every intermediate pipeline command.

The main required business approval is WooCommerce DRAFT creation after a product reaches READY_FOR_DRAFT.

---

# 2. Golden Principles

## 2.1 Preserve provenance

Never silently remove, fabricate, downgrade, or replace source provenance.

Every important external reference must remain traceable through the pipeline.

Preferred provenance chain:

source_urls
→ raw_pages / candidate_reference_sources
→ product_references
→ product_candidates
→ internal_products
→ product_contents / product_images
→ woocommerce_product_syncs

A product_reference used for identity must have a valid registered source_url_id.

Do not create product_references with source_url_id = NULL through normal application workflows.

---

## 2.2 Never invent metadata

Do not fabricate:

- ISBN
- barcode
- author
- translator
- publisher
- page count
- dimensions
- weight
- publication year
- edition
- purchase price
- selling price
- source authorization
- image rights

If metadata is unknown or conflicting, leave it null/pending and continue where project rules allow.

Missing ISBN and missing weight are warnings, not automatic blockers for WooCommerce draft creation.

---

## 2.3 Do not confuse barcode and ISBN

Vietnamese identifiers beginning with 893 are normally EAN/product barcodes.

Do NOT record a 893-prefixed identifier as ISBN unless independent evidence proves that it is an ISBN.

A valid ISBN-13 normally begins with:

978
or
979

---

## 2.4 Never auto-publish

WooCommerce product creation must always use:

status = "draft"

Never create or modify a product with:

status = "publish"

Never automatically publish an existing draft.

Publishing is permanently outside the automatic pipeline.

---

## 2.5 Never automatically set or change selling price

The automation must never automatically set or modify:

regular_price
sale_price
price

Pricing may remain PENDING while the product is created as a WooCommerce draft.

The shop owner may review and enter/update the selling price later.

---

## 2.6 Never blind-retry uncertain remote operations

If a WooCommerce or WordPress operation returns an uncertain result:

- timeout
- connection interruption
- ambiguous response
- possible remote success with local failure

DO NOT blindly retry the create operation.

Reconcile remote state first.

Search by exact SKU / known remote ID.

Reuse already-created WordPress media when safely provable.

Only retry after remote non-existence has been explicitly established.

---

## 2.7 Existing verified/approved data is protected

Do not silently overwrite:

- IDENTITY_VERIFIED candidate identity
- APPROVED content
- VALIDATED image selections
- selected main image
- established source provenance
- successful WooCommerce sync state

A lower-confidence source must never overwrite higher-confidence verified data automatically.

---

# 3. Stable Automation Policy

## 3.1 Core operating goal

Normal bounded TSYC processing should run automatically from source collection until:

READY_FOR_DRAFT

Do not ask for approval for ordinary deterministic pipeline work.

The user should normally be interrupted only when:

1. the system encounters true ambiguity or a recovery condition that cannot be resolved deterministically; or
2. products are READY_FOR_DRAFT and WooCommerce draft creation requires authorization.

---

# 4. Database Permission Policy

Human approval is based on BUSINESS DECISION RISK, not merely on whether an operation writes to the database.

Do not ask the user for approval simply because a normal approved pipeline stage performs a Supabase INSERT or UPDATE.

---

## 4.1 AUTO-ALLOW — database reads

Automatically allow normal bounded reads through approved scripts/repository methods, including:

- candidate lookup
- raw_page lookup
- source_url lookup
- product_reference lookup
- internal_product lookup
- product_content lookup
- product_image lookup
- Woo sync lookup
- pipeline-state derivation
- audit reads
- readiness reads
- reconciliation reads
- duplicate checks
- exact SKU checks
- source provenance verification

---

## 4.2 AUTO-ALLOW — deterministic database writes

Automatically allow normal bounded writes performed through approved TSYC scripts/repository methods.

Examples:

- raw_pages creation/update
- cleaned_text update
- source crawl status update
- product candidate creation
- duplicate-state recording
- source registration
- candidate_reference_sources writes
- product_reference creation
- deterministic MATCH decisions
- deterministic identity verification
- internal_product creation after IDENTITY_VERIFIED
- DRAFTED product content creation
- DRAFTED product content REVISE
- product image registration with safe defaults
- deterministic rights classification under existing explicit policy
- deterministic main-image selection when unambiguous
- content APPROVAL when all automated factual/content validation gates pass
- readiness status updates
- READY_FOR_DRAFT transition
- process_logs
- deterministic Woo reconciliation updates
- DRAFT_CREATED local synchronization after confirmed remote creation
- audit/checkpoint bookkeeping

All automatic writes must use exact bounded targets.

---

## 4.3 Do not auto-allow arbitrary SQL mutation

Normal automation writes should use:

approved Python scripts
→ SupabaseRepository / repository layer

Do not broaden arbitrary MCP execute_sql mutation permissions merely to make the workflow automated.

Raw mutation SQL should remain restricted.

---

# 5. Human Decision Policy

## 5.1 Normal pipeline stages should NOT require approval

Do NOT stop merely for:

- database read
- deterministic database write
- candidate creation
- internal product creation
- content DRAFT creation
- content REVISE
- deterministic content approval
- image registration
- deterministic image-rights classification under established policy
- deterministic main-image selection
- readiness updates
- audit
- reconciliation
- ordinary pipeline logging

---

## 5.2 Stop only for true ambiguity

Human review is required when deterministic evidence is insufficient.

Examples:

### Identity ambiguity

Stop when:

- conflicting credible sources identify different products
- multiple editions materially affect product identity
- sellable unit is unclear
- combo vs individual volume is unclear
- evidence does not support deterministic MATCH

Do NOT stop merely because string similarity is low if stronger deterministic evidence resolves identity.

---

### Image ambiguity

Stop when:

- image does not clearly match product
- combo image does not represent the full sellable unit
- multiple plausible main images require judgment
- image ownership/authorization cannot be determined under existing policy

---

### Content ambiguity

Stop when:

- verified references conflict
- required factual statement cannot be made without guessing
- content validator finds unsupported claims
- a meaningful product distinction cannot be resolved automatically

Do not stop simply because content needs normal deterministic formatting/revision.

---

### Recovery ambiguity

Stop when remote state cannot be proven.

Examples:

- Woo create may or may not have succeeded
- remote media may exist but cannot be linked confidently
- local state conflicts with remote state
- duplicate SKU condition cannot be reconciled safely

---

# 6. Single Required Business Approval — WooCommerce Draft

The normal stable pipeline should automatically process products until:

woocommerce_status = READY_FOR_DRAFT

At that point, request explicit human authorization before creating WooCommerce drafts.

Example:

3 products are READY_FOR_DRAFT.
Create WooCommerce drafts for:
CAN-X
CAN-Y
CAN-Z?

A bounded approval authorizes exactly those candidate/product codes.

Do NOT ask once per product after the user has already approved the exact bounded batch.

---

## 6.1 Woo draft authorization requirements

Before creation, revalidate:

- identity_status = IDENTITY_VERIFIED
- content_status = APPROVED
- review_required = false
- image aggregate status = APPROVED
- exactly one selected main image
- main image status = VALIDATED
- main image is_publish_eligible = true
- usage-rights status is publishable
- woocommerce_status = READY_FOR_DRAFT
- no created local Woo sync already exists
- recovery_required is false
- no duplicate remote SKU exists

Pricing PENDING is non-blocking.

---

# 7. Hard Deny Rules

Never automate or permit through normal unattended operation:

- DROP
- TRUNCATE
- destructive/unbounded DELETE
- broad UPDATE without an exact bounded target
- database/security/provenance bypass
- automatic WooCommerce publish
- automatic selling-price mutation
- blind retry after uncertain Woo create
- disabling readiness gates
- disabling identity gates
- disabling image-rights gates
- replacing verified identity with lower-confidence data
- overwriting APPROVED content silently
- creating duplicate Woo products knowingly
- reading or exposing secrets
- reading .env content for display
- reading browser cookies/auth secrets for display
- committing credentials
- committing Playwright/Facebook profiles
- committing .env

---

# 8. Source Priority

## 8.1 Book identity source priority

Preferred identity order:

1. PUBLISHER
2. AUTHORIZED_SUPPLIER
3. BOOKSTORE
4. FAHASA
5. FACEBOOK_POST
6. OTHER

Use exact canonical project/DB source_type values.

Do not invent a new source_type without schema/project review.

---

## 8.2 Purchase price source priority

Official purchase price must come from:

1. purchase invoice
2. confirmed purchase order
3. supplier quotation
4. current supplier price list

Fahasa is NEVER the official purchase-price source.

Facebook listing price is not automatically the official purchase price.

---

## 8.3 Fahasa role

Fahasa may be used as a reference for:

- identity
- dimensions
- estimated weight
- images
- book description/content understanding
- cover/reference price where project pricing logic requires it

Do not treat Fahasa as the official supplier purchase-price source.

---

# 9. Identity Rules

## 9.1 Deterministic identity may auto-verify

The stable automated pipeline may set:

IDENTITY_VERIFIED

without asking the user when strong evidence deterministically establishes identity and no material conflict exists.

Examples:

- exact canonical title match from approved source
- ISBN match
- exact title + author
- exact volume title with only a known/common series prefix omitted
- verified combo/set mapping where all volumes/topics correspond

The automated matcher must preserve evidence and confidence.

---

## 9.2 Ambiguous identity must stop

Do not force MATCH merely to advance the pipeline.

Use:

IDENTITY_PENDING
IDENTITY_CONFLICT
MANUAL_REVIEW
POSSIBLE_MATCH

as appropriate.

Only stop for a human decision when evidence is genuinely ambiguous.

---

## 9.3 Edition differences

Identity and edition-specific metadata are separate concerns.

A title may be safely IDENTITY_VERIFIED even when:

- ISBN differs by edition
- page count differs
- weight differs
- dimensions differ
- publication year differs

If product identity is clear but exact edition is unknown:

verify identity
leave edition-specific fields null/pending

Do not block normal draft workflow merely because physical edition metadata is unresolved.

---

# 10. Reference and Provenance Rules

Every identity reference must originate from a registered source.

Required chain:

registered source_url
→ source_url_id
→ product_reference
→ match_decision
→ candidate

Do not create a MATCH reference with missing provenance.

Manual and automated reference workflows must enforce the same invariant.

---

# 11. Facebook Collection Rules

Only collect approved/authorized Facebook sources.

Use exact source targeting.

Never silently select an arbitrary newest pending source during bounded automation.

For multi-book Facebook posts:

- create distinct candidates for distinct sellable products
- preserve the shared raw_page/source provenance
- do not silently attach all shared-post images to the newest candidate
- explicit candidate mapping is required where image ownership is ambiguous

---

# 12. Facebook Text Cleaning

Facebook may inject invisible Unicode anti-scraping characters.

The cleaner must remove known verified obfuscation characters such as:

U+034F COMBINING GRAPHEME JOINER

without damaging legitimate Vietnamese Unicode text.

Do not strip non-ASCII text broadly.

Do not remove Vietnamese diacritics.

Regression tests must protect this behavior.

---

# 13. Internal Product Rules

Internal product creation is deterministic after:

identity_status = IDENTITY_VERIFIED

Requirements:

- valid candidate
- valid MATCH reference
- reference belongs to candidate
- reference has source_url_id
- no duplicate internal_product

No extra human approval is required merely to insert the internal_product.

---

# 14. Image Policy

## 14.1 Image registration is automatic

Images may be collected and registered automatically with safe defaults.

Initial default examples:

image_status = PENDING
is_selected_main_image = false
is_publish_eligible = false

Registration itself is not a human gate.

---

## 14.2 Rights statuses

Use exact canonical DB values.

Current publishable rights statuses include:

STORE_OWNED
PUBLISHER_APPROVED
SUPPLIER_APPROVED

Non-publishable examples include:

REFERENCE_ONLY
RIGHTS_UNKNOWN

Use shared project constants rather than independently hardcoding these values in multiple scripts.

---

## 14.3 STORE_OWNED

An image may be classified as STORE_OWNED when existing TSYC business policy/evidence establishes that the image was created or photographed by the shop.

Example:

exact TSYC Facebook post
+ shop-created/shop-photographed product image
+ provenance preserved
→ STORE_OWNED

Do NOT assume every image posted to Facebook is STORE_OWNED.

---

## 14.4 Publisher/supplier/bookstore images

Where TSYC has established permission to use an exact publisher/supplier/bookstore product image:

publisher permission
→ PUBLISHER_APPROVED

supplier/bookstore permission
→ SUPPLIER_APPROVED

Do not require repeated human approval for every image when the source falls under an already-approved business policy and the visual match is deterministic.

Stop only when rights/source classification is unclear.

---

## 14.5 Main-image selection

Automatically select a main image when:

- exactly one eligible image exists
- visual match is deterministic
- rights are publishable
- image represents the correct sellable unit

Stop when multiple plausible images require subjective selection.

Exactly one selected main image is required for Woo draft readiness.

---

## 14.6 Combo/set image rule

For a combo/set product, the selected main image must represent the full sellable unit.

Do not select a single-volume cover as the main image for a multi-volume combo.

A shop-created composite showing the complete set is acceptable when provenance and ownership are established.

---

# 15. Content Workflow

Official content lifecycle:

PENDING
→ DRAFTED
→ optional REVISE
→ DRAFTED
→ validation
→ APPROVED

---

## 15.1 Content drafting

Content must be based only on verified data.

Do not invent missing facts.

Default generated drafts must be customer-facing.

Never put internal workflow text into storefront content.

Forbidden examples:

"manager must review this before publishing"

"pending manager review"

"this description should be completed later"

Internal workflow instructions belong in logs/review fields, not customer-facing descriptions.

---

## 15.2 REVISE workflow

Use the official:

prepare_product_content.py --action REVISE

for human or deterministic reviewer edits.

REVISE must:

- target an exact existing DRAFTED row
- update in place
- never create a duplicate row
- preserve omitted fields
- keep content_status = DRAFTED
- keep review_required = true until final validation/approval
- refuse APPROVED/REJECTED rows

Do not use scratch scripts or ad-hoc SQL for normal content revision.

---

## 15.3 Automated content approval

Stable automation may approve content automatically when deterministic validation confirms:

- identity facts match verified data
- no unsupported claims
- no invented metadata
- no unresolved factual ambiguity
- no internal workflow notes
- correct product type
- correct combo/individual-volume distinction
- content is not generic placeholder-only copy
- customer-facing Vietnamese is structurally valid

If validation fails or ambiguity remains:

STOP for human review.

Do not require human approval merely because the operation changes content_status to APPROVED.

---

# 16. Readiness

READY_FOR_DRAFT requires:

- IDENTITY_VERIFIED
- APPROVED content
- image aggregate approved
- exactly one selected main image
- selected image VALIDATED
- selected image publish eligible
- valid usage rights
- no blocking recovery state
- no existing created Woo product/sync conflict

Pricing may remain PENDING.

Missing ISBN or weight may remain warnings.

---

# 17. WooCommerce Draft Creation

Woo creation occurs only after explicit bounded human authorization.

Always create:

status = "draft"

Never include:

regular_price
sale_price
price

Use exact SKU.

Before POST:

- search remote by SKU
- verify no duplicate
- verify local sync state
- verify recovery state
- fresh re-read all readiness gates

---

## 17.1 Media handling

Reuse safely identifiable existing WordPress media when possible.

Preserve identifier contract:

product_images.id
→ source_image_id
→ wordpress_media_id
→ Woo payload image id

Do not confuse:

image_id
source_image_id
wordpress_media_id

Regression tests must protect this contract.

---

# 18. Woo Recovery

If media was uploaded but product creation stopped:

do not automatically upload duplicate media on retry.

Reconcile and reuse safely identifiable media.

If Woo create response is uncertain:

DO NOT RETRY.

Reconcile remote SKU/product state first.

If remote product is confirmed:

sync local state.

If remote product is confirmed absent:

retry may be allowed only after recovery state is explicitly resolved.

---

# 19. Batch Orchestrator

Primary stable orchestration entry point:

scripts/run_batch.py

The orchestrator should become the normal way to process batches.

Individual stage scripts remain authoritative writers.

---

## 19.1 Required bounded targeting

Batch execution must require:

explicit candidate allowlist
and
--max-candidates

No implicit:

all candidates
newest candidates
all pending candidates

for production writes.

---

## 19.2 Serial writes

Database-changing and external-side-effect stages execute serially.

For each candidate:

derive state
→ execute one valid next stage
→ re-read state
→ verify transition
→ audit checkpoint
→ continue

Do not execute production writers concurrently until concurrency has been explicitly designed and tested.

---

## 19.3 No blind retry

A failed or uncertain writer is not automatically retried.

The orchestrator must distinguish:

deterministic local failure
vs
uncertain remote side effect

Recovery state stops normal progression.

---

## 19.4 Automatic progression target

The orchestrator should continue automatically through deterministic safe stages until:

READY_FOR_DRAFT

It should stop earlier only for:

true ambiguity
recovery condition
hard safety failure

At READY_FOR_DRAFT:

request bounded human Woo draft authorization.

---

# 20. Audit

Run deterministic audit:

after meaningful writer transitions
after recovery
after Woo creation
at batch completion

Expected acceptable result:

PASS
or
PASS_WITH_WARNINGS

Warnings such as accepted ISBN_MISSING / WEIGHT_MISSING may continue.

Any ERROR stops the batch until investigated.

---

# 21. Pre-flight

Before stable production batch execution, run:

.venv/Scripts/python.exe scripts/preflight_pipeline.py

The preflight must be read-only.

Expected success:

READY_FOR_BATCH

It should verify:

- Python environment
- required configuration presence
- DB connectivity
- schema/domain constants compatibility
- pipeline audit
- Woo read connectivity where appropriate
- no known blocking recovery state
- Git state where available

Never print secrets.

---

# 22. Python Environment

Always use the repository virtual environment.

Windows:

.venv\Scripts\python.exe

Never rely on:

python
py
system Python

for production workflow commands.

---

# 23. UTF-8 Policy

TSYC content contains Vietnamese Unicode text.

CLI bootstrap must configure Windows streams consistently.

stdin:
UTF-8
errors = strict

stdout/stderr:
UTF-8
errors = replace

Subprocess capture:
encoding = UTF-8
errors = replace

Do not require the operator to manually set PYTHONIOENCODING for normal production use.

---

# 24. Git Discipline

Before commit:

git status
git diff --check
review exact diff
run relevant tests

Do not commit:

.env
credentials
browser profiles
cookies
auth state
temporary raw images
temporary reviewer JSON
secrets

Prefer small focused commits.

Do not perform destructive Git operations without explicit need.

---

# 25. Test Policy

Every production defect discovered during pilot/stable operation should receive a regression test whenever reasonably possible.

Required protected areas include:

- Facebook U+034F normalization
- UTF-8 stdin/stdout/stderr
- UTF-8 subprocess capture
- candidate dedupe
- reference source provenance
- source_url_id invariant
- rights-status enum consistency
- main-image uniqueness
- image rights gating
- Minh Khai image extraction fallback
- content REVISE workflow
- APPROVED-content overwrite protection
- Woo image identifier contract
- WordPress media reuse
- Woo draft-only payload
- no-price payload
- duplicate SKU prevention
- no-blind-retry
- orchestrator dispatch/stop behavior
- recovery behavior

The offline pytest suite must not access live:

Supabase
WooCommerce
Facebook
Playwright
credentials

unless a test is explicitly categorized as a manual/integration check and excluded from normal pytest collection.

---

# 26. Stable Operating Mode

Normal production flow should be:

1. Run preflight.
2. Select explicit bounded candidate batch.
3. Run orchestrator.
4. Let deterministic stages auto-progress.
5. Stop only if true ambiguity/recovery occurs.
6. Continue until READY_FOR_DRAFT.
7. Ask user once for Woo draft authorization.
8. Create authorized drafts serially.
9. Reconcile.
10. Audit.
11. Stop.

The user should not be required to manually coordinate intermediate stage scripts.

---

# 27. Stable Batch Scaling

Do not immediately jump from pilot to very large unattended batches.

Recommended progression:

first stable batch: 5 candidates
next: 5–10
next: 10

If several consecutive batches run without production hotfixes, batch size may be increased gradually.

Keep exact allowlist and max-candidate safeguards.

---

# 28. Definition of Success

A stable automated candidate should reach:

identity_status = IDENTITY_VERIFIED
content_status = APPROVED
image_status = APPROVED
woocommerce_status = READY_FOR_DRAFT

without requiring routine human intervention.

Then:

HUMAN AUTHORIZATION FOR WOO DRAFT

After authorization:

remote Woo status = draft
local Woo status = DRAFT_CREATED
recovery_required = false
no price set by automation
no duplicate SKU
correct validated image attached

Then:

reconciliation
audit
STOP

Publishing and final selling-price decisions remain outside this automation.

---

# 29. Final Decision Rule

When deciding whether to ask the user for approval, use this question:

"Does this step require a genuine business judgment that cannot be resolved by existing verified evidence and established TSYC policy?"

If NO:

execute the bounded deterministic pipeline action automatically.

If YES:

stop and request the minimum necessary human decision.

Do NOT ask merely because:

- a database is being read
- a database row is being inserted
- a bounded pipeline row is being updated
- a deterministic status transition is being performed
- an approved TSYC script is being run

The goal is safe automation, not approval fatigue.