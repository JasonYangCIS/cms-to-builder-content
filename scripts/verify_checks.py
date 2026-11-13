#!/usr/bin/env python3
"""Deterministic source-vs-target checks for one migrated entry.

The verifier agent runs this first and only applies judgment to the `fuzzy`
items it returns. Output is always structured JSON on stdout.

Usage:
  verify_checks.py --source SRC.json --target TGT.json --mapping mapping/field-mapping.json \
                   --content-type page [--locale en-US] [--id-map IDMAP.json]

Inputs:
  SRC.json   Contentful entry as returned by the MCP (`fields.<name>.<locale>`) or already flat.
  TGT.json   Builder content document as returned by get_content (`data`, `name`, ...).
  mapping    mapping/field-mapping.json (see docs/phases.md for the format):
             content_types.<ct>.fields = [{"source": "title", "target": "data.title", "type": "text"}, ...]
             A field may set "constant": the target is compared to that value instead of the source.
  IDMAP.json {"<contentful source_id>": "<builder target_id>"} used by the `reference` check.

Field types: text | slug | asset_url | reference | richtext | number | boolean | json | handle_list | inline | dropped
Mapping extras: target paths and block_path may contain {index}/{item} placeholders filled by --var;
  a field may set `constant_map` ({source value: expected target}); a content type may set `target_name`,
  `target_name_by_entry` ({source id: name}) or `target_name_from` (source field used as the entry name).
Check statuses: pass | fail | fuzzy | skip
Exit code: 0 all pass/skip, 1 any fail, 2 no fail but fuzzy items remain.
"""
from typing import Callable
import argparse
import json
import sys
from pathlib import Path


# ---------- helpers ----------

def get_path(obj, path, default=None):
    cur = obj
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            return default
    return cur


def source_value(entry, field, locale):
    """Unwrap Contentful's per-locale value; accept already-flat entries. A locale map without the configured locale
    reads as no value (never as the whole map)."""
    sys.path.insert(0, str(Path(__file__).resolve().parent / "transform"))
    import localeval  # noqa: E402
    return localeval.unwrap(entry.get("fields", entry).get(field), locale)


def link_id(val):
    if isinstance(val, dict):
        return get_path(val, "sys.id") or val.get("id")
    return val


def result(name, field, status, **extra):
    return {"name": name, "field": field, "status": status, **extra}


# ---------- comparators: each returns a check dict ----------

def check_text(field, src, tgt):
    s, t = ("" if src is None else str(src)).strip(), ("" if tgt is None else str(tgt)).strip()
    if s == t:
        return result("field", field, "pass")
    return result("field", field, "fail", expected=s, actual=t, diff="text differs")


def check_scalar(field, src, tgt):
    return result("field", field, "pass") if src == tgt else result(
        "field", field, "fail", expected=src, actual=tgt, diff="value differs")


def check_slug(field, src, tgt):
    norm = lambda v: "/" + str(v or "").strip().strip("/")
    if norm(src) == norm(tgt):
        return result("slug", field, "pass")
    return result("slug", field, "fail", expected=norm(src), actual=norm(tgt),
                  diff="slug/url differs after normalizing leading/trailing slashes")


def check_asset_url(field, src, tgt, **_):
    """Pilot keeps Contentful asset URLs as-is. After the asset phase, the target
    should be a Builder CDN URL; flag those for judgment rather than failing."""
    def url(v):
        if isinstance(v, dict):
            v = get_path(v, "fields.file.url") or v.get("url")
        return ("https:" + v) if isinstance(v, str) and v.startswith("//") else v
    s, t = url(src), url(tgt)
    if s is None and t is None:
        return result("asset_url", field, "pass")
    if t is None:
        return result("asset_url", field, "fail", expected=s, actual=None, diff="target asset URL missing")
    if s == t:
        return result("asset_url", field, "pass", note="source URL preserved")
    if isinstance(s, dict) or (isinstance(src, dict) and src.get("sys", {}).get("linkType") == "Asset"):
        return result("asset_url", field, "fuzzy", expected=src, actual=t,
                      diff="source is an unresolved Asset link; confirm the target URL points at the same file")
    return result("asset_url", field, "fuzzy", expected=s, actual=t, diff="URL differs (re-hosted?)")


