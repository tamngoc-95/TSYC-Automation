# TSYC Automation — Project Instructions

## Mission
Operate the Tiệm Sách Yêu Con automation pipeline safely, reproducibly, and audibly.

## Repository
Windows project root:
`C:\Users\tamng\Documents\TSYC-Automation`

## Python runtime
Always use the repository virtual environment.

PowerShell:
`\.\.venv\Scripts\python.exe`

Claude Code Bash:
`.venv/Scripts/python.exe`

Never fall back to system Python when `.venv` exists.

## Golden principles
1. Never publish WooCommerce products automatically.
2. Never treat Fahasa or a public bookstore price as purchase price.
3. Purchase price priority:
   1. Purchase invoice
   2. Confirmed purchase order
   3. Supplier quotation
   4. Current supplier price list
4. Reference identity priority:
   1. Publisher
   2. Authorized supplier
   3. Reliable bookstore
   4. Fahasa
   5. Facebook
5. Missing ISBN or weight is a warning, not a blocker for WooCommerce draft creation.
6. Do not overwrite APPROVED content or finalized identity evidence silently.
7. Do not retry WooCommerce draft creation if a remote draft may already exist.
8. Run integrity audit after every meaningful batch or recovery action.
9. Work serially unless database concurrency controls are explicitly introduced.
10. Never modify `.env`, browser auth/cookies, secrets, or credentials unless the user explicitly requests it.

## Required pipeline order
1. `collect_one_facebook_post.py`
2. `clean_facebook_raw_pages.py`
3. `create_candidates_from_cleaned_posts.py`
4. `upload_facebook_images_to_supabase.py`
5. `register_reference_source.py`
6. `collect_reference_metadata.py`
7. `manual_create_product_reference.py` only when required
8. `match_candidate_identity.py`
9. `create_internal_product.py`
10. `review_product_images.py`
11. `prepare_product_content.py`
12. `check_draft_readiness.py`
13. `create_woocommerce_draft.py`
14. `sync_woocommerce_product_status.py`
15. `audit_pipeline_state.py`

## Mandatory gates
Before `create_internal_product.py`:
- candidate identity must be `IDENTITY_VERIFIED`
- a valid `MATCH` reference must exist

Before content approval:
- generic metadata-only content must not be approved
- reviewed/enriched content is required

Before image approval:
- exactly one selected main image
- `image_status = VALIDATED`
- `is_publish_eligible = True`
- usage rights must be explicit and publishable

Before WooCommerce draft:
- `woocommerce_status = READY_FOR_DRAFT`
- content `APPROVED`
- image `APPROVED`
- exactly one valid selected main image
- no duplicate SKU
- payload status must remain `draft`
- no `regular_price`, `sale_price`, or `price` field

## Audit rule
Run with the project virtual environment:

PowerShell: `\.\.venv\Scripts\python.exe scripts\audit_pipeline_state.py`

Claude Code Bash: `.venv/Scripts/python.exe scripts/audit_pipeline_state.py`

Accept:
- `PASS`
- `PASS_WITH_WARNINGS` when warnings are only accepted metadata gaps such as ISBN/weight

Stop:
- any `ERROR`
- any Woo recovery marker
- any identity/reference mismatch
- any multiple-main-image condition
- any SKU mismatch

## Git discipline
Before edits:
- `git status`

Before commit:
- compile changed Python files
- run pipeline audit
- `git diff --cached --check`

Never commit:
- `.env`
- Facebook profile/session data
- cookies
- secrets

## Human approval boundaries
Human approval is required for:
- changing business pricing rules
- approving ambiguous identity conflicts
- approving image rights when evidence is uncertain
- publishing WooCommerce products
- deleting production records
- changing database constraints/schema
- destructive Git operations
- changing secrets/credentials

## Communication
For code, logs, comments, errors, and technical artifacts: use English.
For explanations to the owner: Vietnamese is acceptable.
