TSYC Automation Controller

1. Purpose

This file defines the operating rules for the TSYC automation system.

The system uses a two-tier architecture:

Cowork / Claude Desktop = orchestration, planning, prioritization, review, and reporting

Claude Code = production execution engine and single production writer

The primary business objective is:

Publish the safest, most automatable products first.

Do not optimize for recovering every historical Facebook product before publishing usable products.

Historical recovery remains an important feeder into the production pipeline, but it is lower priority than progressing already-automatable products toward WooCommerce drafts.

2. System Architecture

2.1 Cowork / Claude Desktop

Cowork is the controller layer.

Cowork may:

inspect repository files

inspect CLAUDE.md

inspect this file

inspect persisted orchestration state

inspect execution reports

classify the backlog

determine the next safest action

prioritize Fast Track candidates

prepare exact bounded Claude Code prompts

review Claude Code results

maintain human-review queues

maintain recovery-review queues

maintain conflict queues

decide whether scale-up is safe

coordinate multilingual content

report progress

Cowork must NOT:

write directly to Supabase

create or modify WooCommerce products

execute production run_batch.py

perform production candidate writes

set or modify selling prices

publish products

perform raw SQL repair

bypass human gates

fabricate metadata

weaken identity semantics

silently overwrite verified data

Cowork status:

CONTROLLER_ONLY_READY

2.2 Claude Code

Claude Code is the production execution engine.

Claude Code is responsible for:

production Python execution

Supabase writes

WooCommerce writes

internal product creation

reference collection

image extraction / upload / review

content preparation writes

multilingual content persistence

Woo draft creation

pipeline state transitions

tests

audit

preflight

execution report persistence

orchestration state export

Claude Code must follow:

CLAUDE.md

this file

explicit candidate allowlists

batch-size limits

audit/preflight rules

no-price/no-publish rules

3. Single-Writer Rule

Only one Claude Code production process may perform production writes at a time.

Never run multiple production writers concurrently.

Read-only analysis may run in parallel.

Before starting a production run, verify that no other production Claude Code process is already writing to:

Supabase

WooCommerce

candidate state

internal products

image state

content state

If concurrent production activity is suspected:

STOP

and resolve the writer conflict first.

4. Primary Business Strategy

The previous strategy was:

recover all historical products -> then publish

The current strategy is:

publish safest automatable products first

Target production flow:

AUTOMATABLE PRODUCT
    ↓
identity / sellable unit sufficiently clear
    ↓
internal product
    ↓
candidate-specific images
    ↓
accurate Vietnamese content
    ↓
English translation
    ↓
German translation
    ↓
multilingual consistency validation
    ↓
WooCommerce DRAFT
    ↓
shop owner price review
    ↓
final human review
    ↓
manual publish

Historical recovery continues only as a feeder into this Fast Track pipeline.

5. Operational Priority Order

Always prioritize work in the following order unless a global safety issue requires otherwise.

Priority 1 — READY_FOR_DRAFT

Highest priority.

Candidates already ready for WooCommerce draft creation should be processed first.

Requirements normally include:

sellable unit clear

no unresolved identity contradiction

images approved

content acceptable

multilingual content ready when multilingual workflow applies

no recovery state

no conflict

no human-review blocker

Endpoint:

WooCommerce DRAFT

Never publish automatically.

Priority 2 — MULTILINGUAL_CONTENT

Products that are otherwise close to publication but need content completion should be processed before recovering more historical products.

Required language flow:

Vietnamese canonical content
    ↓
Vietnamese factual validation
    ↓
English translation/localization
    ↓
German translation/localization
    ↓
cross-language consistency validation
    ↓
MULTILINGUAL_CONTENT_READY

Priority 3 — FAST_TRACK

Process candidates with the highest deterministic chance of reaching Woo draft without human intervention.

Prefer candidates with:

clear identity

clear sellable unit

deterministic image provenance

valid image rights

usable content or deterministic enrichment path

no recovery state

no conflict

no known human gate

Priority 4 — DETERMINISTIC ENRICHMENT

Process deterministic steps that directly unlock publication.

Examples:

supported reference metadata collection

deterministic historical image ownership resolution

content revision using verified reference data

missing language translation

deterministic content validation

Only perform enrichment when it improves Fast Track throughput.

Priority 5 — HUMAN REVIEW

Maintain human-review queues, but do not allow them to block unrelated candidates.

Examples:

CONTENT_REVIEW_REQUIRED

IMAGE_REVIEW_REQUIRED

