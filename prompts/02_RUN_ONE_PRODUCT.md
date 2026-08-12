# Run One Product Prompt

Runtime rule: use the repository `.venv`; never use system Python when `.venv` exists.

Operate on exactly one TSYC candidate/product.

Target:
`<CANDIDATE_CODE_OR_PRODUCT_CODE>`

Read the project instructions and runbook first.

Execute only the next valid pipeline stage for this target.
Do not skip gates.
Do not process another candidate.
After the stage:
1. verify the expected record changes,
2. run the integrity audit for the target if supported, otherwise the full audit,
3. report before/after statuses,
4. stop if any ERROR appears.

Do not publish WooCommerce.
Do not set selling price automatically.
Do not retry draft creation if a remote Woo product may already exist.