def check_reference(field, src, tgt, id_map=None, **_):
    id_map = id_map or {}
    src_ids = [link_id(x) for x in (src if isinstance(src, list) else [src] if src else [])]
    tgt_ids = [link_id(x) for x in (tgt if isinstance(tgt, list) else [tgt] if tgt else [])]
    missing = [s for s in src_ids if s not in id_map]
    if missing:
        # Reference resolution is a second pass; unresolved targets are not a failure yet.
        return result("reference", field, "skip", unresolved_sources=missing,
                      note="target not in id-map yet (second pass)")
    expected = [id_map[s] for s in src_ids]
    if expected == tgt_ids:
        return result("reference", field, "pass")
    return result("reference", field, "fail", expected=expected, actual=tgt_ids, diff="reference targets differ")


def check_richtext(field, src, tgt, **_):
    """Re-run the deterministic converter (scripts/transform/richtext.py) on the source document and
    compare to the target HTML. Anything the converter flags as an exception goes to judgment."""
    if src in (None, "", {}) and tgt in (None, "", {}, []):
        return result("richtext", field, "pass")
    if not (isinstance(src, dict) and src.get("nodeType") == "document"):
        return result("richtext", field, "fuzzy", diff="source is not a Contentful rich-text document; verifier should judge")
    sys.path.insert(0, str(Path(__file__).resolve().parent / "transform"))
    import richtext  # noqa: E402
    expected, exceptions = richtext.convert(src)
    if exceptions:
        return result("richtext", field, "fuzzy", expected=expected, actual=tgt, exceptions=exceptions,
                      diff="converter flagged exceptions; verifier should judge")
    if str(tgt or "").strip() == expected.strip():
        return result("richtext", field, "pass")
    return result("richtext", field, "fail", expected=expected, actual=tgt, diff="HTML differs from deterministic conversion")


def check_dropped(field, src, tgt, spec=None, **_):
    """Field deliberately not migrated (documented in the mapping). Never a failure,
    but surfaced as `skip` so the report shows what was dropped and why."""
    return result("dropped", field, "skip", reason=(spec or {}).get("reason", "no reason given"),
                  dropped_source_present=src not in (None, "", {}, []))


def check_inline(field, src, tgt, **_):
    """Parent field whose referenced entries are inlined; each is verified via its own content type."""
    return result("inline", field, "skip", note="referenced entries are verified via their own content type")


def check_component(block_path, expected, target):
    actual = get_path(target, block_path + ".component.name")
    if actual == expected:
        return result("component", block_path, "pass")
    return result("component", block_path, "fail", expected=expected, actual=actual,
                  diff="wrong or missing Builder component")


def check_handle_list(field, src, tgt, spec=None, **_):
    """Source: list of strings. Target: Builder list of {<item_key>: string} (e.g. ProductGridSelected.handles)."""
    key = (spec or {}).get("item_key", "value")
    s = [str(x).strip() for x in (src or [])]
    t = [str(x.get(key, "")).strip() if isinstance(x, dict) else str(x).strip() for x in (tgt or [])]
    if s == t:
        return result("handle_list", field, "pass")
    return result("handle_list", field, "fail", expected=s, actual=t, diff=f"list of {key} values differs")


def subst(text, variables):
    """Replace {name} placeholders (e.g. {index}, {item}) with --var values."""
    for k, v in variables.items():
        text = text.replace("{" + k + "}", str(v))
    return text


COMPARATORS: dict[str, Callable[..., dict]] = {
    "handle_list": check_handle_list,
    "dropped": check_dropped,
    "inline": check_inline,
    "text": lambda f, s, t, **_: check_text(f, s, t),
    "number": lambda f, s, t, **_: check_scalar(f, s, t),
    "boolean": lambda f, s, t, **_: check_scalar(f, s, t),
    "json": lambda f, s, t, **_: check_scalar(f, s, t),
    "slug": lambda f, s, t, **_: check_slug(f, s, t),
    "asset_url": check_asset_url,
    "reference": check_reference,
    "richtext": check_richtext,
}