IMAGE_GROUP_OWNERSHIP_AMBIGUOUS

IDENTITY_CONFLICT

EDITION_REVIEW_REQUIRED

sellable-unit ambiguity

Priority 6 — RECOVERY REVIEW

Woo uncertainty/recovery cases are isolated from unrelated candidates.

Examples:

CREATE_RESULT_UNCERTAIN

stale remote Woo state

sanctioned recovery review

Never blind-retry uncertain Woo creation.

Priority 7 — HISTORICAL RECOVERY

Recover additional historical products only when:

Fast Track pool is depleted, or

recovery is explicitly requested, or

new product supply is needed

Do not attempt to recover 100% of historical Facebook products before publishing existing automatable products.

6. Operational Lanes

Every relevant candidate should be classified into one operational lane.

LANE A — READY_FOR_DRAFT

Candidate is ready for Woo draft creation.

LANE B — MULTILINGUAL_CONTENT

Candidate has sufficient identity/images but needs VI/EN/DE content completion.

LANE C — FAST_TRACK

Candidate appears capable of deterministic progression toward Woo draft.

LANE D — ENRICHMENT_NEEDED

One or more deterministic enrichment steps can likely unlock Fast Track.

LANE E — HUMAN_REVIEW

Candidate needs genuine human judgment.

LANE F — RECOVERY_REVIEW

Candidate needs sanctioned Woo recovery review.

LANE G — CONFLICT

Candidate has identity, sellable-unit, edition, or other contradiction.

LANE H — TERMINAL

Candidate is already reconciled/completed or requires no further automatic processing.

LANE I — HISTORICAL_RECOVERY_BACKLOG

Historical source/post has not yet been fully recovered.

This is the lowest routine priority.

7. Historical Low-Touch Policy

Historical candidates use a lower-friction migration policy.

Goal:

WooCommerce DRAFT

not perfect canonical identity.

Historical progression may continue when:

title is meaningful

sellable unit is sufficiently clear

at least one validated candidate-relevant image exists

description is usable and customer-facing

no known contradiction proves the product is wrong

Missing optional metadata must not block historical draft creation by itself.

Examples of optional enrichment:

ISBN

author

publisher

page count

weight

dimensions

second reference

These remain warnings unless their absence creates a real identity/sellable-unit problem.

Do not weaken strict live-workflow semantics.

8. Image Rules

8.1 Candidate-specific ownership

Do not assign every image from a Facebook source post to every candidate.

Use candidate-specific persisted provenance.

Valid evidence may include:

exact image id

exact local media path

persisted MANUAL_VISUAL_REVIEW

persisted candidate-image association

explicit multi-product image evidence

8.2 Multi-image model

A candidate may use:

exactly one deterministic PRIMARY image

zero or more validated GALLERY images

Model:

Candidate
    ├── PRIMARY
    └── GALLERY [0..N]

Use all validated candidate-relevant images.

Do not cap to one image unnecessarily.

8.3 Primary selection

Primary image priority:

explicitly marked cover/primary

explicitly isolated candidate image

highest-confidence persisted candidate-specific image

deterministic first validated image

If multiple images are equally valid and all clearly belong to the same candidate:

select PRIMARY deterministically

place remaining images in GALLERY

Do not create an unnecessary human gate only because several valid images exist.

8.4 Multi-product shared images

One image may legitimately map to multiple candidates only when persisted evidence explicitly confirms multiple distinct sellable products in the image.

Do not treat documented multi-product sharing as accidental duplication.

If multiple candidates claim one image without explicit supporting evidence:

HUMAN_REVIEW

8.5 Image rights

Historical rights policy:

STORE_OWNED → allowed

PUBLISHER_APPROVED → allowed

SUPPLIER_APPROVED → allowed

unknown/unconfigured → human review

Rights approval does not prove product relevance.

Both are required:

rights acceptable

candidate relevance supported

9. Multilingual Content Strategy

9.1 Canonical language

Vietnamese is the default semantic source of truth.

Flow:

verified facts
    ↓
Vietnamese canonical description
    ↓
Vietnamese factual validation
    ↓
English localization
    ↓
German localization
    ↓
cross-language consistency check

Do not independently invent three unrelated descriptions.

9.2 Content package

Recommended structure:

{
  "candidate_code": "",
  "product_title": "",
  "sellable_unit": "",
  "description_vi": "",
  "description_en": "",
  "description_de": "",
  "short_description_vi": "",
  "short_description_en": "",
  "short_description_de": "",
  "content_source": "",
  "facts_used": [],
  "content_status_vi": "",
  "content_status_en": "",
  "content_status_de": "",
  "multilingual_status": ""
}

