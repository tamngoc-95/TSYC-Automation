# TSYC Historical Automation Controller Policy

**Effective:** 2026-09-22  
**Purpose:** Formal handoff infrastructure between Cowork (orchestration) and Claude Code (production execution)

---

## 1. ROLE SPLIT

### Cowork (Controller Layer)

- **Orchestration**: Read current state, classify backlog, select next safe action
- **Planning**: Generate bounded candidate allowlists, plan execution sequences
- **Review**: Analyze execution reports, classify results (PASS/PARTIAL/STOPPED)
- **Report analysis**: Parse audit/preflight, verify safety conditions
- **Human queue management**: Escalate content/image/identity review cases
- **Scale-up decision**: Determine when to increase batch size (5→10→20→20-repeats)

**Permissions:**
- Read all repository state, database queries, candidate metadata
- Read-only analysis of Supabase (via MCP if available)
- Generate planning reports and execution briefs
- Read execution reports from Claude Code
- **NO production writes** to any system

### Claude Code (Production Execution Engine)

- **Only production writer**: Single process, no parallel writes
- **Supabase execution**: Run all SELECT/INSERT/UPDATE for candidates
- **WooCommerce execution**: Create/sync drafts, reconcile state
- **Python/tests**: Execute scripts in isolated .venv
- **Audit/preflight**: Run validation before each batch
- **State/report persistence**: Write execution reports and snapshots

**Permissions:**
- Execute all production mutations
- Access .env credentials
- Write to data/processed/orchestration/ (reports only)
- Run Python scripts with full dependencies
- **NO human decisions**: Receive allowlists and execute only what's listed

---

## 2. SINGLE-WRITER RULE

**Invariant:** Only one Claude Code production process may run at a time.

- Cowork read-only analysis may run separately
- No concurrent Supabase writes
- No concurrent WooCommerce API calls to same product
- Allowlist must be atomic and pre-approved before Claude Code starts
- Claude Code waits for previous execution to complete and publish report

---

## 3. STATE MACHINE

Each automation cycle follows:

```
[Cowork]  READ_STATE
↓
[Cowork]  CLASSIFY_BACKLOG (automatable / human-review / recovery / conflict / terminal)
↓
[Cowork]  SELECT_NEXT_ACTION (5 to 20 candidates)
↓
[Cowork]  GENERATE_BOUNDED_ALLOWLIST
↓
[Cowork→Claude Code]  "Execute FB-HIST-2026-001-CAN-[0001..0020]"
↓
[Claude Code]  EXECUTE_WITH_CLAUDE_CODE (run_batch.py)
↓
[Claude Code]  AUDIT (audit_pipeline_state.py)
↓
[Claude Code]  PREFLIGHT (preflight_pipeline.py)
↓
[Claude Code]  PERSIST_REPORT (latest_execution_report.json)
↓
[Cowork]  CLASSIFY_RESULT (PASS / PARTIAL / STOPPED)
↓
[Cowork]  SCALE_OR_QUEUE (determine next batch or escalate)
```

---

## 4. RESULT TYPES

### PASS
- All candidates in allowlist completed successfully
- Audit: zero errors
- Preflight: READY_FOR_BATCH
- No global invariant violation
- No unexpected recovery state
- Safe to proceed with same or larger batch

### PARTIAL
- Some candidates progressed, others gated to human review
- Candidate-specific gates:
  - CONTENT_REVIEW_REQUIRED
  - IMAGE_REVIEW_REQUIRED
  - IMAGE_GROUP_OWNERSHIP_AMBIGUOUS
  - IDENTITY_CONFLICT
  - EDITION_REVIEW_REQUIRED
  - SELLABLE_UNIT_AMBIGUITY
- These do NOT block unrelated candidates
- Cowork escalates to review queue, Cowork continues with next batch

### STOPPED
- Global invariant failure detected
- Audit error or preflight BLOCKED
- System/auth/database correctness failure
- Candidate outside allowlist was touched
- Non-historical candidate was touched
- Unexpected recovery state required
- Requires human intervention before resuming

---

## 5. SCALE POLICY

**Scale progression:** 5 → 10 → 20 → repeated 20-candidate runs

**Scale only when all conditions are met:**
- Previous execution result = PASS
- Audit errors = 0
- Preflight status = READY_FOR_BATCH
- No global invariant failure
- No unexpected recovery state
- Zero price writes (regular_price, sale_price, price)
- Zero publish actions
- No candidate outside allowlist touched

**De-scale:**
If PARTIAL or STOPPED, return to smaller batch size (20→10→5) until root cause addressed.

---

## 6. HUMAN QUEUES

These queues are independent and never block unrelated candidates:

1. **CONTENT_REVIEW_REQUIRED**
   - Description/metadata needs human validation
   - Candidate waits in queue; other candidates proceed

2. **IMAGE_REVIEW_REQUIRED**
   - Image rights or quality issues
   - Candidate waits; others proceed

3. **IMAGE_GROUP_OWNERSHIP_AMBIGUOUS**
   - Multiple candidates claim same image
   - Await manual disambiguation

