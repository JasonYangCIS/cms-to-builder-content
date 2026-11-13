#!/usr/bin/env python3
"""Bulk, READ-ONLY export + inventory of a Contentful space (Stage 1 of docs/bulk-plan.md).

Uses the Content Delivery API (published) and, if available, the Preview API (published + drafts).
Never writes to Contentful. Output goes to exports/ (git-ignored).

Tokens come from .env and are never printed:
  CONTENTFUL_DELIVERY_TOKEN, CONTENTFUL_PREVIEW_TOKEN (optional)
Space and environment default to source.space / source.environment in mapping/field-mapping.json;
CONTENTFUL_SPACE_ID / CONTENTFUL_SOURCE_ENVIRONMENT in .env override them.

Usage:
  python3 scripts/export_contentful.py            # DRY RUN: counts only (delivery vs preview = draft delta)
  python3 scripts/export_contentful.py --export   # writes exports/*.json(l) and the inventory report
  option:  --delivery  force the Delivery API (published only). Without it the Preview API (published + drafts) is used
           whenever a preview token exists.
Everything is fetched into memory first, then staged under temporary names and swapped into place with inventory.json LAST,
so an interrupted export never leaves a truncated entries.jsonl; inventory.json is the "export complete" marker.
"""
from __future__ import annotations

from typing import Any
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "exports"
PAGE = 1000  # CDA max page size


def load_env() -> None:
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


class Api:
    TIMEOUT = 60  # seconds per request
    BACKOFF = (2, 5, 15, 60)

    def __init__(self, host: str, space: str, env: str, token: str):
        q = lambda s: urllib.parse.quote(str(s), safe="")  # ids go into the URL path
        self.base, self.token = f"https://{host}/spaces/{q(space)}/environments/{q(env)}", token

    def get(self, path: str, **params):
        """Read-only GET with retries (429, 5xx, network errors) and a timeout. Exits with a clear message when it gives up."""
        url = f"{self.base}{path}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {self.token}"})
        for attempt in range(len(self.BACKOFF) + 1):
            more = attempt < len(self.BACKOFF)
            try:
                with urllib.request.urlopen(req, timeout=self.TIMEOUT) as r:
                    return json.loads(r.read())
            except urllib.error.HTTPError as e:
                if more and (e.code == 429 or e.code >= 500):
                    time.sleep(self.BACKOFF[attempt])
                    continue
                raise SystemExit(f"HTTP {e.code} on GET {path}: {e.read()[:200]!r}") from e
            except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError) as e:
                if more:
                    time.sleep(self.BACKOFF[attempt])
                    continue
                raise SystemExit(f"network error on GET {path}: {e}") from e
            except ValueError as e:
                raise SystemExit(f"unreadable reply on GET {path}: {e}") from e
        raise SystemExit(f"gave up on GET {path}")

    def paged(self, path: str, **params):
        skip = 0
        while True:
            d = self.get(path, limit=PAGE, skip=skip, order="sys.id", **params)
            yield from d["items"]
            skip += PAGE
            if skip >= d["total"]:
                return


def stage(final: Path, write) -> tuple[Path, Path]:
    """Write `final` to a temporary sibling first (never truncating the real file); the caller swaps it in with os.replace."""
    tmp = final.with_name(final.name + ".tmp")
    try:
        with tmp.open("w") as f:
            write(f)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return tmp, final


def walk(obj):
    """Yield every dict/list node under obj."""
    stack = [obj]
    while stack:
        cur = stack.pop()
        yield cur
        if isinstance(cur, dict):
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)


def inventory(types, entries, assets, locales) -> dict:
    ct_fields = {t["sys"]["id"]: t for t in types}
    per_ct: defaultdict[str, dict[str, Any]] = defaultdict(lambda: {"count": 0, "localized_fields": [], "richtext_fields": [], "link_fields": []})
    for tid, t in ct_fields.items():
        for f in t["fields"]:
            if f.get("localized"):
                per_ct[tid]["localized_fields"].append(f["id"])
            if f["type"] == "RichText":
                per_ct[tid]["richtext_fields"].append(f["id"])
            if f["type"] == "Link" or (f["type"] == "Array" and f.get("items", {}).get("type") == "Link"):
                per_ct[tid]["link_fields"].append(f["id"])
    node_types: Counter[str] = Counter()
    mark_types: Counter[str] = Counter()
    embedded: Counter[str] = Counter()
    graph, slugs, asset_links = defaultdict(set), defaultdict(list), 0
    locale_use: Counter[str] = Counter()
    for e in entries:
        tid, eid = e["sys"]["contentType"]["sys"]["id"], e["sys"]["id"]
        per_ct[tid]["count"] += 1
        for fname, byloc in e.get("fields", {}).items():
            for loc, val in (byloc.items() if isinstance(byloc, dict) else []):
                locale_use[loc] += 1
                for n in walk(val):
                    if isinstance(n, dict):
                        if n.get("nodeType"):
                            node_types[n["nodeType"]] += 1
                            if n["nodeType"].startswith("embedded-"):
                                embedded[n["nodeType"]] += 1
                        for m in n.get("marks", []) if isinstance(n.get("marks"), list) else []:
                            if isinstance(m, dict) and "type" in m:
                                mark_types[m["type"]] += 1
                        s = n.get("sys")
                        if isinstance(s, dict) and s.get("type") == "Link":
                            if s.get("linkType") == "Entry":
                                graph[eid].add(s["id"])
                            elif s.get("linkType") == "Asset":
                                asset_links += 1
                if fname == "slug" and isinstance(val, str):
                    slugs[(loc, val)].append(eid)

    depth_cache: dict = {}

    def depth(n, seen=()):
        if n in depth_cache:
            return depth_cache[n]
        if n in seen:
            return 0  # cycle
        d = 1 + max((depth(c, seen + (n,)) for c in graph.get(n, ())), default=0)
        depth_cache[n] = d
        return d

    max_depth = max((depth(e["sys"]["id"]) for e in entries), default=0)
    inbound = Counter(c for cs in graph.values() for c in cs)
    shared = {k: v for k, v in inbound.items() if v > 1}
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "totals": {"entries": len(entries), "assets": len(assets), "content_types": len(types), "locales": [ln["code"] for ln in locales]},
        "per_content_type": {k: v for k, v in sorted(per_ct.items())},
        "locale_value_counts": dict(locale_use),
        "richtext": {"node_types": dict(node_types), "mark_types": dict(mark_types), "embedded": dict(embedded)},
        "references": {"entry_links": sum(len(v) for v in graph.values()), "asset_links": asset_links,
                       "max_reference_depth": max_depth, "entries_referenced_by_more_than_one": len(shared)},
        "duplicate_slugs_in_source": {f"{loc}:{s}": ids for (loc, s), ids in slugs.items() if len(ids) > 1},
        "slugs_to_check_against_live_builder": sorted({s for (_, s) in slugs}),
    }