Suggested states:

CONTENT_VI_READY

CONTENT_EN_READY

CONTENT_DE_READY

MULTILINGUAL_CONTENT_READY

9.3 Anti-fabrication

Never invent:

ISBN

author

publisher

age recommendation

educational benefit

plot summary

page count

dimensions

edition

weight

awards

stock promises

shipping promises

unless supported by persisted evidence or approved reference data.

9.4 Historical minimum content

Historical content may proceed when:

title meaningful

sellable unit clear

description understandable

image approved

no contradiction

Optional enrichment does not block historical draft-safe progression.

If content is genuinely unusable and there is no safe enrichment path:

CONTENT_REVIEW_REQUIRED

10. WooCommerce Multilingual Rule

Before implementing multilingual Woo writes, determine the website's actual multilingual mechanism.

Possible mechanisms may include:

WPML

Polylang

another multilingual plugin/system

Do not assume.

Preferred outcome:

Vietnamese product
    ↕
English translation
    ↕
German translation

Do not concatenate all three languages into a single description unless the site's actual architecture requires that.

11. Pricing Policy

Pricing must never block historical Woo draft creation.

Automation must NOT:

set regular_price

set sale_price

set price

change existing selling price

The shop owner handles final price review.

12. Publishing Policy

Never auto-publish.

Allowed endpoint:

WooCommerce DRAFT

Final publication requires explicit human decision.

Automation must not send:

status = publish

13. Candidate Selection

Candidate eligibility must be derived from:

current database state

pipeline_state.py

selector logic

persisted evidence

audit/preflight state

Do not invent candidate eligibility.

Exclude from normal Fast Track:

HUMAN_REVIEW

RECOVERY_REVIEW

CONFLICT

TERMINAL

known unresolved human gates

uncertain Woo create state

Use deterministic ordering.

Prefer oldest-first unless a more specific business priority is explicitly defined.

14. Batch Scaling Policy

Use controlled ramp-up:

5
→ 10
→ 20
→ repeated 20-candidate batches

Scale only when previous execution confirms:

audit errors = 0

preflight = READY_FOR_BATCH

no global invariant failure

no unexpected recovery state

no price writes

no publish actions

candidate-specific failures isolated correctly

Candidate-specific human gates do not require resetting the ramp automatically.

15. Candidate-Specific Isolation

A candidate-specific failure/gate must:

stop that candidate

preserve evidence

record exact reason

not stop unrelated candidates

Examples:

CONTENT_REVIEW_REQUIRED

IMAGE_REVIEW_REQUIRED

IMAGE_GROUP_OWNERSHIP_AMBIGUOUS

unsupported reference

sellable-unit ambiguity

identity conflict

These are not global failures unless they reveal a system-wide invariant problem.

16. Global Stop Conditions

Stop the entire production workflow immediately for:

audit error

preflight BLOCKED

authentication/system/database failure affecting correctness

candidate outside explicit allowlist touched

non-approved workflow candidate touched

duplicate natural key introduced

accidental image association

uncertain Woo create without sanctioned recovery

selling-price mutation

publish action

destructive/irreversible action outside policy

verified data overwritten unexpectedly

raw SQL repair required

core invariant violation

concurrent production writer detected

Accepted historical warnings are not global stop conditions.

17. Human Review Queues

Maintain queues instead of interrupting Fast Track.

Recommended decision types:

REVIEW_MINIMAL_CONTENT

SELECT_MAIN_IMAGE

CONFIRM_IMAGE_OWNERSHIP

CONFIRM_IDENTITY

CONFIRM_SELLABLE_UNIT

REVIEW_EDITION_VARIANT

REVIEW_WOO_RECOVERY

Group similar human-review cases where practical.

18. Controller Cycle

Every Cowork cycle should follow this state machine.

STEP 1 — READ STATE

Read:

CLAUDE.md

CLAUDE_AUTOMATION.md

orchestration state

latest execution report

latest audit

latest preflight

relevant repository state

Recommended runtime files:

data/processed/orchestration/historical_state.json
data/processed/orchestration/latest_execution_report.json

Database remains source of truth.

Runtime state files are resumability aids only.

STEP 2 — CLASSIFY BACKLOG

Count:

READY_FOR_DRAFT

MULTILINGUAL_CONTENT

FAST_TRACK

ENRICHMENT_NEEDED

HUMAN_REVIEW

RECOVERY_REVIEW

CONFLICT

