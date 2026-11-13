#!/usr/bin/env python3
"""Deterministic migration runner (Stage 5 of docs/bulk-plan.md): no LLM in the loop.

Claims manifest rows, builds Builder DRAFT payloads from exports/ + the approved mapping + the
transformers in scripts/transform/, writes them with the Builder Write API, and records every write.

Safety (enforced in code, not by convention):
  * DRY RUN by default; writes only with --apply. The mapping gate (mapping.py check) must pass.
  * Builder calls are limited to GET (listing/lookup), POST (create) and PATCH (update). No DELETE. New entries are
    created with published="draft"; an update first reads the target and REFUSES unless it is still a draft, so live
    content is never edited and publish state is never changed.
  * A create whose outcome is unknown (timeout, connection reset, 5xx, no id in the reply) is never blindly retried
    or recreated: the runner looks the draft up by URL and adopts it, or stops the row for a human (needs_human).
  * Right after a successful write the target id is saved (compare-and-set on the claim); if that fails the batch
    stops loudly and the id is written to logs/runner-unsaved.jsonl.
  * Anything a transformer flags (slug collision, rich-text exception, unmapped type, missing link) is NOT
    guessed: the row goes to needs_human with the reason.
  * Rows are claimed via manifest.py only; claims that are not finished are released back to pending.
  * Key from .env (BUILDER_CONTENT_MCP_PK), never printed. Every write -> manifest.py log-write.

Usage:
  python3 scripts/run_migration.py [--apply] [--limit 10] [--content-type page] [--delay 0.5]
  python3 scripts/run_migration.py --preview SOURCE_ID    # print the payload for one entry; no claim, no write
"""
from __future__ import annotations

from typing import Any
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "transform"))
import localeval  # noqa: E402
import manifest as mf  # noqa: E402
import refs  # noqa: E402
import richtext  # noqa: E402
import slug as slugmod  # noqa: E402

WRITE = "https://builder.io/api/v1/write"
CONTENT = "https://cdn.builder.io/api/v3/content"
PAGE_MODELS = ["page", "blog-post", "collection", "product"]
BACKOFF = (2, 5, 15, 60)
HTTP_TIMEOUT = 30.0  # seconds; overridable with --timeout


# ---------------------------------------------------------------- environment / manifest

def check_configured(mapping: dict) -> None:
    """Stop with a clear message if the mapping still has YOUR_* placeholders (i.e. it is the unedited example)."""
    bad = [f"{k}.space={mapping.get(k, {}).get('space')}" for k in ("source", "target")
           if str(mapping.get(k, {}).get("space", "")).startswith("YOUR_")]
    if bad:
        raise SystemExit("mapping/field-mapping.json still has placeholders (" + ", ".join(bad) + "). Copy "
                         "mapping/field-mapping.example.json, fill in your space ids, review it, then run "
                         "`python3 scripts/mapping.py approve`.")


def load_env() -> None:
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def manifest(*args) -> dict:
    """The manifest CLI, in-process (same code path, no Python start per call). Raises RuntimeError on failure."""
    return mf.run(list(args))


# ---------------------------------------------------------------- payload building (pure, testable)

def value_of(entry: dict, field: str, locale: str):
    return localeval.unwrap(entry.get("fields", {}).get(field), locale)


def option_name(target: str):
    return target.split(".options.", 1)[1] if ".options." in target else None


def convert(spec: dict, raw, exc: list, where: str):
    """One field value -> Builder value, by mapping field type. Flags instead of guessing."""
    t = spec.get("type", "text")
    if raw in (None, "", []):
        return None
    if t in ("text", "number", "boolean", "json"):
        return raw
    if t == "richtext":
        html, rexc = richtext.convert(raw)
        exc.extend({"where": where, **e} for e in rexc)
        return html
    if t == "handle_list":
        return [{spec.get("item_key", "value"): h} for h in raw]
    exc.append({"where": where, "reason": f"field type {t!r} is not supported by the runner"})
    return None


