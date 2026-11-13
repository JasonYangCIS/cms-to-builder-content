#!/usr/bin/env python3
"""Stage 7 (part 2): redirect map + SEO field check. Read-only (Builder listing + local exports); no LLM.

REDIRECTS. For every exported page entry: source URL (slug) vs the URL its Builder draft actually has.
  same            Builder URL == source URL
  rewritten       Builder URL differs on purpose (mapping `constant_map`, e.g. "/" -> "/home-contentful-migration");
                  `url_changed` = true: decide to redirect old -> new, or replace the Builder page already at the old URL
  url-mismatch    Builder URL is neither the source URL nor what the mapping predicts (investigate)
  not-migrated    no Builder draft yet; shows the URL it WOULD get (free / collision with an existing Builder page)
  target-missing  the manifest has a Builder id but that page is no longer listed (archived/deleted in Builder)
  Also reports whether the SOURCE url is currently served by a different Builder page (collision).
SEO. Lists source fields that look like SEO/meta (seo, meta, canonical, og/opengraph, noindex, robots, keywords,
  description), whether the mapping carries them, and gaps versus the Builder page model.
Output: reports/redirects-<ts>.csv and reports/seo-redirect-<ts>.json (git-ignored).
Usage: python3 scripts/seo_redirect_check.py
"""
from __future__ import annotations

import csv
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "transform"))
import run_migration as rm  # noqa: E402
import slug as slugmod  # noqa: E402

SEO_RE = re.compile(r"seo|meta|canonical|opengraph|open_graph|(^|[a-z])og([A-Z_]|$)|noindex|robots|keyword|description", re.I)
# Builder `page` model fields relevant to SEO (from the Builder MCP list_builder_models, 2026-11-13). Refresh if the model changes.
BUILDER_PAGE_SEO_FIELDS = ["title", "description"]


def analyze(entries: dict, mapping: dict, locale: str, rows: dict, urls: dict) -> list[dict]:
    """One redirect-map row per top-level (page) entry. `urls`: builder url -> {id, model}; includes drafts."""
    by_id = {v["id"]: u for u, v in urls.items() if v.get("id")}
    out = []
    for sid, e in sorted(entries.items()):
        ct = e["sys"]["contentType"]["sys"]["id"]
        cmap = mapping["content_types"].get(ct)
        if not cmap or cmap.get("inline_into"):
            continue
        spec = next((s for s in cmap["fields"] if s.get("type") == "slug"), None)
        if spec is None:
            continue
        raw = rm.value_of(e, spec["source"], locale)
        src_url = slugmod.normalize(raw)
        row = rows.get(sid) or {}
        tid = row.get("target_id")
        title = rm.value_of(e, "title", locale)
        owner = urls.get(src_url, {}).get("id")
        rec = {"source_id": sid, "title": title, "source_url": src_url, "builder_id": tid or "", "builder_url": "",
               "status": "", "url_changed": False, "note": ""}
        live = by_id.get(tid) if tid else None
        expected, _ = slugmod.resolve(raw, spec.get("constant_map"), urls.keys(), live)  # a key view is trusted as normalized: nothing is rebuilt per page
        if live:
            rec["builder_url"] = live
            if live == src_url:
                rec["status"] = "same"
            elif live == expected["url"]:
                rec["status"] = "rewritten"
                rec["url_changed"] = True
                rec["note"] = "URL rewritten by mapping; decide redirect or replacing the page at the old URL"
            else:
                rec["status"] = "url-mismatch"
                rec["url_changed"] = True
                rec["note"] = f"Builder has {live}, mapping predicts {expected['url']}"
        elif tid:
            rec["status"] = "target-missing"
            rec["note"] = f"Builder id {tid} is not among live/draft pages (archived or deleted in Builder)"
        else:
            rec["status"] = "not-migrated"
            rec["builder_url"] = expected["url"]
            rec["note"] = f"would get {expected['url']} ({expected['status']})"
            rec["url_changed"] = expected["url"] != src_url
        if owner and owner != tid:
            rec["note"] = (rec["note"] + "; " if rec["note"] else "") + f"source URL {src_url} is already served by Builder page {owner}"
        out.append(rec)
    return out


