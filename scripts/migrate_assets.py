#!/usr/bin/env python3
"""Upload Contentful assets that entries actually reference to Builder's Upload API (Stage 4 of docs/bulk-plan.md).

Scope: ONLY assets linked from exported entries (exports/entries.jsonl); unreferenced assets are ignored.
Idempotent: exports/asset_map.json ({asset_id: {builder_url, sha256, ...}}) records uploads; an asset whose
sha256 is already mapped is skipped. DRY RUN by default; --apply uploads.

Safety: uploads only (no delete); key from .env (BUILDER_CONTENT_MCP_PK) is never printed; each upload is
appended to logs/writes.jsonl via manifest log-write (source-id = Contentful asset id; a failed audit line is reported).
Downloads are limited to https://*.ctfassets.net (Contentful's asset CDN) and every request has a timeout, so a tampered
export cannot make this script read local files (file://) or hang. If an upload's outcome is unknown (timeout, reset, 5xx)
it stops and tells you to check the Asset Library before re-running: a blind retry could upload the same file twice.

Usage: python3 scripts/migrate_assets.py [--apply] [--locale en-US]
"""
from __future__ import annotations

import hashlib
import json
import os
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import manifest as mf  # noqa: E402

EXPORTS = ROOT / "exports"
MAP = EXPORTS / "asset_map.json"
UPLOAD = "https://builder.io/api/v1/upload"
ALLOWED_HOST_SUFFIXES = (".ctfassets.net",)  # Contentful's asset CDN (images./assets./videos./downloads.[eu.]ctfassets.net)
DOWNLOAD_TIMEOUT, UPLOAD_TIMEOUT = 60, 180  # seconds


def check_asset_url(url: str) -> str:
    """Only https URLs on Contentful's asset CDN may be downloaded; anything else raises ValueError."""
    parts = urllib.parse.urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or not any(host.endswith(s) for s in ALLOWED_HOST_SUFFIXES):
        raise ValueError(f"refusing to download {url[:80]!r}: only https URLs on {', '.join('*' + s for s in ALLOWED_HOST_SUFFIXES)} are allowed")
    return url


def load_env() -> None:
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def referenced_asset_ids(entries) -> set[str]:
    ids, stack = set(), []
    for e in entries:
        stack.append(e.get("fields", {}))
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            s = cur.get("sys")
            if isinstance(s, dict) and s.get("type") == "Link" and s.get("linkType") == "Asset":
                ids.add(s["id"])
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)
    return ids


def pick(field: dict, locale: str):
    return (field or {}).get(locale)


def main() -> int:
    apply = "--apply" in sys.argv
    locale = sys.argv[sys.argv.index("--locale") + 1] if "--locale" in sys.argv else "en-US"
    load_env()
    key = os.environ.get("BUILDER_CONTENT_MCP_PK")
    if apply and not key:
        print("Missing BUILDER_CONTENT_MCP_PK in .env", file=sys.stderr)
        return 1
    entries = [json.loads(ln) for ln in (EXPORTS / "entries.jsonl").read_text().splitlines() if ln.strip()]
    assets = {a["sys"]["id"]: a for a in (json.loads(ln) for ln in (EXPORTS / "assets.jsonl").read_text().splitlines() if ln.strip())}
    wanted = sorted(referenced_asset_ids(entries))
    amap = json.loads(MAP.read_text()) if MAP.exists() else {}
    print(f"{'APPLY' if apply else 'DRY RUN'}: {len(wanted)} referenced asset(s) of {len(assets)} exported (others ignored)")

    for aid in wanted:
        a = assets.get(aid)
        if not a:
            print(f"  ERROR  {aid}: referenced but missing from exports/assets.jsonl", file=sys.stderr)
            return 1
        f = a["fields"]
        file = pick(f["file"], locale)
        if not file:
            print(f"  ERROR  {aid}: no file for locale {locale}", file=sys.stderr)
            return 1
        url = "https:" + file["url"] if file["url"].startswith("//") else file["url"]
        title, alt = pick(f.get("title"), locale) or "", pick(f.get("description"), locale) or ""
        try:
            data = urllib.request.urlopen(check_asset_url(url), timeout=DOWNLOAD_TIMEOUT).read()  # public Contentful CDN URL
        except (ValueError, urllib.error.URLError, socket.timeout, TimeoutError, OSError) as e:
            print(f"  ERROR  {aid}: could not download the source file: {e}", file=sys.stderr)
            return 1
        sha = hashlib.sha256(data).hexdigest()
        prev = amap.get(aid)
        if prev and prev.get("sha256") == sha:
            print(f"  skip   {aid} ({file['fileName']}): already uploaded -> {prev['builder_url']}")
            continue
        print(f"  upload {aid} ({file['fileName']}, {file['contentType']}, {len(data)} bytes, sha256 {sha[:12]}...)")
        if not apply:
            continue
        q = urllib.parse.urlencode({"name": file["fileName"], "altText": alt, "title": title})
        req = urllib.request.Request(f"{UPLOAD}?{q}", data=data, method="POST",
                                     headers={"Authorization": f"Bearer {key}", "Content-Type": file["contentType"]})
        try:
            with urllib.request.urlopen(req, timeout=UPLOAD_TIMEOUT) as r:
                body = json.loads(r.read())
        except urllib.error.HTTPError as e:
            kind = "rejected" if e.code < 500 else "outcome unknown"
            print(f"  ERROR  upload {aid}: HTTP {e.code} ({kind}) {e.read()[:200]!r}", file=sys.stderr)
            if e.code >= 500:
                print(f"         Check Builder's Asset Library for {file['fileName']!r} before re-running (a blind retry could upload it twice).", file=sys.stderr)
            return 1
        except (urllib.error.URLError, socket.timeout, TimeoutError, OSError, ValueError) as e:
            print(f"  ERROR  upload {aid}: outcome unknown ({e}). Check Builder's Asset Library for {file['fileName']!r} before re-running "
                  "(a blind retry could upload it twice).", file=sys.stderr)
            return 1
        if not isinstance(body, dict) or "url" not in body:
            print(f"  ERROR  upload {aid}: no url in response: {str(body)[:200]}", file=sys.stderr)
            return 1
        amap[aid] = {"builder_url": body["url"], "sha256": sha, "source_url": url, "name": file["fileName"],
                     "uploaded_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        MAP.write_text(json.dumps(amap, indent=2) + "\n")  # persist before logging (crash-safe)
        try:
            mf.run(["log-write", "--source-id", aid, "--action", "upload-asset", "--target-id", body["url"]])
        except RuntimeError as e:
            print(f"  WARNING {aid}: uploaded and recorded in the asset map, but the audit line failed: {e}", file=sys.stderr)
        print(f"  done   {aid} -> {body['url']}")
    print("Done." if apply else "Dry run only. Re-run with --apply to upload.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
