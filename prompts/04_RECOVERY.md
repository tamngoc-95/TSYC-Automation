# Recovery Prompt

Runtime rule: use the repository `.venv`; never use system Python when `.venv` exists.

A TSYC workflow has failed or local/remote state may be inconsistent.

Operate in recovery mode.

Rules:
- do not rerun create operations first
- preserve existing remote IDs and evidence
- inspect the exact candidate/product/reference/image/content/Woo sync chain
- run integrity audit
- identify the earliest inconsistent transition
- propose the smallest reversible repair

If WooCommerce draft may already exist:
- do NOT create another draft
- reconcile using `sync_woocommerce_product_status.py`

Before any write, state:
1. observed state
2. expected state
3. exact repair
4. rollback plan
5. verification query/command

After repair:
- re-run integrity audit
- stop unless Errors = 0