def run(source, target, mapping, content_type, locale, id_map, variables=None):
    variables = variables or {}
    # Builder returns page blocks as a JSON string in data.blocksString; expose them as data.blocks
    tdata = target.get("data") if isinstance(target, dict) else None
    if isinstance(tdata, dict) and "blocks" not in tdata and isinstance(tdata.get("blocksString"), str):
        try:
            target = {**target, "data": {**tdata, "blocks": json.loads(tdata["blocksString"])}}
        except ValueError:
            pass
    ct = mapping.get("content_types", {}).get(content_type)
    if ct is None:
        return {"error": f"content type {content_type!r} not in mapping", "checks": [], "fuzzy": [], "summary": {}}
    checks = []
    # Builder entry name (top-level `name`, not data.*): per-entry override > fixed > from a source field
    source_id = get_path(source, "sys.id") or source.get("id")
    expected_name = (ct.get("target_name_by_entry", {}).get(source_id) or ct.get("target_name")
                     or (source_value(source, ct["target_name_from"], locale) if ct.get("target_name_from") else None))
    if expected_name:
        actual_name = target.get("name")
        checks.append(result("entry_name", "name", "pass") if actual_name == expected_name else
                      result("entry_name", "name", "fail", expected=expected_name, actual=actual_name,
                             diff="Builder entry name differs from the mapping's expected name"))
    block_path = subst(ct["block_path"], variables) if ct.get("block_path") else None
    if ct.get("target_component") and block_path:
        if "{" in block_path:
            checks.append(result("component", block_path, "fail", diff="unresolved placeholder; pass --var index=N"))
        else:
            checks.append(check_component(block_path, ct["target_component"], target))
    for spec in ct.get("fields", []):
        ftype = spec.get("type", "text")
        cmp_ = COMPARATORS.get(ftype)
        if cmp_ is None:
            checks.append(result("field", spec["source"], "fail", diff=f"unknown field type {ftype!r} in mapping"))
            continue
        raw = source_value(source, spec["source"], locale)
        # `constant` = the mapping overrides the source value; `constant_map` overrides only listed source values
        if "constant" in spec:
            src = spec["constant"]
        elif "constant_map" in spec and raw in spec["constant_map"]:
            src = spec["constant_map"][raw]
        else:
            src = raw
        tpath = subst(spec["target"], variables) if "target" in spec else None
        if tpath and "{" in tpath:
            checks.append(result("field", spec["source"], "fail", diff=f"unresolved placeholder in {tpath}; pass --var"))
            continue
        tgt = get_path(target, tpath) if tpath else None
        checks.append(cmp_(spec["source"], src, tgt, id_map=id_map, spec=spec))
    counts = {k: sum(1 for c in checks if c["status"] == k) for k in ("pass", "fail", "fuzzy", "skip")}
    return {
        "content_type": content_type,
        "checks": checks,
        "fuzzy": [c for c in checks if c["status"] == "fuzzy"],
        "summary": counts,
        "ok": counts["fail"] == 0 and counts["fuzzy"] == 0,
    }


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--source", required=True)
    p.add_argument("--target", required=True)
    p.add_argument("--mapping", default=str(Path(__file__).resolve().parent.parent / "mapping" / "field-mapping.json"))
    p.add_argument("--content-type", required=True)
    p.add_argument("--locale", default="en-US")
    p.add_argument("--id-map")
    p.add_argument("--var", action="append", default=[], metavar="NAME=VALUE",
                   help="fill {NAME} placeholders in mapping paths, e.g. --var index=2 --var item=0 "
                        "(index = position of the component in the page's componentsReference)")
    a = p.parse_args()

    load = lambda path: json.loads(Path(path).read_text())
    variables = dict(v.split("=", 1) for v in a.var)
    report = run(load(a.source), load(a.target), load(a.mapping), a.content_type, a.locale,
                 load(a.id_map) if a.id_map else {}, variables)
    print(json.dumps(report, indent=2, default=str))
    if "error" in report or report["summary"].get("fail"):
        sys.exit(1)
    sys.exit(2 if report["summary"].get("fuzzy") else 0)


if __name__ == "__main__":
    main()
