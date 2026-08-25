# TSYC Facebook Source Ingestion

## Architecture boundary

**EXISTING — `scripts/collect_one_facebook_post.py`:** takes one already-registered `source_urls` row (`crawl_status=PENDING`) and opens that *exact* URL with the authenticated Playwright profile to extract the post's content. Unchanged by this feature. Remains the sole authority for actually opening a Facebook page.

**NEW — `src/services/source_ingestion.py` + `scripts/ingest_source_urls.py`:** takes an exact permalink URL a caller already has in hand and turns it into a valid `source_urls` row. Never opens a browser, never visits Facebook, never crawls or scrolls a group feed to *discover* URLs. Validates and registers only.

These two layers never merge. Collection remains exact-target-only; ingestion only decides what counts as a valid, non-duplicate target before collection ever runs.

## Three ingestion modes

All three converge on the same call: `src.services.source_ingestion.ingest_facebook_post_url(s)`.

### Mode A — Automatic (NOT IMPLEMENTED — external setup required)
An automatic adapter would need TSYC's own post-creation step (or a Meta integration) to hand the exact permalink to this service the moment a post is created — no code in this repository currently creates Facebook posts or receives Meta webhook events. Inventory performed for this change found:
- no Facebook/Meta SDK dependency in `requirements.txt`
- no webhook receiver, app config, or Page/Graph API client anywhere in the repo
- no evidence TSYC posts are created through anything but manually posting in the Facebook group

Building Mode A for real would require, outside this repo, before any code is written:
1. A registered Meta developer App for the TSYC Page/group.
2. The Groups API or Page webhook product subscribed to the exact event needed (e.g. a Page's own post-publish confirmation, since Meta does not offer a general "any post in a group I belong to" webhook to non-admin apps).
3. App Review approval for the specific permission scope, which Meta grants only after reviewing the exact use case.
4. A public HTTPS endpoint to receive the callback.

None of that exists yet, so Mode A is documented, not implemented. Do not attempt to fake or guess this integration.

### Mode B — Batch inbox
```
.venv/Scripts/python.exe scripts/ingest_source_urls.py \
  --input new_posts.txt \
  --batch-code FB-2026-001 \
  --max-sources 5 \
  --non-interactive --confirm-register
```
`--input` is a text file, one exact permalink URL per line (blank lines and `#` comments ignored). Bounded by `--max-sources` — exceeding it fails before any read or write, the same fail-fast contract `run_batch.py` already uses for `--max-candidates`.

### Mode C — Direct single URL
```
.venv/Scripts/python.exe scripts/ingest_source_urls.py \
  --url https://www.facebook.com/groups/2415122391976246/permalink/123456789/ \
  --batch-code FB-2026-001 \
  --max-sources 1 \
  --non-interactive --confirm-register
```
`--url` is repeatable and may be combined with `--input` in one call; both feed the same bounded, deduplicated URL list.

## Validation

`normalize_facebook_permalink()` accepts only `https://(www.|m.)?facebook.com/groups/2415122391976246/permalink/<digits>/` (query strings, e.g. a comment anchor, are stripped and ignored — they still resolve to the containing post). Everything else — a foreign group, a profile, a photo viewer, a share link, an ads/sponsored link, a spoofed hostname (`evil-facebook.com`, `facebook.com.evil.tld`), a placeholder like `<URL>`, or a non-numeric post ID — is rejected with `SOURCE_INVALID` and never reaches `source_urls`. Never guesses or fabricates a post ID.

## Idempotency

`ingest_facebook_post_url()` reads for an existing row (same `batch_id` + `source_type` + normalized `source_url`) before ever writing. If found, it returns that row unchanged (`ALREADY_KNOWN`) instead of calling `SupabaseRepository.save_source_url()` again — that method unconditionally upserts and would otherwise silently reset an already-`COLLECTED` source back to `PENDING`. A duplicate URL within the same input batch is also only ingested once.

## Continuing after ingestion

Ingestion's only output is validated `source_urls` rows. Nothing here parses post content or creates a candidate. To continue, run the existing stage scripts with the exact `source_url_id`(s) just registered:

```
collect_one_facebook_post.py --source-url-id <id> --non-interactive --confirm-save
clean_facebook_raw_pages.py --source-url-id <id> --action SAVE --non-interactive
create_candidates_from_cleaned_posts.py --source-url-id <id> --candidate-title "..." --confirm-create --non-interactive
run_batch.py --candidate-code <code> --max-candidates 5 --non-interactive
```

**Known limitation:** `create_candidates_from_cleaned_posts.py` requires an explicit `--candidate-title` per candidate — there is no automatic title/candidate-count extraction from cleaned post text in this repo. A human (or a future, separately-designed extraction step) still decides how many candidates one post contains and what each is titled; ingestion removes the URL-copying step, not this one.

## Safety boundaries

- Never browses, crawls, scrolls, or discovers Facebook content of any kind.
- Only the one authorized group (`2415122391976246`) is ever accepted.
- No WooCommerce write, publish, or price mutation exists anywhere in this feature.
- Registration (an AUTO-ALLOW deterministic write per CLAUDE.md §4.2) still requires an explicit `--confirm-register` flag, matching every other stage script's convention.