TERMINAL

HISTORICAL_RECOVERY_BACKLOG

STEP 3 — CHOOSE NEXT ACTION

Use this priority:

READY_FOR_DRAFT
→ MULTILINGUAL_CONTENT
→ FAST_TRACK
→ ENRICHMENT_NEEDED
→ high-value HUMAN_REVIEW
→ HISTORICAL_RECOVERY

Never choose new historical recovery while higher-priority Fast Track work exists unless explicitly requested.

STEP 4 — CREATE BOUNDED PLAN

Use deterministic selection.

Prepare exact bounded candidate allowlist.

Never dynamically expand the production allowlist after execution begins.

STEP 5 — GENERATE CLAUDE CODE PROMPT

Cowork generates one exact execution prompt containing:

exact objective

exact allowlist or deterministic selection rule

batch limit

--non-interactive

exclusion rules

candidate isolation rules

global stop conditions

audit requirement

preflight requirement

final report contract

STEP 6 — CLAUDE CODE EXECUTION

Claude Code executes the production task.

Production writes are serialized.

STEP 7 — AUDIT

Run:

scripts/audit_pipeline_state.py

STEP 8 — PREFLIGHT

Run:

scripts/preflight_pipeline.py

STEP 9 — PERSIST EXECUTION REPORT

Write machine-readable execution result to:

data/processed/orchestration/latest_execution_report.json

Do not store secrets.

Do not commit runtime execution reports unless policy explicitly changes.

STEP 10 — CLASSIFY RESULT

Result types:

PASS

PARTIAL

STOPPED

Definitions:

PASS

Execution completed safely without unexpected candidate-specific blockers.

PARTIAL

At least one candidate progressed or was safely handled, but one or more candidate-specific human gates occurred.

STOPPED

Global stop condition triggered.

STEP 11 — SCALE OR QUEUE

If PASS/PARTIAL with:

audit errors = 0

preflight = READY_FOR_BATCH

safety invariants intact

then:

queue human-gated candidates

continue unrelated Fast Track work

scale according to ramp policy when appropriate

19. Execution Report Contract

Recommended structure:

{
  "execution_id": "",
  "started_at": "",
  "completed_at": "",
  "candidate_codes": [],
  "requested_count": 0,
  "attempted": 0,
  "progressed": 0,
  "unchanged": 0,
  "human_gated": 0,
  "failed": 0,
  "per_candidate": [],
  "writes": {
    "internal_products": 0,
    "reference_updates": 0,
    "content_updates": 0,
    "image_updates": 0,
    "woo_drafts": 0,
    "woo_reconciliations": 0,
    "price_writes": 0,
    "publish_actions": 0
  },
  "audit": {
    "status": "",
    "errors": 0,
    "warnings": 0
  },
  "preflight": {
    "status": ""
  },
  "global_stop": {
    "triggered": false,
    "reason": null
  },
  "safe_to_scale": false,
  "next_recommended_action": ""
}

20. Orchestration State Contract

Recommended runtime snapshot:

{
  "schema_version": 1,
  "updated_at": null,
  "repository_commit": null,
  "ready_for_draft": null,
  "multilingual_content": null,
  "fast_track": null,
  "enrichment_needed": null,
  "human_review": null,
  "recovery_review": null,
  "conflict": null,
  "terminal": null,
  "historical_recovery_backlog": null,
  "latest_execution": {
    "execution_id": null,
    "result": null,
    "candidate_codes": [],
    "attempted": 0,
    "progressed": 0,
    "unchanged": 0,
    "human_gated": 0,
    "failed": 0
  },
  "audit": {
    "status": null,
    "errors": null,
    "warnings": null
  },
  "preflight": {
    "status": null
  },
  "safety": {
    "price_writes": 0,
    "publish_actions": 0,
    "outside_allowlist_touched": 0,
    "non_historical_touched": 0
  },
  "next_recommended_action": null
}

The database and pipeline remain source of truth.

21. Cowork Response Contract

When execution is required, Cowork should return only:

NEXT ACTION

WHY

EXPECTED OUTCOME

EXACT CLAUDE CODE EXECUTION PROMPT

When reviewing a completed execution, Cowork should return only:

RESULT

STATE CHANGE

PRODUCTS MOVED CLOSER TO WOO DRAFT

NEW HUMAN / RECOVERY / CONFLICT ITEMS

SAFE TO SCALE

NEXT ACTION

Avoid unnecessary narrative.

22. Claude Code Execution Prompt Contract

Production prompts generated by Cowork should normally include:

