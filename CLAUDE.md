# Contentful → Builder migration pipeline

Maker / checker / reporter pipeline built from Claude Code subagents. **The main session is the orchestrator** (subagents cannot spawn subagents). You coordinate; the subagents do the work and return short summaries.

- Source: the Contentful space/environment in `mapping/field-mapping.json` (`source`), via the `contentful` MCP server (**read-only**).
- Target: the Builder space in `mapping/field-mapping.json` (`target.space`, ideally a sandbox) via the Builder MCP connector. Builder content is created as **drafts only**.
- State: `state/manifest.db`, touched **only** through `scripts/manifest.py`. Never open the DB directly and never let a subagent do so.
- Audit: every Builder write is appended to `logs/writes.jsonl` (`manifest.py log-write`).

## Agents (`.claude/agents/`)
| Agent | Role | Never |
|---|---|---|
| `migrator` | Reads a source entry, transforms per mapping, writes a Builder draft. Idempotent. | marks `verified`; publishes |
| `verifier` | Independent re-read, `scripts/verify_checks.py`, judgment only on fuzzy items. | fixes anything; writes to Builder/Contentful |
| `reporter` | Read-only summary into `reports/`, samples ~5 verified entries per content type. | changes state or content |

Deterministic code does structured conversion (rich text, slugs, references, assets) via `scripts/`; the LLM only handles exceptions the scripts flag.

**Skills (`.claude/skills/`)** package the recurring jobs; follow them when the user asks for the job: `/setup-migration` (onboard a space; never approves the mapping), `/run-migration` (pilot or wave: dry run, approval, drafts, verify, report, stop; user-started only), `/map-content-model` (classify each content type as page / inline component / shared section / data record and propose Builder targets; proposal only), `/migration-report` (refresh state and build the report site), `/triage-needs-human` (group stuck entries, fix each cause once, requeue or skip; user-started only).

**Scale path (see `docs/bulk-plan.md`):** `scripts/run_migration.py` (maker) and `scripts/run_verification.py` (checker) do the same roles without an LLM, with the same manifest lifecycle and rules (drafts only, dry-run by default, `--apply` to write, mapping gate, no delete). Rows are changed with `manifest.py set --expect-status in_progress` (compare-and-set) so a stale worker cannot overwrite a re-claimed row. The agents above remain for exceptions (`needs_human`, fuzzy items) and pilots. `run_verification.py` may mark entries `verified` only when every deterministic check passes; it never writes to Builder or Contentful.

## Status lifecycle
`pending` → (claim) `in_progress` → `migrated` → (claim) `in_progress` → `verified`
Failure path: `failed` → back to the migrator with the verifier's notes. `manifest.py set --status failed` escalates to `needs_human` automatically once an entry has failed more than 3 times.
`skipped` = deliberately not migrated (a human decision recorded with a reason); it is not work and not "needs attention". **Resolving many `needs_human` rows:** fix the cause once (mapping/transformer/policy), then `manifest.py requeue` (resets the retry count; a plain `set --status pending` keeps it, so an escalated row would escalate again) or `manifest.py skip --reason "..."`. Both are dry runs without `--apply`, are for the orchestrator/human only (subagents must never run them), and are logged to `logs/manifest-changes.jsonl`.

## Orchestration loop (per content type)
1. **Gate:** `python3 scripts/mapping.py check` must exit 0. If not, stop and tell the user the mapping needs their approval. Do not approve it yourself. Editing the mapping resets approval.
2. `python3 scripts/manifest.py release-stale --minutes 30`.
3. **Batch of 10:** `claim --status pending --limit 10 --content-type <ct>` (plus `--status failed` entries, which carry verifier notes). Pass the claimed rows and `DRY_RUN` to `migrator`.
4. Then `claim --status migrated --limit 10` and pass those rows to `verifier`.
5. Failed entries go back to the migrator with the verifier's notes (steps 3–4 again). After 3 retries the manifest sets `needs_human`; skip it and move on.
6. Rate limits: back off (2s, 5s, 15s, 60s) and retry; do not hammer. If still limited, release the claims and stop the batch.
7. When the batch loop is done, run `reporter`.

Keep your own context small: only relay the subagents' short summaries to the user.

## Modes and limits
- `--dry-run` (tell the migrator `DRY_RUN=true`): transform and diff, no Builder writes, no manifest `migrated`.
- **Pilot first:** one content type, up to ~10 entries. When the pilot batch is verified/reported, **STOP and wait for the user's review.** Do not continue to other content types or the full run on your own.
- **Publishing:** only after an entry is `verified`, and only when the user explicitly says so. Never publish as a side effect.

## Safety (also enforced by `.claude/settings.json` and `scripts/guard.py`)
- Contentful: never call a write/delete tool. Credentials only via env vars (`.env`, see `.env.example`); never print or commit them, and never read `.env` (not with Read, and not with `cat`/`sed`/`grep` in Bash).
  - **One user-approved exception:** `scripts/seed_contentful.py` seeds test content types/entries via the CMA (`CONTENTFUL_MANAGEMENT_TOKEN` in `.env`). The target must be named explicitly (`CONTENTFUL_SEED_SPACE` + `CONTENTFUL_SEED_ENVIRONMENT`, no default) and it refuses the migration source unless `--allow-source`. Dry-run by default, `--apply` to write, `--publish --apply` to publish `seed-*` entries only. Run by the orchestrator/user on request, never by subagents. Never delete. Revoke the token when seeding is done.
- Builder: no delete operations; never publish or archive; never create symbols (they publish on creation). Always pass `published: "draft"` explicitly on `create_builder_content` (the guard blocks it otherwise). Never edit a Builder entry that is not a draft. If a create's outcome is unknown, never recreate it blindly: look it up by URL first.
- Bulk writes (Builder create/update) require user approval during the pilot. Bulk manifest changes (`requeue`, `skip`) require `--apply` and are in the permission `ask` list.
- No customer data in git: `exports/`, `state/`, `logs/`, `reports/`, `.env` are git-ignored.
- Commit scaffolding changes to git after each step. Do not commit anything from the ignored dirs.

## Phases and scale (see `docs/phases.md`, `docs/bulk-plan.md`)
Schema/mapping proposal → asset migration (before content) → core pipeline → reference/reconcile pass → redirect/SEO check. All are implemented as scripts and were exercised on a small pilot only; nothing has been run at scale. Tests: `make test`; full gate (tests + ruff + mypy): `make check` (after `make dev-setup`).