4. **IDENTITY_CONFLICT**
   - ISBN/metadata conflict with existing product
   - Await verification from publisher/supplier

5. **RECOVERY_REVIEW**
   - WooCommerce sync recovery needed
   - Manual reconciliation before retry

6. **EDITION_REVIEW_REQUIRED**
   - Edition metadata ambiguous
   - Await clarification

7. **SELLABLE_UNIT_AMBIGUITY**
   - Bundle vs. single product unclear
   - Await clarification

---

## 7. GLOBAL STOP CONDITIONS

**Any of these trigger STOPPED result and halt the batch:**

1. **Audit error**: Unrecoverable data integrity issue
2. **Preflight BLOCKED**: Safety checks failed
3. **System/auth/database correctness failure**: Connection lost, credential invalid, corruption detected
4. **Candidate outside allowlist touched**: Script modified data outside pre-approved list
5. **Non-historical candidate touched**: Historical pipeline modified non-FB candidate
6. **Duplicate natural key introduced**: Same ISBN/metadata as existing product
7. **Accidental image association**: Image linked to wrong candidate
8. **Uncertain WooCommerce create without sanctioned recovery**: Draft creation ambiguous, recovery state unclear
9. **Price mutation**: Any regular_price, sale_price, or price write
10. **Publish action**: Any attempt to move draft→publish
11. **Destructive/irreversible action outside policy**: Deletion, permanent data loss
12. **Core invariant violation**: Identity semantics, ownership rules, authorization rules

---

## 8. SAFETY INVARIANTS

**Claude Code must never:**

- Auto-publish a product (status must remain `draft`)
- Change `regular_price`, `sale_price`, or `price` fields
- Blind-retry uncertain WooCommerce create operations
- Fabricate metadata (ISBN, author, publisher) from assumptions
- Overwrite verified evidence with unverified data
- Weaken identity semantics (conflate different editions/versions)
- Perform raw SQL recovery without explicit human authorization
- Dynamically expand an approved allowlist
- Mutate candidates not in the pre-approved allowlist
- Modify non-historical candidates from the historical pipeline

---

## 9. NORMAL WORKFLOW

### Step 1: Cowork reads state
```
export_historical_orchestration_state.py --status
```
Returns: historical total, automatable_now, human_review, recovery_review, conflict, terminal

### Step 2: Cowork classifies backlog
- Count candidates in each category
- Identify ready-for-automation subset
- Check for blocking conditions

### Step 3: Cowork selects next action
- Apply scale policy (5/10/20)
- Build allowlist of candidate codes
- Verify no global stop conditions

### Step 4: Cowork instructs Claude Code
```
"Execute batch FB-HIST-2026-001 with candidates: [CAN-0001..CAN-0020]"
```

### Step 5: Claude Code executes
```
.venv\Scripts\python.exe scripts/select_historical_batch.py --limit 20 --candidate-codes FB-HIST-2026-001-CAN-[0001..0020]
.venv\Scripts\python.exe scripts/run_batch.py --batch-id FB-HIST-2026-001-EXEC-20260922-001
.venv\Scripts\python.exe scripts/audit_pipeline_state.py
.venv\Scripts\python.exe scripts/preflight_pipeline.py
```

### Step 6: Claude Code persists report
```
data/processed/orchestration/latest_execution_report.json
```

### Step 7: Cowork analyzes report
- Parse execution_report.json
- Classify result: PASS / PARTIAL / STOPPED
- Extract human queues
- Determine next action

### Step 8: Cowork decides scale
- PASS + audit=0 + preflight=READY → scale up
- PARTIAL → continue with next batch (don't block on gated)
- STOPPED → escalate to human, await fix, retry same batch

---

## 10. TRANSITION RULES

### PASS → Next action
- If scaled batch: continue with same size
- If limit not reached: consider scaling (5→10, 10→20)
- If 20 batch: run another 20-candidate batch
- If backlog exhausted: complete

### PARTIAL → Next action
- Extract human-gated candidates to review queues
- Cowork queues for content/image/identity review
- Continue with next batch (non-gated candidates in backlog)
- Do NOT block on gated candidates

### STOPPED → Next action
- Stop execution
- Log global stop reason
- Escalate to human with detailed error context
- Await root cause fix (data correction, script fix, etc.)
- Rerun same batch once fixed

---

## 11. EXECUTION REPORT FORMAT

Claude Code writes to:
```
data/processed/orchestration/latest_execution_report.json
```

See schema in Phase 3 below.

---

## 12. STATE EXPORT SNAPSHOT

Cowork reads/updates:
```
data/processed/orchestration/historical_state.json
```

This file is a handoff snapshot only—not the source of truth.
Database and pipeline state remain authoritative.

---

## Version Control

- Do NOT commit `.env` files
- Commit `CLAUDE_AUTOMATION.md` (this policy)
- Commit orchestration state templates and exporters
- Commit test suites
- Do NOT commit execution reports to main branch (may be temporary logs only)

