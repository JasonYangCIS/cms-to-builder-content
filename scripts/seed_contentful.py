#!/usr/bin/env python3
"""Seed Contentful with content types + DRAFT entries from scripts/seed_data.json.

Direct CMA access (the Contentful MCP exposes read-only tools to the pipeline); run by the
orchestrator/user only, never by the migrator/verifier subagents.

Safety:
  * The target must be named EXPLICITLY (CONTENTFUL_SEED_SPACE and CONTENTFUL_SEED_ENVIRONMENT in .env): there is no
    default. It REFUSES the migration's own source (mapping source.space/environment) unless you pass --allow-source,
    because seeding creates content types and entries there and would contaminate what you export. Use a sandbox.
  * Default is DRY RUN. Writes only with --apply. The target is printed first, even for a dry run.
  * Only GET and PUT. Never DELETE. Entries are never archived or overwritten. Entries are published
    only with an explicit --publish --apply, and only the seed-* entries from seed_data.json.
  * Existing content types / entries are skipped (idempotent), never modified.
  * New content types ARE published (Contentful requires that before entries can use them).
  * Token comes from .env (CONTENTFUL_MANAGEMENT_TOKEN); it is never printed.
  * Every write is appended to logs/seed-contentful.jsonl (git-ignored).

Usage: python3 scripts/seed_contentful.py [--apply] [--publish] [--allow-source]
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SEED = ROOT / "scripts" / "seed_data.json"
MAPPING = ROOT / "mapping" / "field-mapping.json"  # only read to refuse seeding the migration source
LOG = ROOT / "logs" / "seed-contentful.jsonl"
API = "https://api.contentful.com"

ENTRY_LINK_FIELDS = {"items", "componentsReference"}  # list of entry ids -> Link array
RICHTEXT_FIELDS = {"answer"}                          # plain string -> RichText doc


def load_env() -> None:
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def call(method: str, path: str, token: str, body=None, headers=None):
    assert method in ("GET", "PUT"), "only GET/PUT allowed"
    h = {"Authorization": f"Bearer {token}", "Content-Type": "application/vnd.contentful.management.v1+json"}
    h.update(headers or {})
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API + path, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read())
        except Exception:
            return e.code, {}


def log(action: str, **kw) -> None:
    LOG.parent.mkdir(exist_ok=True)
    with LOG.open("a") as f:
        f.write(json.dumps({"ts": datetime.now(timezone.utc).isoformat(), "action": action, **kw}) + "\n")


def richtext(text: str) -> dict:
    return {"nodeType": "document", "data": {}, "content": [
        {"nodeType": "paragraph", "data": {}, "content": [
            {"nodeType": "text", "value": text, "marks": [], "data": {}}]}]}


def ct_body(ct: dict) -> dict:
    fields = []
    for f in ct["fields"]:
        d = {"id": f["id"], "name": f["name"], "type": f["type"], "required": f.get("required", False),
             "localized": f.get("localized", False)}
        for k in ("linkType", "items", "validations"):
            if k in f:
                d[k] = f[k]
        fields.append(d)
    return {"name": ct["name"], "displayField": ct["displayField"], "fields": fields}


def entry_body(entry: dict, locale: str) -> dict:
    out = {}
    for k, v in entry["fields"].items():
        if k in ENTRY_LINK_FIELDS:
            v = [{"sys": {"type": "Link", "linkType": "Entry", "id": i}} for i in v]
        elif k in RICHTEXT_FIELDS:
            v = richtext(v)
        out[k] = {locale: v}
    return {"fields": out}


def main() -> int:
    apply = "--apply" in sys.argv
    load_env()
    token = os.environ.get("CONTENTFUL_MANAGEMENT_TOKEN")
    src = json.loads(MAPPING.read_text()).get("source", {}) if MAPPING.exists() else {}
    space = os.environ.get("CONTENTFUL_SEED_SPACE")
    env = os.environ.get("CONTENTFUL_SEED_ENVIRONMENT")
    if not space or not env:
        print("Name the seed target explicitly: set CONTENTFUL_SEED_SPACE and CONTENTFUL_SEED_ENVIRONMENT in .env "
              "(a sandbox space/environment, not your migration source). There is deliberately no default.", file=sys.stderr)
        return 1
    if str(space).startswith("YOUR_"):
        print("CONTENTFUL_SEED_SPACE still has a placeholder value", file=sys.stderr)
        return 1
    if not token:
        print("Missing CONTENTFUL_MANAGEMENT_TOKEN in .env", file=sys.stderr)
        return 1
    if space == src.get("space") and env == (src.get("environment") or "master") and "--allow-source" not in sys.argv:
        print(f"Refusing to seed the migration's SOURCE (space {space}, environment {env}): seeding creates content types and entries "
              "there and would contaminate what you export. Point CONTENTFUL_SEED_SPACE/ENVIRONMENT at a sandbox, or pass "
              "--allow-source if you really mean it.", file=sys.stderr)
        return 1
    seed = json.loads(SEED.read_text())
    locale = seed["locale"]
    base = f"/spaces/{space}/environments/{env}"
    print(f"{'APPLY' if apply else 'DRY RUN'}: space={space} env={env} locale={locale}")

    if "--publish" in sys.argv:
        # Publish ONLY seed entries (ids from seed_data.json, which are all 'seed-*'), in file order
        # (dependencies first: Contentful rejects publishing an entry with unpublished links).
        for e in seed["entries"]:
            if not e["id"].startswith("seed-"):
                continue
            status, cur = call("GET", f"{base}/entries/{e['id']}", token)
            if status != 200:
                print(f"  ERROR  entry {e['id']}: GET returned {status}", file=sys.stderr)
                return 1
            sysd = cur["sys"]
            if sysd.get("publishedVersion") and sysd["version"] == sysd["publishedVersion"] + 1:
                print(f"  skip   publish {e['id']} (already published, no changes)")
                continue
            print(f"  publish entry {e['id']}")
            if apply:
                s, res = call("PUT", f"{base}/entries/{e['id']}/published", token,
                              headers={"X-Contentful-Version": str(sysd["version"])})
                if s not in (200, 201):
                    print(f"  ERROR  publish {e['id']}: {s} {res.get('message', '')} {res.get('details', '')}", file=sys.stderr)
                    return 1
                log("publish_entry", id=e["id"], space=space, env=env)
        print("Done." if apply else "Dry run only. Re-run with --publish --apply to publish.")
        return 0

    for ct in seed["content_types"]:
        status, _ = call("GET", f"{base}/content_types/{ct['id']}", token)
        if status == 200:
            print(f"  skip   content_type {ct['id']} (exists)")
            continue
        if status not in (404,):
            print(f"  ERROR  content_type {ct['id']}: GET returned {status}", file=sys.stderr)
            return 1
        print(f"  create content_type {ct['id']} (+publish)")
        if apply:
            s, res = call("PUT", f"{base}/content_types/{ct['id']}", token, ct_body(ct))
            if s not in (200, 201):
                print(f"  ERROR  create {ct['id']}: {s} {res.get('message', '')}", file=sys.stderr)
                return 1
            s, res = call("PUT", f"{base}/content_types/{ct['id']}/published", token,
                          headers={"X-Contentful-Version": str(res["sys"]["version"])})
            if s not in (200, 201):
                print(f"  ERROR  publish {ct['id']}: {s} {res.get('message', '')}", file=sys.stderr)
                return 1
            log("create_content_type", id=ct["id"], space=space, env=env)

    for e in seed["entries"]:
        status, _ = call("GET", f"{base}/entries/{e['id']}", token)
        if status == 200:
            print(f"  skip   entry {e['id']} ({e['content_type']}, exists)")
            continue
        if status != 404:
            print(f"  ERROR  entry {e['id']}: GET returned {status}", file=sys.stderr)
            return 1
        print(f"  create entry {e['id']} ({e['content_type']}) as draft")
        if apply:
            s, res = call("PUT", f"{base}/entries/{e['id']}", token, entry_body(e, locale),
                          headers={"X-Contentful-Content-Type": e["content_type"]})
            if s not in (200, 201):
                print(f"  ERROR  entry {e['id']}: {s} {res.get('message', '')} {res.get('details', '')}", file=sys.stderr)
                return 1
            log("create_entry", id=e["id"], content_type=e["content_type"], space=space, env=env)
    print("Done." if apply else "Dry run only. Re-run with --apply to write.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
