#!/usr/bin/env python3
"""Deterministic verification at scale (Stage 6 of docs/bulk-plan.md): no LLM in the loop.

For every claimed `migrated` page row: re-read the Builder draft (Content API, live) and the source entry
(exports/), run scripts/verify_checks.py for the page and for EVERY component/item in it, and decide:
  * any check fails, or block/item counts differ  -> `failed` (notes = specific diffs; manifest escalates
    to needs_human after repeated failures)
  * target archived in Builder (a human action)   -> `needs_human`
  * target missing in Builder                     -> `failed`
  * only `fuzzy` items (e.g. rich-text exceptions) -> stays `migrated` with a note; listed for LLM/human review
  * everything pass/skip                           -> `verified`; its inlined component rows become `verified`
Never writes to Builder or Contentful; it only updates the manifest (via manifest.py) and writes a report to
reports/ (git-ignored) with status counts, failure clusters, fuzzy items and a deterministic ~5-per-type sample
for human review.

Usage: python3 scripts/run_verification.py [--apply] [--limit 50] [--content-type page]
  Dry run by default (reports what it would decide, changes no manifest status); --apply records the outcomes.
  A target that is published (a human published it; this pipeline only creates drafts) or archived goes to needs_human,
  never to verified, and the published state of every checked entry is recorded in the run report.
"""
from __future__ import annotations

from typing import Any
import hashlib
import json
import os
import subprocess
import sys
import urllib.parse
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "transform"))
import refs  # noqa: E402
import run_migration as rm  # noqa: E402  (http with backoff, manifest(), load_env)
import verify_checks as vc  # noqa: E402

SAMPLE_PER_TYPE = 5


def fetch_target(key: str, space: str, model: str, target_id: str):
    q = urllib.parse.urlencode({"apiKey": space, "query.id": target_id, "includeUnpublished": "true",
                                "limit": 1, "cachebust": "true"})
    res = rm.http("GET", f"{rm.CONTENT}/{model}?{q}", key).get("results", [])
    if not res:
        return None
    doc = res[0]
    data = doc.get("data", {})
    if isinstance(data.get("blocks"), list):  # the Content API appends a tracking-pixel <img> block: not ours
        data = {**data, "blocks": [b for b in data["blocks"] if not str(b.get("id", "")).startswith("builder-pixel")]}
        doc = {**doc, "data": data}
    return doc


def verify_tree(page: dict, entries: dict, mapping: dict, locale: str, doc: dict) -> list[dict]:
    """All checks for a page and everything inlined into it. Each check carries its source entry id."""
    out: list[dict] = []
    cts = mapping["content_types"]

    def add(eid, ct, res):
        if "error" in res:
            out.append({"entry": eid, "content_type": ct, "name": "mapping", "field": "", "status": "fail", "diff": res["error"]})
        for c in res.get("checks", []):
            out.append({"entry": eid, "content_type": ct, **c})

    def fail(eid, ct, name, diff):
        out.append({"entry": eid, "content_type": ct, "name": name, "field": "", "status": "fail", "diff": diff})

    pid, pct = page["sys"]["id"], page["sys"]["contentType"]["sys"]["id"]
    add(pid, pct, vc.run(page, doc, mapping, pct, locale, {}, {}))
    ids = [refs.link_id(x) for x in (vc.source_value(page, "componentsReference", locale) or [])]
    blocks = doc.get("data", {}).get("blocks", [])
    if len(blocks) != len(ids):
        fail(pid, pct, "block_count", f"expected {len(ids)} blocks, Builder has {len(blocks)}")
    for idx, sid in enumerate(ids):
        child = entries.get(sid)
        if child is None:
            fail(sid, "?", "source", "linked entry missing from exports")
            continue
        ct = child["sys"]["contentType"]["sys"]["id"]
        add(sid, ct, vc.run(child, doc, mapping, ct, locale, {}, {"index": idx}))
        for spec in cts.get(ct, {}).get("fields", []):
            if spec.get("type") == "inline" and spec.get("target", "").endswith(".options.items"):
                item_ids = [refs.link_id(x) for x in (vc.source_value(child, spec["source"], locale) or [])]
                tgt_items = vc.get_path(doc, spec["target"].replace("{index}", str(idx))) or []
                if len(tgt_items) != len(item_ids):
                    fail(sid, ct, "item_count", f"expected {len(item_ids)} items, Builder has {len(tgt_items)}")
                for j, iid in enumerate(item_ids):
                    item = entries.get(iid)
                    if item is None:
                        fail(iid, "?", "source", "linked item missing from exports")
                        continue
                    ict = item["sys"]["contentType"]["sys"]["id"]
                    add(iid, ict, vc.run(item, doc, mapping, ict, locale, {}, {"index": idx, "item": j}))
    return out