def build_blocks(page: dict, entries: dict, mapping: dict, locale: str, exc: list):
    """componentsReference -> Builder blocks (in order). Returns (blocks, child_source_ids)."""
    cts, blocks, children = mapping["content_types"], [], []
    ids = [refs.link_id(x) for x in (value_of(page, "componentsReference", locale) or [])]
    for sid in ids:
        child = entries.get(sid)
        if child is None:
            exc.append({"where": sid, "reason": "linked entry missing from exports"})
            continue
        ct = child["sys"]["contentType"]["sys"]["id"]
        cmap = cts.get(ct)
        if not cmap or not cmap.get("target_component"):
            exc.append({"where": sid, "reason": f"content type {ct!r} has no target_component in the mapping"})
            continue
        children.append(sid)
        options = {}
        for spec in cmap.get("fields", []):
            if spec.get("type") == "dropped":
                continue
            opt = option_name(spec.get("target", ""))
            if opt is None:
                exc.append({"where": f"{sid}.{spec['source']}", "reason": "target is not a component option"})
                continue
            raw = value_of(child, spec["source"], locale)
            if spec.get("type") == "inline":  # e.g. FAQ items: child entries inlined as option sub-objects
                items = []
                for item_id in [refs.link_id(x) for x in (raw or [])]:
                    item = entries.get(item_id)
                    if item is None:
                        exc.append({"where": item_id, "reason": "linked entry missing from exports"})
                        continue
                    children.append(item_id)
                    obj = {}
                    for ispec in cts.get(item["sys"]["contentType"]["sys"]["id"], {}).get("fields", []):
                        if ispec.get("type") == "dropped" or ".items.{item}." not in ispec.get("target", ""):
                            continue
                        v = convert(ispec, value_of(item, ispec["source"], locale), exc, f"{item_id}.{ispec['source']}")
                        if v is not None:
                            obj[ispec["target"].split(".items.{item}.", 1)[1]] = v
                    items.append(obj)
                if items:
                    options[opt] = items
                continue
            v = convert(spec, raw, exc, f"{sid}.{spec['source']}")
            if v is not None:
                options[opt] = v
        blocks.append({"@type": "@builder.io/sdk:Element",
                       "component": {"name": cmap["target_component"], "options": options}})
    return blocks, children


def build_page(entry: dict, entries: dict, mapping: dict, locale: str, existing_urls, own_url=None):
    """-> (payload, exceptions, child_ids, url_status). Payload has no `published` key; the writer adds it."""
    exc: list = []
    ct = entry["sys"]["contentType"]["sys"]["id"]
    cmap = mapping["content_types"].get(ct)
    if not cmap or cmap.get("inline_into"):
        return None, [{"where": entry["sys"]["id"], "reason": f"{ct!r} is not a top-level mapped type"}], [], None
    sid = entry["sys"]["id"]
    name = (cmap.get("target_name_by_entry", {}).get(sid) or cmap.get("target_name")
            or (value_of(entry, cmap["target_name_from"], locale) if cmap.get("target_name_from") else None))
    data: dict = {}
    url_status = None
    children: list = []
    for spec in cmap.get("fields", []):
        tgt = spec.get("target")
        if spec.get("type") == "dropped" or not tgt:
            continue
        if spec["type"] == "slug":
            res, sexc = slugmod.resolve(value_of(entry, spec["source"], locale), spec.get("constant_map"),
                                        existing_urls, own_url)
            exc.extend({"where": sid, **e} for e in sexc)
            data[tgt.split(".", 1)[1]] = res["url"]
            url_status = res["status"]
        elif spec["type"] == "inline" and tgt == "data.blocks":
            blocks, children = build_blocks(entry, entries, mapping, locale, exc)
            data["blocks"] = blocks
        else:
            v = convert(spec, value_of(entry, spec["source"], locale), exc, f"{sid}.{spec['source']}")
            if v is not None:
                data[tgt.split(".", 1)[1]] = v
    if not name:
        exc.append({"where": sid, "reason": "no entry name could be derived from the mapping"})
    return {"name": name, "data": data}, exc, children, url_status


# ---------------------------------------------------------------- Builder I/O

class BuilderError(RuntimeError):
    """Any Builder call failure. Subclasses say what is known about the outcome."""


