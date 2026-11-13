# Pilot checklist (completed 2026-11-13; kept as a template for a new space)

Pilot scope: the 2 existing Contentful entries (`page` "Home" and `componentHero` "Sandbox Shopify") plus seeded test
content → Builder sandbox, as drafts. Result: 2 pages migrated and verified, nothing published.

## Credentials / access
- [x] Contentful MCP connected and authorized via `/mcp` (OAuth). The Contentful MCP app only needs **read-only** tool
      categories for the pipeline. Seeding test data is a separate, optional step (see below).
- [x] Builder MCP connector authenticated; `who_am_i` shows the sandbox space.
- [x] `.env` created from `.env.example` (needed by the export, asset, runner and verification scripts).
- [x] Builder Private API Key stored as `BUILDER_CONTENT_MCP_PK` (Write API for drafts, Upload API for assets).

## Decisions
- [x] **Target URL for "Home":** `/home-contentful-migration` (a `constant_map` entry in the mapping) because `/` is
      already a live Builder page. Policy for other URLs: free → use as-is; already exists → never overwrite
      (`needs_human`); no prefix.
- [x] **Hero rendering:** Builder SDK component `HeroCentered`. `preheader` and `backgroundImage` are dropped.
- [x] **Assets:** the one referenced asset was uploaded; no field uses it yet (mapping v3 drops image fields).
- [x] **Locales:** out of scope (the Builder space has no locales); only `en-US`.
- [x] SDK components only appear in `get_registered_components` while the app/preview is connected to the Builder space.

## Pre-flight (run in order)
1. `python3 scripts/guard.py --selftest`
2. `python3 scripts/mapping.py status` → `approved: true` (a human runs `mapping.py approve` after review)
3. Export: `python3 scripts/export_contentful.py` (dry run), then `--export`
4. Seed the manifest: `python3 scripts/manifest.py seed --file <json>` with `[{"source_id", "content_type"}, ...]`
5. Dry run first (`DRY_RUN=true` for the migrator agent, or `python3 scripts/run_migration.py` without `--apply`)
6. Real pilot batch (drafts only) → verify → report. **Stop and review before anything else.** Publishing happens only
   when you say so.

## Optional: seeding test content into Contentful
`scripts/seed_contentful.py` (dry-run default) creates the demo content types and draft entries in
`scripts/seed_data.json`. It needs a Content Management API token, which is a user-approved exception to the
read-only rule (see `CLAUDE.md`). Name an explicit sandbox with `CONTENTFUL_SEED_SPACE` / `CONTENTFUL_SEED_ENVIRONMENT` (the script refuses
your migration source unless `--allow-source`), then revoke the token.
