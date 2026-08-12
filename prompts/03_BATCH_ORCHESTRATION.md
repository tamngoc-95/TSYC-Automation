# Batch Orchestration Prompt

Runtime rule: use the repository `.venv`; never use system Python when `.venv` exists.

Process batch `<BATCH_CODE>` serially.

Before execution:
- read all project instructions/runbooks
- run pre-flight audit
- enumerate exact candidate/source scope
- identify any existing ERROR or recovery marker

For each candidate:
1. determine its current stage from database state,
2. execute only the next valid stage,
3. verify the result,
4. run integrity checks,
5. continue only when the candidate remains error-free.

Do not parallelize candidate-code creation or other database writes that may race.
Do not auto-resolve identity conflicts.
Do not auto-approve uncertain image rights.
Do not auto-publish WooCommerce.

At the end return a table:
candidate_code | stage_before | action | stage_after | warnings | errors | next_action