class HttpError(BuilderError):
    """The request definitely failed (or was a read)."""


class AmbiguousWriteError(BuilderError):
    """A create may or may not have reached Builder (timeout, reset, 5xx, unusable reply): never retry or recreate blindly."""


class PublishedTarget(BuilderError):
    """The update target is not a draft (published, archived or missing): refuse to touch it."""


def http(method: str, url: str, key: str, body=None):
    """GET/POST/PATCH only. Retries (2s, 5s, 15s, 60s) apply to GET and PATCH on 429/5xx/network errors and to POST on 429
    only (a 429 means the request was not processed). Any other POST failure is an AmbiguousWriteError."""
    assert method in ("GET", "POST", "PATCH"), "runner never issues other methods (no DELETE)"
    if body is not None:
        assert body.get("published", "draft") == "draft", "runner only ever writes drafts"
    req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    where = f"{method} {url.split('?')[0]}"
    for attempt in range(len(BACKOFF) + 1):
        more = attempt < len(BACKOFF)
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            if more and (e.code == 429 or (method != "POST" and e.code >= 500)):
                time.sleep(BACKOFF[attempt])
                continue
            cls = AmbiguousWriteError if (method == "POST" and e.code >= 500) else HttpError
            raise cls(f"HTTP {e.code} {where}: {e.read()[:200]!r}") from e
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError) as e:
            if more and method != "POST":
                time.sleep(BACKOFF[attempt])
                continue
            cls = AmbiguousWriteError if method == "POST" else HttpError
            raise cls(f"network error on {where}: {e}") from e
        except ValueError as e:  # a 2xx whose body is not JSON
            cls = AmbiguousWriteError if method == "POST" else HttpError
            raise cls(f"unreadable reply from {where}: {e}") from e
    raise HttpError(f"gave up on {where}")


def published_state(key: str, space: str, model: str, target_id: str):
    """'draft' | 'published' | 'archived' | None (not found) for one entry."""
    q = urllib.parse.urlencode({"apiKey": space, "query.id": target_id, "includeUnpublished": "true", "limit": 1,
                                "fields": "id,published", "cachebust": "true"})
    res = http("GET", f"{CONTENT}/{model}?{q}", key).get("results", [])
    return res[0].get("published") if res else None


def find_draft(key: str, space: str, model: str, name: str, url: str):
    """After an ambiguous create: the id of THE draft with this name at this URL, or None (also None if it is not unique)."""
    q = urllib.parse.urlencode({"apiKey": space, "query.data.url": url, "includeUnpublished": "true", "limit": 10,
                                "omit": "data.blocks,data.blocksString", "cachebust": "true"})
    try:
        res = http("GET", f"{CONTENT}/{model}?{q}", key).get("results", [])
    except BuilderError:
        return None
    hits = [r for r in res if slugmod.normalize(r.get("data", {}).get("url", "")) == slugmod.normalize(url)
            and r.get("name") == name and r.get("published") == "draft" and r.get("id")]
    return hits[0]["id"] if len(hits) == 1 else None


def record_unsaved(kind: str, source_id: str, target_id: str, error) -> None:
    """Last-resort breadcrumb when a Builder write succeeded but the bookkeeping did not."""
    try:
        path = Path(os.environ.get("RUNNER_UNSAVED_LOG") or ROOT / "logs" / "runner-unsaved.jsonl")
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as f:
            f.write(json.dumps({"timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "kind": kind, "source_id": source_id,
                                "target_id": target_id, "error": str(error)[:300]}) + "\n")
    except OSError:
        pass


def existing_urls(key: str, space: str) -> dict:
    """url -> {id, model} for every page-kind entry incl. unpublished drafts."""
    out = {}
    for model in PAGE_MODELS:
        offset = 0
        while True:
            q = urllib.parse.urlencode({"apiKey": space, "limit": 100, "offset": offset, "includeUnpublished": "true",
                                        "omit": "data.blocks,data.blocksString", "cachebust": "true"})
            d = http("GET", f"{CONTENT}/{model}?{q}", key)
            res = d.get("results", [])
            for r in res:
                u = r.get("data", {}).get("url")
                if u:
                    out[slugmod.normalize(u)] = {"id": r.get("id"), "model": model}
            if len(res) < 100:
                break
            offset += 100
    return out


