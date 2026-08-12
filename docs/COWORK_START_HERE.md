# Claude Cowork — TSYC Start Here

## Purpose
Use Cowork as the orchestration and review layer for the TSYC automation repository.

## Every new Cowork task must begin by reading
1. `CLAUDE.md`
2. `docs/TSYC_RUNBOOK.md`
3. `docs/TSYC_OPERATING_POLICY.md`
4. `checklists/PRE_RUN.md`

## Default mode
Start in audit/read mode. Do not write or execute production changes until the current repository state and database integrity are known.

## First actions in every session
1. Confirm project folder is the TSYC repository.
2. Read required instructions.
3. Inspect `git status`.
4. Confirm the virtual environment and project paths.
5. Run or review the latest `audit_pipeline_state.py` result.
6. State the exact candidate/product/batch scope before making changes.

## Safe operating pattern
Observe -> Plan -> Execute one bounded step -> Verify -> Audit -> Report.

## Never do
- never auto-publish WooCommerce
- never invent identity metadata
- never invent purchase price
- never treat warnings as facts
- never bulk-modify unknown records
- never retry a Woo draft if recovery is required
