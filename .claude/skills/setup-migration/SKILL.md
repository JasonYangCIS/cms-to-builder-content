---
name: setup-migration
description: Guided first-time setup of this Contentful to Builder.io migration for a new space. Checks prerequisites, creates the mapping from the example, runs the read-only export, proposes a mapping, then stops for the human to approve it. Use when the user wants to set up, configure or onboard a space, or when mapping/field-mapping.json is missing or still contains YOUR_ placeholders.
argument-hint: [contentful space id, optional]
---

# Set up a new migration

Walk the user through setup, **one step at a time**, confirming each result before moving on. Read `CLAUDE.md` first.

**Hard rules**
- Never read, print or ask the user to paste `.env` (the Read tool is denied; `cat` is not, so do not use it). Ask the user to confirm the keys exist instead.
- **Never run `python3 scripts/mapping.py approve`.** Approval is the human's decision, always.
- Never write to Contentful or Builder. This skill only reads them and writes local files.

## Steps

1. **Prerequisites.** `python3 --version` (3.9+). Check that `.env` and `mapping/field-mapping.json` exist (`ls`, not `cat`). Ask the user to run `/mcp` and confirm `contentful` is authenticated and the Builder.io connector is connected. Then call the read-only tools `mcp__contentful__get_initial_context` and Builder `who_am_i`, and **show the user the Contentful space/environment and the Builder space name and id**. Ask them to confirm the Builder space is the intended **sandbox**.
2. **Mapping file.** If `mapping/field-mapping.json` is missing: `cp mapping/field-mapping.example.json mapping/field-mapping.json`. Ask for (or take from `$ARGUMENTS`): Contentful space id and environment, the Builder space id from step 1, the locale (single locale only), and the entry id of the home page if there is one. Fill in the `YOUR_*` placeholders. Editing the mapping resets its approval, which is expected.
3. **Export (read-only).** `python3 scripts/export_contentful.py` first (counts only). Show it. With the user's OK, `python3 scripts/export_contentful.py --export`. Summarize `exports/inventory.json`: content types, entries per type, assets, locales, drafts vs published.
4. **Builder components snapshot.** Call `mcp__claude_ai_Builder_io_Publish__get_registered_components` and write `exports/builder_components.json` as `{"components": {"Name": {"input": "type"}}, "page_models": ["page", ...]}` (SDK components only).
5. **Mapping proposal.** `python3 scripts/propose_mapping.py --fresh` for a new space. Read `mapping/proposal.json`. Explain low-confidence or ambiguous matches (the tool cannot know your design intent), fields it would drop and why, localized fields, rich text, asset fields. Put the user's accepted choices into `mapping/field-mapping.json`. Record every dropped field with a `reason`.
6. **STOP for approval.** Tell the user to review `mapping/field-mapping.json` and approve it themselves: type `! python3 scripts/mapping.py approve` at the prompt. Wait.
7. **After approval.** `python3 scripts/mapping.py check` must exit 0. Seed the manifest from the export:
   ```bash
   python3 - <<'PY' | python3 scripts/manifest.py seed --file -
   import json
   print(json.dumps([{"source_id": e["sys"]["id"], "content_type": e["sys"]["contentType"]["sys"]["id"]}
                     for e in map(json.loads, open("exports/entries.jsonl"))]))
   PY
   ```
   Then `python3 scripts/guard.py --selftest` and `python3 scripts/manifest.py summary`. Finish with: "Next: `/run-migration <content type>` for a pilot."
