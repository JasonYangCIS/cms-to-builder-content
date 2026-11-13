---
name: migration-report
description: Refresh the derived state (reference coverage, id maps, redirect and SEO check) and build the HTML report site, then summarize what it says. Read-only against Contentful and Builder. Use when the user asks for status, a report, progress, or what needs attention.
argument-hint: [optional: source id prefix to hide, e.g. test fixtures]
---

# Build and summarize the migration report

Local files and read-only calls only. Never change manifest statuses here.

1. `python3 scripts/manifest.py summary` for the counts.
2. `python3 scripts/reconcile.py` (**dry run**: it rewrites only the derived `exports/id_map.json` files). Note any `UNCOVERED` references and any `WARNING` that a verified page drifted.
3. `python3 scripts/seo_redirect_check.py` (read-only Builder listing) for URL changes and SEO gaps.
4. `python3 scripts/build_report_html.py` (add `--exclude-prefix <prefix>` if `$ARGUMENTS` names test fixtures). It writes the static site to `reports/site/`.
5. Summarize for the user, in plain words: the verdict line, the biggest attention groups with counts and the decision each needs, any URL changes, anything published by a human, and what was deliberately not migrated. Give the path `reports/site/index.html` (they can `open` it; `--zip` makes a shareable archive).

If the manifest has rows in `needs_human`, point to `/triage-needs-human`. If reconcile proposes manifest updates, say so and that `/run-migration` applies them.
