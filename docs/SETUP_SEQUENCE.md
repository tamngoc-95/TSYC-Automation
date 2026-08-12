# Setup Sequence

## Phase 1 — Claude Desktop / Cowork
1. Install/update Claude Desktop.
2. Open Cowork.
3. Select only the TSYC repository folder as the working folder.
4. Prefer read-only or read-write-no-delete while validating the setup.
5. Do not expose unrelated personal folders.
6. Do not add connectors that are not required.

## Phase 2 — Repository instructions
Copy into repo:
- `CLAUDE.md`
- `docs/`
- `prompts/`
- `checklists/`
- `.claude/rules/tsyc-safety.md`

Commit these as documentation/configuration only.

## Phase 3 — First Cowork session
Paste `prompts/00_COWORK_BOOTSTRAP.md`.
Require audit-only behavior first.
Do not run a new product on the first session.

## Phase 4 — Controlled pilot
Choose one new Facebook post.
Run one stage at a time.
After each write stage:
- verify DB
- run audit
- report

## Phase 5 — Repeated operations
Use `03_BATCH_ORCHESTRATION.md` only after the controlled pilot completes with zero errors.

## Phase 6 — Claude Code hardening (optional but recommended)
For shell/code execution, use Claude Code project settings, permission rules, and hooks.
Keep dangerous/destructive commands blocked.
Use hooks for deterministic enforcement rather than relying only on instructions.


## Runtime validation
Before any agent pilot, verify the repository `.venv` exists and use it explicitly for audit/compile commands. Do not fall back to system Python.
