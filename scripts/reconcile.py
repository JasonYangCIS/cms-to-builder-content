#!/usr/bin/env python3
"""Stage 7 (part 1): reference second pass + parent/child manifest reconciliation. No LLM, no Builder/Contentful writes.

1. REFERENCE COVERAGE. Every Entry link in exports/ must be accounted for: inlined via a mapped `inline` field,
   resolved via a mapped `reference` field (target present in the id map), or explicitly `dropped`. Anything else
   is reported as UNCOVERED (it would silently vanish from the migration).
2. PARENT/CHILD SYNC. Components are inlined into their page, so their manifest rows must follow the page:
   child -> `verified` only when EVERY parent page is verified AND a live re-check of each parent (Builder draft vs
   source, full tree) shows zero failures/fuzzy items; child -> `migrated` when parents are migrated. Rows only
   move forward (pending < migrated < verified); failed/needs_human rows are never touched.
   Shared components (several parents) and orphan component rows (no parent page in exports) are reported.
3. ID MAPS. exports/id_map.json = {page source_id: Builder id} (what verify_checks `--id-map` / refs.py consume);
   exports/inline_map.json = {inlined component id: {pages: [...], builder_ids: [...]}} (inlined entries have no
   Builder id of their own).

Dry run by default (reports + id maps only); --apply updates the manifest via manifest.py.
Usage: python3 scripts/reconcile.py [--apply]
"""
from __future__ import annotations

import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "transform"))
import run_migration as rm  # noqa: E402
import run_verification as rv  # noqa: E402

RANK = {"pending": 0, "migrated": 1, "verified": 2}


def entry_links(entry: dict, locale: str):
    """Yield (field, target_id) for every Entry link in an entry."""
    for fname, byloc in entry.get("fields", {}).items():
        val = byloc.get(locale) if isinstance(byloc, dict) and locale in byloc else byloc
        stack = [val]
        while stack:
            cur = stack.pop()
            if isinstance(cur, dict):
                s = cur.get("sys")
                if isinstance(s, dict) and s.get("type") == "Link" and s.get("linkType") == "Entry":
                    yield fname, s["id"]
                else:
                    stack.extend(cur.values())
            elif isinstance(cur, list):
                stack.extend(cur)


def reference_coverage(entries: dict, mapping: dict, locale: str, id_map: dict) -> dict:
    cts = mapping["content_types"]
    covered, dropped, uncovered = 0, 0, []
    for eid, e in entries.items():
        ct = e["sys"]["contentType"]["sys"]["id"]
        specs = {s["source"]: s for s in cts.get(ct, {}).get("fields", [])}
        for fname, tid in entry_links(e, locale):
            spec = specs.get(fname)
            if spec is None:
                uncovered.append({"entry": eid, "content_type": ct, "field": fname, "target": tid, "why": "field not in mapping"})
            elif spec["type"] == "dropped":
                dropped += 1
            elif spec["type"] == "inline":
                covered += 1
            elif spec["type"] == "reference":
                if tid in id_map:
                    covered += 1
                else:
                    uncovered.append({"entry": eid, "content_type": ct, "field": fname, "target": tid,
                                      "why": "target not in id map yet (second pass)"})
            else:
                uncovered.append({"entry": eid, "content_type": ct, "field": fname, "target": tid,
                                  "why": f"link in a field mapped as {spec['type']!r}"})
    return {"covered": covered, "dropped_by_mapping": dropped, "uncovered": uncovered}


def parents_map(entries: dict, mapping: dict, locale: str) -> dict:
    """child source id -> [parent page source ids] (inlined components and their items)."""
    parents: defaultdict[str, list[str]] = defaultdict(list)
    for pid, e in entries.items():
        cmap = mapping["content_types"].get(e["sys"]["contentType"]["sys"]["id"])
        if not cmap or cmap.get("inline_into"):
            continue
        _, _, kids, _ = rm.build_page(e, entries, mapping, locale, [], None)
        for k in kids:
            if pid not in parents[k]:
                parents[k].append(pid)
    return parents