def main() -> int:
    load_env()
    mp = ROOT / "mapping" / "field-mapping.json"
    src = json.loads(mp.read_text()).get("source", {}) if mp.exists() else {}
    space = os.environ.get("CONTENTFUL_SPACE_ID") or src.get("space")  # mapping is the source of truth; env overrides
    env = os.environ.get("CONTENTFUL_SOURCE_ENVIRONMENT") or src.get("environment") or "master"
    if str(space or "").startswith("YOUR_"):
        print("Set source.space in mapping/field-mapping.json (copy mapping/field-mapping.example.json) "
              "or CONTENTFUL_SPACE_ID in .env", file=sys.stderr)
        return 1
    dtoken, ptoken = os.environ.get("CONTENTFUL_DELIVERY_TOKEN"), os.environ.get("CONTENTFUL_PREVIEW_TOKEN")
    missing = [n for n, v in (("CONTENTFUL_SPACE_ID", space), ("CONTENTFUL_DELIVERY_TOKEN or CONTENTFUL_PREVIEW_TOKEN", dtoken or ptoken)) if not v]
    if missing:
        print("Missing in .env: " + ", ".join(missing), file=sys.stderr)
        return 1
    assert space is not None  # guaranteed by the `missing` check above
    cda = Api("cdn.contentful.com", space, env, dtoken) if dtoken else None
    cpa = Api("preview.contentful.com", space, env, ptoken) if ptoken else None
    use = cpa if (cpa and "--delivery" not in sys.argv) else cda
    assert use is not None  # at least one token exists (checked above), so one client exists
    label = "preview (published + drafts)" if use is cpa else "delivery (published only)"
    print(f"space={space} env={env} source={label}")

    types = list(use.paged("/content_types"))
    print(f"content types: {len(types)}")
    for t in types:
        tid = t["sys"]["id"]
        line = f"  {tid:<32}"
        if cda:
            line += f" published={cda.get('/entries', content_type=tid, limit=1)['total']}"
        if cpa:
            line += f" preview={cpa.get('/entries', content_type=tid, limit=1)['total']}"
        print(line)
    if cda and cpa:
        draft_delta = cpa.get("/entries", limit=1)["total"] - cda.get("/entries", limit=1)["total"]
        print(f"entries only in preview (unpublished drafts): {draft_delta}")
    print(f"assets: {use.get('/assets', limit=1)['total']}   locales: " +
          ", ".join(ln["code"] for ln in use.get("/locales")["items"]))

    if "--export" not in sys.argv:
        print("Dry run only. Re-run with --export to write exports/.")
        return 0

    OUT.mkdir(exist_ok=True)
    locales = use.get("/locales")["items"]
    entries = list(use.paged("/entries", locale="*", include=0))  # everything is fetched BEFORE any file is touched
    assets = list(use.paged("/assets", locale="*"))
    inv = inventory(types, entries, assets, locales)
    inv["source"] = label
    staged = []  # (temporary file, final file); inventory.json is last = the "export complete" marker
    try:
        staged.append(stage(OUT / "content_types.json", lambda f: f.write(json.dumps(types, indent=2))))
        staged.append(stage(OUT / "locales.json", lambda f: f.write(json.dumps(locales, indent=2))))
        for name, rows in (("entries", entries), ("assets", assets)):
            staged.append(stage(OUT / f"{name}.jsonl", lambda f, rows=rows: f.writelines(json.dumps(r) + "\n" for r in rows)))
        staged.append(stage(OUT / "inventory.json", lambda f: f.write(json.dumps(inv, indent=2))))
    except BaseException:
        for tmp, _ in staged:
            tmp.unlink(missing_ok=True)
        raise
    for tmp, final in staged:
        os.replace(tmp, final)
    print(f"Wrote {len(entries)} entries, {len(assets)} assets to {OUT}/ (inventory.json)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
