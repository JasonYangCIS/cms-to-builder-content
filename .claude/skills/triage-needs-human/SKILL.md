---
name: triage-needs-human
description: Work through entries the pipeline stopped on (needs_human, failed). Groups them by cause, reproduces a sample, helps decide the fix with the user, then requeues or skips the group and re-runs a small wave. Only the user starts this (it changes manifest state after approval).
argument-hint: [optional: group id or content type to start with]
disable-model-invocation: true
---

# Triage `needs_human` entries

Treat a large pile as a handful of decisions, one cause at a time. Read `CLAUDE.md` and the README section "Resolving many needs_human entries" first.

**Hard rules**
- Fix the **cause** once (policy, mapping, transformer, or the source content). Never hand-edit entries one by one, never `set --status` to hide a problem, never loosen verification, never edit Builder content directly, never delete.
- `manifest.py requeue` and `skip` are **dry runs without `--apply`**. Always show the dry-run result and get the user's explicit yes before `--apply`.
- Editing `mapping/field-mapping.json` resets its approval: tell the user they must re-approve (`! python3 scripts/mapping.py approve`). Never approve it yourself.
- Test any new rule on a **small wave** first: the pipeline cannot delete Builder drafts, so a bad rule applied widely leaves wrong drafts for a human to clean up.

## Steps

1. **See the groups.** `python3 scripts/build_report_html.py`, then list `reports/site/worklists/*.csv` with their row counts (`wc -l`). Each file is one cause (the file name starts with the category: `url_exists`, `failed`, `needs_human`, `archived`, `published`, `exceptions`, ...). Start with the largest, or `$ARGUMENTS`.
2. **Understand one group.** Read its CSV header and first rows (`reason`, `suggested_action`). Reproduce a sample: `python3 scripts/run_migration.py --preview <source_id>`. Read the manifest note: `python3 scripts/manifest.py get <source_id>`.
3. **Decide the cause with the user** (AskUserQuestion). Typical answers:

   | Category | Options |
   |---|---|
   | `url_exists` | Pick one URL policy for the whole group: rewrite via the mapping's `constant_map`, add a rule in `scripts/transform/slug.py` (with tests), skip these pages, or replace the existing Builder page by hand |
   | `failed` / `needs_human` | Usually one bug: read the failing check, fix the transformer or mapping, add a test |
   | `exceptions` | Extend the converter (for example an embedded entry), or accept dropping it |
   | `archived` | A human archived the draft: restore it in Builder, or skip the entries |
   | `published` | A human published it: confirm that was intended; the pipeline will not touch it |
   | bad source content | Send the CSV to the content team, fix in Contentful, re-export (`/setup-migration` step 3), re-run |

4. **Apply the fix**, then **send the group back or retire it**, using the group's CSV as the selector:
   ```bash
   python3 scripts/manifest.py requeue --ids-file reports/site/worklists/<group>.csv           # dry run: show matched / sample
   python3 scripts/manifest.py requeue --ids-file reports/site/worklists/<group>.csv --apply   # only after the user says yes
   python3 scripts/manifest.py skip --reason "<why>" --ids-file reports/site/worklists/<group>.csv   # dry run first, then --apply
   ```
5. **Re-run a small wave** (10 to 100) following `/run-migration`, and check it is clean before widening. If more than ~5% lands in `needs_human` again, the cause is not fixed: go back to step 2.
6. **Report** what changed (counts before and after) and what is left.