def sample(verified: list[dict]) -> dict:
    by = defaultdict(list)
    for v in verified:
        for eid, ct in v["entries"]:
            by[ct].append((hashlib.sha256(eid.encode()).hexdigest(), eid, v["builder_id"], v["url"]))
    return {ct: [{"source_id": e, "builder_id": b, "url": u} for _, e, b, u in sorted(rows)[:SAMPLE_PER_TYPE]]
            for ct, rows in sorted(by.items())}


def append_note(source_id: str, note: str) -> str:
    try:
        old = (rm.manifest("get", source_id).get("notes") or "").strip()
    except RuntimeError:
        old = ""
    return (f"{old} | {note}" if old else note)[:900]


def main() -> int:
    argv = sys.argv[1:]
    arg = lambda n, d=None: argv[argv.index(n) + 1] if n in argv else d
    dry, limit = "--apply" not in argv, int(arg("--limit", 50))  # (--dry-run is accepted and is the default)
    rm.load_env()
    key = os.environ.get("BUILDER_CONTENT_MCP_PK")
    mapping = json.loads((ROOT / "mapping" / "field-mapping.json").read_text())
    rm.check_configured(mapping)
    if not key:
        print("Missing BUILDER_CONTENT_MCP_PK in .env", file=sys.stderr)
        return 1
    if subprocess.run([sys.executable, str(ROOT / "scripts" / "mapping.py"), "check"], capture_output=True).returncode:
        print("Mapping is not approved (mapping.py check failed). Stopping.", file=sys.stderr)
        return 1
    locale, space = mapping["source"]["locale"], mapping["target"]["space"]
    entries = {}
    for line in (ROOT / "exports" / "entries.jsonl").read_text().splitlines():
        if line.strip():
            e = json.loads(line)
            entries[e["sys"]["id"]] = e
    rm.manifest("release-stale", "--minutes", "30")
    cts = [arg("--content-type")] if arg("--content-type") else [c for c, m in mapping["content_types"].items() if not m.get("inline_into")]
    claimed: list[dict[str, Any]] = []
    for ct in cts:
        room = limit - len(claimed)
        if room > 0:
            claimed += rm.manifest("claim", "--status", "migrated", "--limit", str(room), "--content-type", ct)["claimed"]
    print(f"{'DRY RUN' if dry else 'VERIFY'}: claimed {len(claimed)} migrated row(s) of types {cts}")

    outcome: Counter[str] = Counter()
    clusters: Counter[tuple] = Counter()
    check_counts: defaultdict[str, Counter[str]] = defaultdict(Counter)
    fuzzy_items, verified_rows, results = [], [], []
    done = set()
    try:
        for row in claimed:
            sid, tid = row["source_id"], row.get("target_id")
            entry = entries.get(sid)
            status, note, checks, doc = None, "", [], None
            if entry is None or not tid:
                status, note = "needs_human", "no source entry in exports or no target_id"
            else:
                model = mapping["content_types"][entry["sys"]["contentType"]["sys"]["id"]].get("target_model", "page")
                try:
                    doc = fetch_target(key, space, model, tid)
                except RuntimeError as e:
                    status, note = "migrated", f"verification could not read Builder ({str(e)[:120]}); retry"
                if doc is None and status is None:
                    status, note = "failed", f"target {tid} not found in Builder"
                elif doc is not None and doc.get("published") == "archived":
                    status, note = "needs_human", f"target {tid} is archived in Builder (human action); decide whether to restore or drop"
                elif doc is not None and doc.get("published") == "published":
                    status, note = "needs_human", (f"target {tid} is PUBLISHED in Builder. This pipeline only creates drafts, so a human published it; "
                                                   "confirm that was intended (it will not be verified or edited by the pipeline)")
                elif doc is not None:
                    checks = verify_tree(entry, entries, mapping, locale, doc)
                    fails = [c for c in checks if c["status"] == "fail"]
                    fuzzy = [c for c in checks if c["status"] == "fuzzy"]
                    for c in checks:
                        check_counts[c["content_type"]][c["status"]] += 1
                    for c in fails:
                        clusters[(c["content_type"], c["name"], c.get("field", ""), c.get("diff", ""))] += 1
                    if fails:
                        status = "failed"
                        note = "verify: " + "; ".join(f"{c['entry']}.{c.get('field') or c['name']}: {c.get('diff', '?')}" for c in fails[:4])
                    elif fuzzy:
                        status = "migrated"
                        note = f"verify: {len(fuzzy)} fuzzy item(s) need review"
                        fuzzy_items += [{"source_id": sid, "entry": c["entry"], "field": c.get("field"), "diff": c.get("diff")} for c in fuzzy]
                    else:
                        status, note = "verified", "verify: all checks pass/skip"
            if status is None:  # unreachable today (every branch sets it); never crash a batch if a future edit misses one
                status, note = "needs_human", "internal: verification produced no outcome for this entry"
            outcome[status] += 1
            url = (doc or {}).get("data", {}).get("url")
            results.append({"source_id": sid, "builder_id": tid, "outcome": status, "note": note, "url": url, "published": (doc or {}).get("published")})
            print(f"  {sid}: {status}" + (f" - {note[:140]}" if status != "verified" else ""))
            if dry:
                continue
            try:
                rm.manifest("set", sid, "--status", status, "--notes", append_note(sid, note), "--expect-status", "in_progress")
            except RuntimeError as e:
                print(f"  WARNING {sid}: not updated ({e})")  # the claim was lost: leave the row to its new owner
                done.add(sid)
                continue
            done.add(sid)
            if status == "verified":
                kids = {(c["entry"], c["content_type"]) for c in checks}
                verified_rows.append({"source_id": sid, "builder_id": tid, "url": url, "entries": sorted(kids)})
                for kid, _ in kids:
                    if kid == sid:
                        continue
                    try:
                        current = rm.manifest("get", kid).get("status")
                        if current in ("pending", "migrated"):
                            rm.manifest("set", kid, "--status", "verified", "--target", tid, "--notes", f"verified with parent page {sid}",
                                        "--expect-status", current)
                    except RuntimeError:
                        pass
    finally:  # never leave claims in_progress
        for row in claimed:
            if row["source_id"] not in done:
                try:
                    rm.manifest("set", row["source_id"], "--status", "migrated", "--expect-status", "in_progress")
                except RuntimeError:
                    pass

    report = {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "dry_run": dry,
              "outcomes": dict(outcome), "check_counts_by_content_type": {k: dict(v) for k, v in sorted(check_counts.items())},
              "failure_clusters": [{"content_type": a, "check": b, "field": c, "diff": d, "count": n}
                                   for (a, b, c, d), n in clusters.most_common()],
              "fuzzy_items": fuzzy_items, "sample_for_human_review": sample(verified_rows), "rows": results}
    out = Path(os.environ.get("REPORTS_DIR") or ROOT / "reports")
    out.mkdir(exist_ok=True)
    path = out / f"verification-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    path.write_text(json.dumps(report, indent=2))
    print(f"Summary: {dict(outcome)} | failure clusters: {len(clusters)} | fuzzy: {len(fuzzy_items)}\nReport: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