def main() -> int:
    apply = "--apply" in sys.argv
    rm.load_env()
    key = os.environ.get("BUILDER_CONTENT_MCP_PK")
    mapping = json.loads((ROOT / "mapping" / "field-mapping.json").read_text())
    rm.check_configured(mapping)
    locale, space = mapping["source"]["locale"], mapping["target"]["space"]
    entries = {}
    for line in (ROOT / "exports" / "entries.jsonl").read_text().splitlines():
        if line.strip():
            e = json.loads(line)
            entries[e["sys"]["id"]] = e
    rows = {r["source_id"]: r for r in rm.manifest("list")["rows"]}
    parents = parents_map(entries, mapping, locale)
    top = {pid for pid, e in entries.items() if (c := mapping["content_types"].get(e["sys"]["contentType"]["sys"]["id"]))
           and not c.get("inline_into")}

    # id maps (derived artifacts, safe to rewrite)
    id_map = {pid: rows[pid]["target_id"] for pid in top if pid in rows and rows[pid].get("target_id")
              and rows[pid]["status"] in ("migrated", "verified")}
    inline_map = {c: {"pages": ps, "builder_ids": [id_map[p] for p in ps if p in id_map]} for c, ps in parents.items()}
    (ROOT / "exports" / "id_map.json").write_text(json.dumps(id_map, indent=2) + "\n")
    (ROOT / "exports" / "inline_map.json").write_text(json.dumps(inline_map, indent=2) + "\n")

    # live re-check of every verified parent (read-only)
    clean = {}
    for pid in sorted(top):
        r = rows.get(pid)
        if r and r["status"] == "verified" and r.get("target_id"):
            if not key:
                print("Missing BUILDER_CONTENT_MCP_PK in .env (needed for the live re-check)", file=sys.stderr)
                return 1
            model = mapping["content_types"][entries[pid]["sys"]["contentType"]["sys"]["id"]].get("target_model", "page")
            doc = rv.fetch_target(key, space, model, r["target_id"])
            checks = rv.verify_tree(entries[pid], entries, mapping, locale, doc) if doc and doc.get("published") not in ("archived", "published") else None
            clean[pid] = bool(checks is not None and not [c for c in checks if c["status"] in ("fail", "fuzzy")])

    actions, held, shared, orphans = [], [], [], []
    for cid, pids in sorted(parents.items()):
        row = rows.get(cid)
        if row is None:
            continue
        if len(pids) > 1:
            shared.append({"component": cid, "pages": pids})
        prow = [rows.get(p) for p in pids]
        if all(p and p["status"] == "verified" and clean.get(pid) for p, pid in zip(prow, pids)):
            want = "verified"
        elif all(p and p["status"] in ("verified", "migrated") for p in prow):
            want = "migrated"
        else:
            want = None
        if prow and all(p and p["status"] == "skipped" for p in prow):
            want = "skipped"  # every parent page is deliberately not migrated: so are its inlined components
        if want == "skipped":
            if row["status"] in ("pending", "migrated"):
                actions.append({"source_id": cid, "from": row["status"], "to": "skipped", "target": None, "parents": pids})
        elif row["status"] in RANK and want and RANK[want] > RANK[row["status"]]:
            actions.append({"source_id": cid, "from": row["status"], "to": want,
                            "target": (prow[0] or {}).get("target_id") if len(pids) == 1 else None, "parents": pids})
        elif want is None and row["status"] == "verified":
            held.append({"source_id": cid, "reason": "verified, but a parent is not verified/clean (not downgraded)"})
    for sid, r in rows.items():
        if sid not in top and sid not in parents:
            orphans.append({"source_id": sid, "content_type": r["content_type"], "status": r["status"],
                            "why": "no page in exports references it"})
    if apply:
        for a in actions:
            args = ["set", a["source_id"], "--status", a["to"], "--notes",
                    f"reconcile: {a['to']} with parent page(s) {', '.join(a['parents'])}"]
            if a["target"]:
                args += ["--target", a["target"]]
            try:
                rm.manifest(*args, "--expect-status", a["from"])  # only if the row is still what we looked at
            except RuntimeError as e:
                print(f"  WARNING {a['source_id']}: changed meanwhile, left alone ({e})")

    coverage = reference_coverage(entries, mapping, locale, id_map)
    report = {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "applied": apply,
              "live_recheck_clean": clean, "manifest_updates": actions, "not_downgraded": held,
              "shared_components": shared, "orphan_rows": orphans, "reference_coverage": coverage,
              "id_map_pages": len(id_map), "inline_map_entries": len(inline_map)}
    out = Path(os.environ.get("REPORTS_DIR") or ROOT / "reports")
    out.mkdir(exist_ok=True)
    path = out / f"reconcile-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    path.write_text(json.dumps(report, indent=2))
    print(f"{'APPLY' if apply else 'DRY RUN'}: live re-check of verified pages: {sum(clean.values())}/{len(clean)} clean")
    for pid, ok in sorted(clean.items()):
        if not ok:
            print(f"  WARNING: page {pid} is `verified` in the manifest but its live Builder draft no longer matches "
                  f"the source (edited or archived in Builder?). Re-verify it; its components were not marked verified.")
    print(f"  manifest updates {'applied' if apply else 'proposed'}: {len(actions)}")
    for a in actions:
        print(f"    {a['source_id']}: {a['from']} -> {a['to']}")
    print(f"  shared components: {len(shared)} | orphan rows: {len(orphans)} | not downgraded: {len(held)}")
    print(f"  reference coverage: {coverage['covered']} inlined/resolved, {coverage['dropped_by_mapping']} dropped by mapping, "
          f"{len(coverage['uncovered'])} UNCOVERED")
    for u in coverage["uncovered"][:10]:
        print(f"    UNCOVERED {u['entry']}.{u['field']} -> {u['target']}: {u['why']}")
    print(f"  id maps: exports/id_map.json ({len(id_map)} pages), exports/inline_map.json ({len(inline_map)} inlined)\n  Report: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
