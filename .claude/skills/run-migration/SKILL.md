---
name: run-migration
description: Run a pilot or a wave of the migration for one content type. Dry run first, shows what it would write, asks before writing, creates Builder DRAFTS, verifies them, reconciles, builds the report, then stops for the human's review. Only the user starts this (it writes to Builder).
argument-hint: [content type] [batch size, default 10]
disable-model-invocation: true
---

# Run a migration batch

Arguments: `$ARGUMENTS` (content type, then optional batch size; default 10). If the content type is missing, ask. Read `CLAUDE.md` first.

**Hard rules**
- Mapping gate first: `python3 scripts/mapping.py check` must exit 0. If it does not, stop and tell the user the mapping needs **their** approval. Never approve it.
- **Drafts only.** Never publish, archive, delete or create symbols. Never edit a Builder entry that is not a draft.
- **One content type per run.** If the manifest has no `verified` rows yet, this is a **pilot**: at most ~10 entries, then **STOP for the user's review**. Do not continue to other content types on your own.
- Never run `manifest.py requeue` or `skip` here (that is `/triage-needs-human`). Never use `set --status` to make numbers look better.
- If a step reports `CRITICAL`, or `logs/runner-unsaved.jsonl` appears, **stop** and show the user.

## Steps

1. **Preflight.** Mapping gate; `python3 scripts/manifest.py release-stale --minutes 30`; `python3 scripts/manifest.py summary`.
2. **Dry run (no writes).** `python3 scripts/run_migration.py --content-type <ct> --limit <n>`. Show: how many would be created or updated, any flagged rows with their reasons (URL collisions, rich-text exceptions, empty slugs), and the URLs. Spot-check one: `python3 scripts/run_migration.py --preview <source_id>`.
3. **Ask before writing.** State the Builder space (id and name from `who_am_i`), the number of drafts, and their URLs; get an explicit yes (use AskUserQuestion). No yes, no writes.
4. **Write the drafts.** `python3 scripts/run_migration.py --apply --content-type <ct> --limit <n>`. Summarize created / needs_human / failed and any `unlogged_writes`. (To use the `migrator` subagent instead, pass it the claimed rows and `DRY_RUN`; prefer it only when entries need judgment.)
5. **Verify.** `python3 scripts/run_verification.py --content-type <ct> --limit <n>` (dry run), then with `--apply`. Summarize verified / failed / needs_human. A published or archived target goes to `needs_human`: tell the user.
6. **Reconcile.** `python3 scripts/reconcile.py` (dry run). Show uncovered references and proposed manifest updates; `--apply` once they look right.
7. **Report.** `python3 scripts/build_report_html.py` and give the user `reports/site/index.html`.
8. **Stop.** Give a short summary (counts, what needs attention, how to review in Builder). If more than ~5% of the batch is `needs_human` or `failed`, say the cause is probably systemic and suggest `/triage-needs-human` before widening. Wait for the user.
