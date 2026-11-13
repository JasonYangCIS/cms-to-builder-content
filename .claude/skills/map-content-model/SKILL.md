---
name: map-content-model
description: Map Contentful content types to what exists in the Builder.io space (pages, SDK components, section models, data models, symbols). Reads both sides, classifies each content type's role from the reference graph, proposes targets, lists what the pipeline cannot carry yet, then stops for the human. Use when the user asks how Contentful content maps to Builder components or models, or after the export and before approving a mapping.
argument-hint: [content type, optional]
---

# Map the content model

Propose how each Contentful content type lands in Builder. **This skill only proposes.** Read `CLAUDE.md` first.

**Hard rules**
- Read-only against Contentful and Builder. Writes only `exports/builder_components.json` and `mapping/proposal.json` (both local).
- **Never run `python3 scripts/mapping.py approve`** and never edit an approved `mapping/field-mapping.json` without telling the user that this resets approval.
- Never create models, symbols or components in Builder. Builder publishes symbols on creation, which breaks the drafts-only rule.

## Steps

1. **Inputs.** `exports/content_types.json` and `exports/entries.jsonl` must exist (else run the export step of `/setup-migration`). Without entries the reference graph is unknown and every role is low confidence: say so.
2. **Read Builder** (read-only tools): `list_builder_models` (group by kind: page, data, component = section model, symbol), `get_model_schema` for each page and data model that a content type might target (note required fields), `get_registered_components` (write the SDK-component snapshot to `exports/builder_components.json`), and `search_content_ids` or `get_pages_hierarchy` for existing section models and symbols.
3. **Classify.** `python3 scripts/propose_mapping.py --fresh`, then read `mapping/proposal.json`. Each type has `suggested_role`:
   - `page`: a Builder page-kind model (`page`, or one like `blog-post`).
   - `inline-component`: an SDK component block inside a page.
   - `shared-component`: used by several pages, so a Builder section model is the natural target.
   - `data-record`: author, category and similar, so a Builder data model plus reference fields.
   - Symbols are never proposed.
4. **Review each type with the user.** Show a table: type, suggested role and why, target model or component, confidence, ambiguous alternatives, fields that would be dropped. For page-kind and data targets, compare against the model schema and list **required Builder fields that have no source** (these would make a create fail). Ask the user about every `ambiguous_with` match; you cannot know design intent.
5. **State the gaps plainly.** The runner writes pages and inline components only. For every `shared-component` and `data-record` type, repeat the proposal's `not_yet_supported` text and give the user the choice: migrate those later once the runner supports them, inline shared components as copies for now, or mark the type skipped. Never imply it will be migrated when it will not.
6. **Record decisions** the user accepts in `mapping/field-mapping.json` (every dropped field gets a `reason`), then **STOP**: tell the user to review it and approve it themselves with `! python3 scripts/mapping.py approve`.

Finish with: "Next: `/run-migration <content type>` for a pilot, once the mapping is approved."
