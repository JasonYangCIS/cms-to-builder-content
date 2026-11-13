# Deterministic converters

Structured conversions live here as plain code, not freehand LLM output. The migrator calls these; the LLM only handles items they flag as exceptions.

Contract: each module is a CLI that reads JSON on stdin and writes `{"result": ..., "exceptions": [...]}` on stdout, and exposes the same logic as a Python function. Nothing is guessed: whatever a module cannot represent faithfully goes into `exceptions`.

| Module | Status | Does |
|---|---|---|
| `richtext.py` | built | Contentful rich-text document → HTML (paragraphs, headings, lists, quotes, bold/italic/underline/code, links). Embedded entries/assets, entry/asset hyperlinks, unknown nodes and marks → `exceptions`. |
| `slug.py` | built | Normalizes slugs, applies the mapping's `constant_map`, detects collisions against existing Builder URLs. Policy: free → use as-is; exists → never overwrite (`collision`, needs a human-chosen URL); own draft → `own`; no prefix. |
| `refs.py` | built | Resolves links via the manifest `source_id → target_id` map (second pass). Unresolved links stay `null` and are listed. |

`scripts/verify_checks.py` has a matching comparator per field type; its rich-text comparator re-runs `richtext.py` and compares HTML (exceptions → fuzzy for the verifier to judge).

Tests: `python3 -m unittest discover -s scripts/transform -v`

Inputs the CLIs expect: `slug.py` needs the list of existing Builder URLs (from the Builder MCP `get_pages_hierarchy`, or a snapshot of it); `refs.py` needs the id map built from the manifest.
