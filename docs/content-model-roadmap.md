# Content-model roadmap: section models, data models, symbols

Status (2026-11-13): NOT implemented. The runner writes **pages and inline SDK-component blocks only**.
`scripts/propose_mapping.py` and `/map-content-model` already suggest a role per content type and flag these gaps
(`not_yet_supported`), so nothing is silently dropped. This file records what is known and what to do later.

## Verified in the sandbox Builder space (read-only checks)
- Model kinds: `page` (also `blog-post`, `collection`, `product`), `component` = **section model** (e.g. `announcement-bar`),
  `data` (e.g. `blog-author`, `blog-category`), `symbol`.
- A section entry stores `data.blocks` with an ordinary `@builder.io/sdk:Element` (`component.name`, `component.options`):
  the same block shape the runner builds for inline components.
- A data-record reference is stored as `{"@type": "@builder.io/core:Reference", "id": "<entry id>", "model": "<data model>"}`;
  a list of references wraps each item, e.g. `[{"category": <Reference>}]`.
- `file` fields hold a plain URL string (fits `exports/asset_map.json`).
- `blog-post` has 24 fields, 14 required (incl. SEO fields): a create can fail on required fields with no source.
- Symbols are published entries in the `symbol` model, used as `symbol={{entry, model, name}}`.

## NOT verified (confirm before building; Builder's docs do not say)
- How a page embeds a section model: a **copy** of its blocks or a **live link**, and the block JSON for either.
  Get it by inserting a section into a draft page in the Builder UI and reading the page back.
- Whether the Write API accepts a Reference payload on create.
- Whether a reference to a **draft** entry resolves at render time.
- The `model` pinning on reference fields (blank on `blog-post.author`).

## Plan, in order
1. **Data models and references.** Create data entries first (drafts), record Contentful id to Builder id in the manifest,
   then a second pass fills reference fields on the parents (`reconcile.py` already builds the id map). Validate required
   fields against `get_model_schema` before any write. First a live test: one author plus one post.
2. **Section models.** Write shared components as section entries (drafts). Keep inline copies in pages until the real
   embed JSON is captured; never claim "linked" before that.
3. **Symbols.** Do not create them: Builder publishes symbols on creation, which breaks drafts-only, and `guard.py` blocks
   `create_symbol`. A mapping may point at an **existing** symbol id. Creating one would be an explicit, per-symbol,
   human-approved exception with its own audit line (and a guard change reviewed by a human).

## When this lands, also update
`scripts/propose_mapping.py` (`not_yet_supported`), `mapping/field-mapping.example.json` and `scripts/mapping.py` (new
roles), `scripts/run_migration.py` and `scripts/run_verification.py` (targets, reference pass), `scripts/reconcile.py`,
the `/map-content-model` skill, `CLAUDE.md` safety rules if symbols change, and `docs/bulk-plan.md`.
