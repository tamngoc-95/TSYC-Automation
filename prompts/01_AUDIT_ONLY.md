# Audit-only Prompt

Runtime rule: use the repository `.venv`; never use system Python when `.venv` exists.

Operate in READ-ONLY audit mode.

Read the TSYC project instructions and run the integrity audit.
Also inspect Git status and the relevant records for the selected scope.

Do not:
- modify files
- modify Supabase data
- call WooCommerce write APIs
- upload images
- create candidates
- create references
- create products
- approve content/images
- commit or push

Return:
1. Scope checked
2. Integrity result
3. Errors
4. Warnings
5. Cross-table inconsistencies
6. Recommended next bounded action
