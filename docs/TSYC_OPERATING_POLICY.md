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
- Publishable rights:
  - STORE_OWNED
  - PUBLISHER_AUTHORIZED
  - SUPPLIER_AUTHORIZED
  - LICENSED
- RIGHTS_UNKNOWN and DO_NOT_USE are not publishable.

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
