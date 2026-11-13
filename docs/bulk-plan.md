# Bulk-scale plan (hundreds to thousands of entries)

Status (2026-11-13): Stages 0-7 built and tested on the pilot space (10 real entries, 2 pages). NOT yet exercised at scale: the runner has done one live create (a synthetic page); `PATCH` updates are only mock-tested; Stage 8 (waves) has not started. Run order follows `docs/phases.md`.

| Stage | Built as |
|---|---|
| 0 pilot | migrator/verifier/reporter agents; 2 pages verified |
| 1 export | `scripts/export_contentful.py` |
| 2 mapping | `scripts/propose_mapping.py` (proposal only) |
| 3 transformers | `scripts/transform/{richtext,slug,refs}.py` |
| 4 assets | `scripts/migrate_assets.py` (referenced assets only) |
| 5 runner | `scripts/run_migration.py` |
| 6 verification | `scripts/run_verification.py` |
| 7 references/SEO | `scripts/reconcile.py`, `scripts/seo_redirect_check.py`, `manifest.py list` |

**Principle:** code moves content in bulk; the LLM handles only the exceptions a script flags. The deterministic runner and verifier (`run_migration.py`, `run_verification.py`) now do that; the agents remain for pilots and for exceptions.

## Decisions so far
- Builder Write API key: `BUILDER_CONTENT_MCP_PK` in `.env` (supersedes the `BUILDER_PRIVATE_KEY` name used in older docs).
- Source reads for bulk export: Contentful Delivery token (`CONTENTFUL_DELIVERY_TOKEN`), plus an optional Preview token (`CONTENTFUL_PREVIEW_TOKEN`) if drafts must be included. A Delivery token returns published entries only.
- Locales: OUT OF SCOPE. The Builder space does not support locales, so only `en-US` is migrated and no multi-locale design work is done. (The exporter still exports all locales, which is harmless. Revisit if the Builder space gains locales; Builder's options are inline localization or one entry per locale.)
- Drafts only. Publishing stays a separate, explicit user decision.
- Space size is unknown until the Stage 1 inventory.

## Stage 0: finish the pilot (gate)
- Real draft batch for the 2 pilot pages, then verifier and reporter. Stop for user review.
- Settle the Builder block shape (the dry run assumed `@builder.io/sdk:Element` blocks with no styles).
- Exit: user sign-off on the pilot output.

## Stage 1: bulk export and inventory (read-only)
- `scripts/export_contentful.py`: cursor-paginated export of entries, assets and content types to `exports/` (git-ignored), all locales.
- Inventory report: entries per content type, localized fields, rich-text node types in use, asset counts, reference-graph depth, slug collisions with live Builder pages.
- Everything downstream reads `exports/`, not one MCP call per entry.

## Stage 2: schema and mapping proposal (phase 1)
- Generate a proposed `mapping/field-mapping.json` for every content type; flag localized fields, references, rich text, asset links, no-equivalent fields.
- Human review and approval (`scripts/mapping.py approve`). Never self-approved.
- Replace hard-coded exceptions (e.g. the Home entry id, the `/` slug rewrite) with rule-based slug and name handling.
- Tool built: `scripts/propose_mapping.py` (writes `mapping/proposal.json`, never the approved mapping). Heuristics are drafts only; e.g. it prefers HeroSplit over the approved HeroCentered for `componentHero`, which is a design choice only a human can make.
- Decide the page URL policy (what to do when a source slug already exists in Builder).

## Stage 3: deterministic transformers (`scripts/transform/`)
- `richtext.py` (AST to HTML/Builder blocks, emits `exceptions` for embedded entries/assets and custom marks), `slug.py` (normalize + collision detection), `refs.py` (resolve via the manifest id map).
- JSON stdin/stdout CLIs, golden-file tests, real rich-text comparator in `verify_checks.py` (replacing the stub).
- Exit: pilot payloads reproduced by code with no hand conversion.

## Stage 4: asset migration (phase 2, before content)
- Upload every referenced asset via the Builder Write API; build the `asset_id -> builder_url` map; dedupe by content hash; log each upload.
- Switch the checker's `asset_url` comparator to expect Builder URLs; un-drop `backgroundImage` and the `HeroSplit` image.

## Stage 5: runner without the agent loop
- `scripts/run_migration.py`: claims manifest batches, transforms from `exports/`, writes drafts via the Builder Write API with rate limiting, backoff and retries; records every write in the manifest and audit log.
- Hard limits like the guard: no publish, no delete. `--dry-run`, `--limit`, safe resume after a crash.
- Flagged items go to `needs_human` or a small LLM-assisted exception pass.

## Stage 6: verification at scale
- `verify_checks.py` over every entry; LLM only for fuzzy items and the ~5-per-content-type sample review.
- Reporter aggregates by content type and clusters failures.

## Stage 7: references second pass (phase 3) and redirects/SEO (phase 4)
- Rewrite links from the full id map and re-verify. Model shared components and parent/child rows in the manifest (inlined components currently have their own rows).
- Redirect map and SEO diff (slugs, meta, canonical, OG).

## Stage 8: rollout
- Waves of 10, 100, 500, then everything, with reporter review between waves.
- Agree a standing approval policy for bulk draft writes at that point.

## Not covered yet
Section models, data models/references and symbols: see `docs/content-model-roadmap.md`.

## Open questions
- Real space size (answered by Stage 1).
- Page URL policy for slugs that already exist in Builder (Stage 2).
- Whether drafts in Contentful need migrating (decides if a Preview token is required).