EXECUTION MODE: MINIMAL-INTERRUPTION

Do not ask follow-up questions.
Do not pause for confirmation for safe, reversible, already-authorized operations.
Do not narrate intermediate steps.
Return only the final consolidated report.

They must also define:

task

candidate selection/allowlist

exclusions

max candidates

candidate-specific isolation

global stop conditions

audit

preflight

execution report

no-price rule

no-publish rule

23. Automatic Next-Action Rule

When the user says:

Continue TSYC automation

Cowork should decide automatically:

IF READY_FOR_DRAFT exists
    → prioritize Woo draft preparation

ELSE IF MULTILINGUAL_CONTENT exists
    → prioritize VI/EN/DE completion

ELSE IF FAST_TRACK exists
    → run next Fast Track batch

ELSE IF ENRICHMENT_NEEDED exists
    → run deterministic enrichment

ELSE IF high-value near-publish HUMAN_REVIEW exists
    → prepare grouped human-review task

ELSE
    → resume historical recovery

Do not ask the user to choose candidate codes manually when deterministic logic can do so.

24. Git and Runtime Files

Tracked policy/code files should be committed normally.

Runtime files should generally remain uncommitted/gitignored:

data/processed/orchestration/historical_state.json
data/processed/orchestration/latest_execution_report.json

Do not commit transient execution state unless explicitly required.

Do not run git add . when unrelated changes exist.

25. Git Lock Safety

If Git reports:

Unable to create '.git/index.lock': File exists

do not repeatedly retry.

Check:

whether another Git process is active

whether another Claude production process is active

Only delete .git/index.lock when no active Git operation is using it.

Concurrent writer detection is a global stop condition.

26. Reference Policy

Source priority:

PUBLISHER
> AUTHORIZED_SUPPLIER
> BOOKSTORE
> FAHASA
> FACEBOOK_POST
> OTHER

Fahasa may be used for:

identity reference

weight estimate

image reference

description reference

Fahasa must NOT be treated as official purchase-price source.

Reference discovery is enrichment for historical migration, not a mandatory blocker when historical draft-safe policy already permits progression.

27. Recovery Policy

Never blind retry uncertain Woo creation.

When Woo create result is uncertain:

stop automatic progression for candidate

inspect exact stored Woo id

exact SKU search

trash/status search

sanctioned recovery logic

human recovery review if uncertainty remains

Candidate-specific recovery cases must not block unrelated candidates.

28. Non-Historical / Live Workflow Protection

Historical low-touch rules must not silently weaken live/future workflows.

For non-historical candidates:

preserve stricter identity requirements

preserve reference requirements

preserve human gates

preserve publication policy

preserve pricing policy

Changes to historical automation must be scoped to historical candidates unless explicitly approved otherwise.

29. Safety Summary

Never:

auto-publish

set/change selling price

fabricate metadata

overwrite verified evidence

blind-retry uncertain Woo create

bypass human gates

perform raw SQL repair

assign unrelated images

expand production allowlists dynamically

run concurrent production writers

Always:

isolate candidate-specific failures

use explicit bounded batches

run audit

run preflight

persist execution report

maintain human/recovery/conflict queues

prioritize Fast Track products

keep final publish decision human-controlled

30. Strategic Principle

The controller must optimize for:

Get correct, safe, automatable products to WooCommerce DRAFT as quickly as possible.

Not:

Recover every historical product before publishing anything.

Preferred operating loop:

select best candidate
→ progress safely
→ complete multilingual content
→ create Woo draft
→ queue human exceptions
→ continue

Historical recovery is a feeder, not the main blocking objective.

31. Default Cowork Command

The standard user instruction may be as short as:

Continue TSYC automation using the Fast Track Master Controller.

Cowork must then:

read current state

classify backlog

choose highest-priority safe task

return exact Claude Code execution prompt

wait for execution report

review report

determine next action

32. Final Architecture

                TSYC MASTER CONTROLLER
                         │
                         ▼
               Claude Desktop / Cowork
          planning / selection / review
                         │
                         ▼
              bounded execution prompt
                         │
                         ▼
                    Claude Code
       production execution / single writer
                         │
               ┌─────────┴─────────┐
               ▼                   ▼
            Supabase           WooCommerce
               │                   │
               └─────────┬─────────┘
                         ▼
                 Audit + Preflight
                         │
                         ▼
              Execution Report / State
                         │
                         ▼
               Claude Desktop / Cowork
                         │
              PASS / PARTIAL / STOPPED
                         │
                         ▼
                   Next safe action