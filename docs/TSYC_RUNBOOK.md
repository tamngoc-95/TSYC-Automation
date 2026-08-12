# TSYC Production Runbook

## Standard Python runtime
PowerShell: `.\.venv\Scripts\python.exe`

Claude Code Bash: `.venv/Scripts/python.exe`

Never use system Python when `.venv` exists.

## Stage 0 — Pre-flight
Run:
- `git status`
- Python compile for any changed script
- `\.\.venv\Scripts\python.exe scripts\audit_pipeline_state.py`

Stop if audit has errors.

## Stage 1 — Collect Facebook post
Use `collect_one_facebook_post.py`.
Non-interactive runs must target one explicit source URL/ID and require target permalink evidence.

Verify:
- correct Facebook post
- raw page created
- source status updated correctly

## Stage 2 — Clean Facebook post
Use `clean_facebook_raw_pages.py`.

Verify:
- cleaned text is non-empty
- no silent overwrite unless explicitly forced

## Stage 3 — Create candidates
Use `create_candidates_from_cleaned_posts.py`.

Rules:
- one distinct sellable product -> one candidate
- multiple distinct products may produce multiple candidates
- a sellable combo/set remains one candidate
- do not auto-link all images when one post maps to multiple candidates

## Stage 4 — Upload images
Use `upload_facebook_images_to_supabase.py`.

Verify:
- image row has correct `candidate_id`
- duplicates do not cross-link candidates
- status remains pending until review

## Stage 5 — Register/collect references
Use:
- `register_reference_source.py`
- `collect_reference_metadata.py`
- manual reference creation only when needed

Verify:
- candidate/source linkage
- allowed source type
- authorized state
- no stale MATCH refresh
- public reference price is not purchase price

## Stage 6 — Match identity
Use `match_candidate_identity.py`.

Verify:
- conflicts are not auto-resolved
- missing ISBN/weight remains warning-only
- successful verification produces `IDENTITY_VERIFIED`

## Stage 7 — Create internal product
Use `create_internal_product.py`.

Verify:
- MATCH reference belongs to candidate
- missing ISBN/weight does not block
- image status is PENDING unless already truly approved

## Stage 8 — Review images
Use `review_product_images.py`.

Verify:
- exactly one selected main image
- rights explicit
- VALIDATED
- publish eligible
- internal product image status synchronized

## Stage 9 — Prepare content
Use `prepare_product_content.py`.

Verify:
- approved content is preserved
- generic safe draft is not approved
- internal/product content statuses match

## Stage 10 — Readiness
Use `check_draft_readiness.py`.

Blocking requirements:
- identity verified
- approved content
- approved image state
- exactly one selected publishable main image

Non-blocking:
- missing ISBN
- missing weight
- pricing not finalized

## Stage 11 — Create Woo draft
Use `create_woocommerce_draft.py`.

Verify:
- exact product selection
- no duplicate SKU
- payload status = draft
- no price fields
- remote product ID stored
- if local finalization fails: recovery marker, no retry

## Stage 12 — Reconcile Woo
Use `sync_woocommerce_product_status.py`.

Verify:
- remote SKU matches local SKU
- recovery markers cleared only after successful reconciliation
- remote price does not change internal pricing workflow status

## Stage 13 — Final audit
Run:
`\.\.venv\Scripts\python.exe scripts\audit_pipeline_state.py`

Only continue/close when:
- Errors = 0
- warnings are understood and accepted

## Stage 14 — Git
- `git status`
- `git diff --check`
- commit only tested changes
- push