def seo_fields(content_types: list, mapping: dict) -> dict:
    found = {}
    for ct in content_types:
        tid = ct["sys"]["id"]
        mapped = {s["source"]: s for s in mapping["content_types"].get(tid, {}).get("fields", [])}
        hits = []
        for f in ct["fields"]:
            if SEO_RE.search(f["id"]) or SEO_RE.search(f.get("name", "")):
                s = mapped.get(f["id"])
                hits.append({"field": f["id"], "mapped": bool(s and s.get("type") != "dropped"),
                             "target": (s or {}).get("target")})
        if hits:
            found[tid] = hits
    return found


def main() -> int:
    rm.load_env()
    key = os.environ.get("BUILDER_CONTENT_MCP_PK")
    if not key:
        print("Missing BUILDER_CONTENT_MCP_PK in .env", file=sys.stderr)
        return 1
    mapping = json.loads((ROOT / "mapping" / "field-mapping.json").read_text())
    rm.check_configured(mapping)
    locale, space = mapping["source"]["locale"], mapping["target"]["space"]
    entries = {}
    for line in (ROOT / "exports" / "entries.jsonl").read_text().splitlines():
        if line.strip():
            e = json.loads(line)
            entries[e["sys"]["id"]] = e
    types = json.loads((ROOT / "exports" / "content_types.json").read_text())
    rows = {r["source_id"]: r for r in rm.manifest("list")["rows"]}
    urls = rm.existing_urls(key, space)

    redirects = analyze(entries, mapping, locale, rows, urls)
    seo = seo_fields(types, mapping)
    titles_missing = [r["source_id"] for r in redirects if not r["title"]]
    unmapped_seo = [f"{ct}.{h['field']}" for ct, hs in seo.items() for h in hs if not h["mapped"]]
    gaps = []
    if unmapped_seo:
        gaps.append(f"SEO-like source fields not mapped to Builder: {', '.join(unmapped_seo)}")
    if not any(h["mapped"] and (h["target"] or "").endswith("description") for hs in seo.values() for h in hs):
        gaps.append("No source field is mapped to Builder page `description` (the meta description); Builder pages will have none")
    gaps.append("No canonical/OG/noindex fields exist in the Builder `page` model (only: " + ", ".join(BUILDER_PAGE_SEO_FIELDS) + ")")
    if not seo:
        gaps.append("The source content types have no SEO/meta fields at all")
    if titles_missing:
        gaps.append(f"{len(titles_missing)} page(s) have no title in the source: {', '.join(titles_missing)}")

    out = Path(os.environ.get("REPORTS_DIR") or ROOT / "reports")
    out.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    csv_path = out / f"redirects-{ts}.csv"
    with csv_path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(redirects[0].keys()) if redirects else ["source_id"])
        w.writeheader()
        w.writerows(redirects)
    json_path = out / f"seo-redirect-{ts}.json"
    json_path.write_text(json.dumps({"redirects": redirects, "seo_fields": seo, "gaps": gaps}, indent=2))

    counts: dict[str, int] = {}
    for r in redirects:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    print(f"Pages checked: {len(redirects)} | {counts} | URL changes: {sum(r['url_changed'] for r in redirects)}")
    for r in redirects:
        print(f"  {r['source_url']:<28} -> {r['builder_url']:<28} [{r['status']}]" + (f"  {r['note']}" if r["note"] else ""))
    print("SEO fields in source:", {k: [h['field'] for h in v] for k, v in seo.items()} or "none")
    for g in gaps:
        print("  GAP:", g)
    print(f"  CSV: {csv_path}\n  Report: {json_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
