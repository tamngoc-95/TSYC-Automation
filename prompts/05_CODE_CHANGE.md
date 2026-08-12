# Code Change Prompt

Runtime rule: use the repository `.venv`; never use system Python when `.venv` exists.

Make one bounded code change in the TSYC repository.

Before editing:
- read `CLAUDE.md`
- inspect `git status`
- identify affected pipeline stages
- preserve existing business rules

After editing:
1. compile changed Python files,
2. run relevant tests,
3. run `.venv/Scripts/python.exe scripts/audit_pipeline_state.py`,
4. run `git diff --check`,
5. summarize files changed and behavior changed.

Do not commit or push unless explicitly instructed.
Never modify `.env`, browser auth/session files, or secrets.