# ---------------------------------------------------------------- main

def main() -> int:
    global HTTP_TIMEOUT
    argv = sys.argv[1:]
    arg = lambda n, d=None: argv[argv.index(n) + 1] if n in argv else d
    apply, limit, delay = "--apply" in argv, int(arg("--limit", 10)), float(arg("--delay", 0.5))
    HTTP_TIMEOUT = float(arg("--timeout", HTTP_TIMEOUT))
    preview = "--preview" in argv
    load_env()
    key = os.environ.get("BUILDER_CONTENT_MCP_PK")
    if not key:
        print("Missing BUILDER_CONTENT_MCP_PK in .env", file=sys.stderr)
        return 1
    if not preview and subprocess.run([sys.executable, str(ROOT / "scripts" / "mapping.py"), "check"], capture_output=True).returncode:
        print("Mapping is not approved (mapping.py check failed). Stopping.", file=sys.stderr)
        return 1
    mapping = json.loads((ROOT / "mapping" / "field-mapping.json").read_text())  # read AFTER the gate: what ran is what was approved
    check_configured(mapping)
    space, locale = mapping["target"]["space"], mapping["source"]["locale"]
    entries = {}
    exports = Path(os.environ.get("EXPORTS_DIR") or ROOT / "exports")  # EXPORTS_DIR: used by tests
    for line in (exports / "entries.jsonl").read_text().splitlines():
        if line.strip():
            e = json.loads(line)
            entries[e["sys"]["id"]] = e
    urls = existing_urls(key, space)
    by_id = {v["id"]: u for u, v in urls.items() if v.get("id")}  # indexed once: the lookups below are O(1)

    if preview:
        sid = arg("--preview")
        own = by_id.get(manifest("get", sid).get("target_id"))
        payload, exc, kids, st = build_page(entries[sid], entries, mapping, locale, urls.keys(), own)
        print(json.dumps({"payload": payload, "url_status": st, "children": kids, "exceptions": exc}, indent=2))
        return 0

    manifest("release-stale", "--minutes", "30")
    top_types = [c for c, m in mapping["content_types"].items() if not m.get("inline_into")]
    cts = [arg("--content-type")] if arg("--content-type") else top_types
    claimed: list[dict[str, Any]] = []
    for ct in cts:
        for status in ("pending", "failed"):
            room = limit - len(claimed)
            if room > 0:
                claimed += manifest("claim", "--status", status, "--limit", str(room), "--content-type", ct)["claimed"]
    print(f"{'APPLY' if apply else 'DRY RUN'}: claimed {len(claimed)} row(s) of types {cts}")
    done: set = set()
    counts = {"migrated": 0, "needs_human": 0, "failed": 0, "dry_run": 0, "unlogged_writes": 0}

    def setrow(sid: str, status: str, notes: str, target: str | None = None) -> bool:
        """Change a row we hold a claim on. Compare-and-set: if the claim was lost the row is left alone."""
        args = ["set", sid, "--status", status, "--notes", notes, "--expect-status", "in_progress"]
        if target:
            args += ["--target", target]
        try:
            manifest(*args)
            return True
        except RuntimeError as e:
            print(f"  WARNING {sid}: not updated ({e})")
            return False

    try:
        for row in claimed:
            sid = row["source_id"]
            entry = entries.get(sid)
            if entry is None:
                setrow(sid, "needs_human", "source entry missing from exports/")
                counts["needs_human"] += 1; done.add(sid); continue
            payload, exc, kids, ustat = build_page(entry, entries, mapping, locale, urls.keys(), by_id.get(row.get("target_id")))
            if exc:
                why = "; ".join(e.get("reason", "?") for e in exc)[:300]
                print(f"  FLAGGED {sid}: {why}")
                setrow(sid, "needs_human", f"runner: {why}")
                counts["needs_human"] += 1; done.add(sid); continue
            url = payload["data"].get("url")
            summary = f"{payload['name']!r} url={url} ({ustat}) blocks={len(payload['data'].get('blocks', []))}"
            if not apply:
                print(f"  would {'update' if row.get('target_id') else 'create'} {sid}: {summary}")
                counts["dry_run"] += 1
                continue
            model = mapping["content_types"][entry["sys"]["contentType"]["sys"]["id"]].get("target_model", "page")
            note_extra = ""
            try:
                if row.get("target_id"):
                    state = published_state(key, space, model, row["target_id"])
                    if state != "draft":
                        raise PublishedTarget(f"target {row['target_id']} is {state or 'missing'} in Builder, not a draft; refusing to edit it")
                    http("PATCH", f"{WRITE}/{model}/{row['target_id']}?triggerWebhooks=false", key, payload)
                    action, tid = "update", row["target_id"]
                else:
                    res = http("POST", f"{WRITE}/{model}?triggerWebhooks=false", key, {**payload, "published": "draft"})
                    tid = res.get("id") if isinstance(res, dict) else None
                    if not tid:
                        raise AmbiguousWriteError("Builder accepted the create but returned no id")
                    action = "create"
            except PublishedTarget as e:
                print(f"  REFUSED {sid}: {e}")
                setrow(sid, "needs_human", f"runner: {e}")
                counts["needs_human"] += 1; done.add(sid); continue
            except AmbiguousWriteError as e:
                tid = find_draft(key, space, model, payload["name"], url) if url else None
                if not tid:
                    msg = (f"runner: create outcome unknown ({str(e)[:160]}). Check Builder for a draft named {payload['name']!r} at {url}; "
                           "if it exists, set its id on this row (manifest set --target), otherwise requeue.")
                    print(f"  UNKNOWN {sid}: {e}")
                    setrow(sid, "needs_human", msg)
                    counts["needs_human"] += 1; done.add(sid); continue
                action, note_extra = "create", " (adopted after an unknown outcome)"
                print(f"  ADOPTED {sid}: found the draft {tid} after: {e}")
            except Exception as e:  # noqa: BLE001  any other failure of an update or a clearly-rejected create
                print(f"  FAILED  {sid}: {e}")
                setrow(sid, "failed", f"runner: {str(e)[:250]}")
                counts["failed"] += 1; done.add(sid); continue
            # The write happened. Save its id FIRST (state before audit); if that fails, stop the batch loudly.
            try:
                manifest("set", sid, "--status", "migrated", "--target", tid, "--notes", f"runner: {action}d draft{note_extra}",
                         "--expect-status", "in_progress")
            except RuntimeError as e:
                record_unsaved("set", sid, tid, e)
                print(f"  CRITICAL {sid}: Builder draft {tid} exists but the manifest could not be updated: {e}", file=sys.stderr)
                raise
            try:
                manifest("log-write", "--source-id", sid, "--target-id", tid, "--action", action)
            except RuntimeError as e:
                record_unsaved("log-write", sid, tid, e)
                counts["unlogged_writes"] += 1
                print(f"  WARNING {sid}: write succeeded but the audit line failed ({e}); see logs/runner-unsaved.jsonl")
            if url:  # later entries in this run must see this URL as taken
                urls[slugmod.normalize(url)] = {"id": tid, "model": model}
                by_id[tid] = slugmod.normalize(url)
            for kid in kids:  # inlined components ride along with their page
                try:
                    manifest("set", kid, "--status", "migrated", "--target", tid, "--notes", f"inlined into page {sid}", "--expect-status", "pending")
                except RuntimeError:
                    pass  # not pending (already handled) or not tracked: leave it
            print(f"  {action}d {sid} -> {tid}: {summary}")
            counts["migrated"] += 1; done.add(sid)
            time.sleep(delay)
    finally:  # never leave claims stuck in_progress
        for row in claimed:
            if row["source_id"] not in done:
                try:  # back to pending (not "failed": that would bump retry_count); only if the claim is still ours
                    manifest("set", row["source_id"], "--status", "pending", "--expect-status", "in_progress")
                except RuntimeError:
                    pass
    print(f"Summary: {counts}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
