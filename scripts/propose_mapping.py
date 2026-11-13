#!/usr/bin/env python3
"""Propose a field mapping from exported Contentful content types (Stage 2 of docs/bulk-plan.md).

READ-ONLY with respect to mapping/field-mapping.json: it never edits the approved mapping (editing it
would reset approval). It writes a PROPOSAL to mapping/proposal.json and prints a review summary.
A human reviews the proposal and moves what they accept into field-mapping.json, then approves it.

Inputs (all local; run scripts/export_contentful.py first):
  exports/content_types.json        Contentful content types
  exports/builder_components.json   snapshot of Builder SDK components (name -> {input: type})
  mapping/field-mapping.json        current mapping (used to find unmapped types/fields)

Usage:
  python3 scripts/propose_mapping.py            # propose only what the current mapping lacks, list gaps
  python3 scripts/propose_mapping.py --fresh    # ignore the current mapping and propose everything
                                                # (sanity check of the heuristics against it)
"""
from __future__ import annotations

from typing import Any
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SYNONYMS = {  # Contentful field -> likely Builder input
    "header": "heading", "title": "heading", "preheader": "eyebrow", "buttonLabel": "ctaLabel",
    "buttonLink": "ctaHref", "image": "imageUrl", "backgroundImage": "imageUrl",
}


def tokens(name: str) -> set[str]:
    name = re.sub(r"^component", "", name)
    return {t.lower() for t in re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|\d+", name)}


def field_type(f: dict) -> str:
    t = f["type"]
    if t in ("Symbol", "Text"):
        return "text"
    if t in ("Integer", "Number"):
        return "number"
    if t == "Boolean":
        return "boolean"
    if t == "RichText":
        return "richtext"
    if t == "Link":
        return "asset_url" if f.get("linkType") == "Asset" else "reference"
    if t == "Array":
        it = f.get("items", {})
        if it.get("type") == "Link":
            return "inline" if it.get("linkType") == "Entry" else "asset_url"
        return "handle_list" if it.get("type") == "Symbol" else "json"
    return "json"  # Object, Date, Location


def flags(f: dict) -> list[str]:
    out = []
    if f.get("localized"):
        out.append("localized")
    if f.get("required"):
        out.append("required")
    for v in f.get("validations", []) + f.get("items", {}).get("validations", []):
        if "unique" in v:
            out.append("unique")
        if "in" in v:
            out.append(f"enum:{v['in']}")
        if "linkContentType" in v:
            out.append(f"links_to:{v['linkContentType']}")
    if f["type"] == "RichText":
        out.append("richtext (needs scripts/transform/richtext.py)")
    if field_type(f) == "asset_url":
        out.append("asset (needs the asset phase)")
    if f["type"] in ("Object", "Date", "Location"):
        out.append("no direct mapping: review")
    return out


def ranked_components(ct: dict, comps: dict) -> list[tuple[str, float]]:
    ids = {f["id"] for f in ct["fields"]}
    mapped = {SYNONYMS.get(i, i) for i in ids}
    ctoks = tokens(ct["sys"]["id"]) | tokens(ct.get("name", ""))
    out = []
    for name, inputs in comps.items():
        overlap = len(mapped & set(inputs)) / max(len(ids), 1)
        name_bonus = 0.4 if tokens(name) & ctoks else 0.0
        out.append((name, round(overlap + name_bonus, 2)))
    return sorted(out, key=lambda x: -x[1])


def classify_roles(types: list[dict], entries: list[dict]) -> dict[str, dict]:
    """Suggest a role per content type from the reference graph (a SUGGESTION: the human decides).

    page              has a slug and links to entries (or has a slug and nothing links to it)
    inline-component  referenced by at most one parent entry each, from a list field
    shared-component  most entries are referenced by 2+ parents -> candidate Builder section model
    data-record       referenced through single-value link fields (author, category) -> candidate Builder data model
    Symbols are never proposed: Builder publishes them on creation, which breaks the drafts-only rule.
    Without exported entries the graph is unknown, so only field shapes are used and confidence is lower.
    """
    by_type = {ct["sys"]["id"]: ct for ct in types}
    page_like = {tid for tid, ct in by_type.items() if any(f["id"] == "slug" for f in ct["fields"])}
    kind_of_entry = {e["sys"]["id"]: e["sys"]["contentType"]["sys"]["id"] for e in entries}
    parents: dict[str, set[str]] = {}     # entry id -> parent entry ids
    single_ref: dict[str, int] = {}       # type -> links to it through single-value fields
    list_ref: dict[str, int] = {}         # type -> links to it through list fields
    fields_by_type = {tid: {f["id"]: f for f in ct["fields"]} for tid, ct in by_type.items()}
    for e in entries:
        tid = e["sys"]["contentType"]["sys"]["id"]
        for fid, val in (e.get("fields") or {}).items():
            spec = fields_by_type.get(tid, {}).get(fid, {})
            for loc_val in (val.values() if isinstance(val, dict) else []):
                for link in (loc_val if isinstance(loc_val, list) else [loc_val]):
                    s = link.get("sys") if isinstance(link, dict) else None
                    if isinstance(s, dict) and s.get("type") == "Link" and s.get("linkType") == "Entry":
                        parents.setdefault(s["id"], set()).add(e["sys"]["id"])
                        target = kind_of_entry.get(s["id"])
                        if target:
                            bucket = list_ref if spec.get("type") == "Array" else single_ref
                            bucket[target] = bucket.get(target, 0) + 1
    out: dict[str, dict] = {}
    for tid in by_type:
        ids = [i for i, k in kind_of_entry.items() if k == tid]
        shared = sum(1 for i in ids if len(parents.get(i, ())) > 1)
        if not entries:  # no graph: use field shapes only
            ref_in = {t for t, c in by_type.items() for f in c["fields"]
                      for v in f.get("validations", []) + f.get("items", {}).get("validations", [])
                      if tid in v.get("linkContentType", [])}
            role, why = (("page", "has a slug") if tid in page_like else
                         ("data-record", "linked to by other types (no entries exported, graph unknown)") if ref_in else
                         ("inline-component", "default (no entries exported, graph unknown)"))
            out[tid] = {"role": role, "why": why, "confidence": "low"}
        elif tid in page_like and not parents.keys() & set(ids):
            out[tid] = {"role": "page", "why": "has a slug and no other entry links to it", "confidence": "medium"}
        elif ids and shared * 2 >= len(ids):
            out[tid] = {"role": "shared-component", "why": f"{shared} of {len(ids)} entries are referenced by 2+ parents",
                        "confidence": "medium"}
        elif single_ref.get(tid, 0) > list_ref.get(tid, 0):
            out[tid] = {"role": "data-record", "why": "referenced through single-value link fields", "confidence": "medium"}
        elif list_ref.get(tid, 0):
            out[tid] = {"role": "inline-component", "why": "referenced from list fields, each by one parent", "confidence": "medium"}
        else:
            out[tid] = {"role": "inline-component" if tid not in page_like else "page",
                        "why": "no entry links to it: unused or standalone", "confidence": "low"}
    return out


def not_yet_supported(role: str) -> str:
    """What the runner cannot do for a role today (so the human sees what would be dropped)."""
    return {"shared-component": "runner cannot write section models yet: shared entries would be inlined as copies",
            "data-record": "runner cannot write data models or reference fields yet: these entries and links are not migrated"}.get(role, "")


def propose_type(ct: dict, comps: dict, page_models: list[str]) -> dict:
    fields = ct["fields"]
    is_page = any(f["id"] == "slug" for f in fields) and any(
        f["type"] == "Array" and f.get("items", {}).get("linkType") == "Entry" for f in fields)
    if is_page:
        page_targets = {"title": "data.title", "slug": "data.url", "componentsReference": "data.blocks"}
        specs = []
        for f in fields:
            s = {"source": f["id"], "type": field_type(f), "flags": flags(f)}
            if f["id"] in page_targets:
                s["target"] = page_targets[f["id"]]
            specs.append(s)
        return {"role": "page", "target_model": "page", "confidence": "medium", "fields": specs}
    ranking = ranked_components(ct, comps)
    comp, score = ranking[0]
    close = [n for n, s in ranking[1:4] if score - s <= 0.15 and s > 0]
    entry: dict[str, Any] = {"role": "inline-component", "inline_into": "page", "target_component": comp,
             "block_path": "data.blocks.{index}",
             "confidence": "low" if close or score < 0.6 else "high" if score >= 1.0 else "medium",
             "match_score": score, "alternatives": [{"component": n, "score": s} for n, s in ranking[1:4] if s > 0],
             "ambiguous_with": close, "fields": []}
    inputs = comps.get(comp, {})
    for f in fields:
        ft, target_in = field_type(f), SYNONYMS.get(f["id"], f["id"])
        spec = {"source": f["id"], "type": ft, "flags": flags(f)}
        if ft == "asset_url" and target_in not in inputs:
            spec["type"] = "dropped"
            spec["reason"] = "no matching component input; revisit in the asset phase"
        elif target_in in inputs:
            spec["target"] = f"data.blocks.{{index}}.component.options.{target_in}"
        else:
            spec["type"] = "dropped"
            spec["reason"] = f"{comp} has no '{target_in}' input"
        entry["fields"].append(spec)
    return entry


def main() -> int:
    fresh = "--fresh" in sys.argv
    for need, how in (("content_types.json", "run scripts/export_contentful.py --export"),
                      ("builder_components.json", "save the Builder MCP get_registered_components output as "
                       '{"components": {"<Name>": {"<input>": "<type>"}}, "page_models": ["page"]}')):
        if not (ROOT / "exports" / need).exists():
            print(f"Missing exports/{need}: {how}", file=sys.stderr)
            return 1
    types = json.loads((ROOT / "exports" / "content_types.json").read_text())
    snap = json.loads((ROOT / "exports" / "builder_components.json").read_text())
    comps, page_models = snap["components"], snap.get("page_models", [])
    mapping = json.loads((ROOT / "mapping" / "field-mapping.json").read_text())
    current = {} if fresh else mapping.get("content_types", {})
    proposal: dict[str, Any] = {"_note": "PROPOSAL ONLY. Not used by the pipeline; a human moves accepted parts into field-mapping.json and approves.",
                "fresh": fresh, "content_types": {}, "gaps": [], "decisions_needed": []}
    ent_file = ROOT / "exports" / "entries.jsonl"
    entries = [json.loads(ln) for ln in ent_file.read_text().splitlines() if ln.strip()] if ent_file.exists() else []
    roles = classify_roles(types, entries)
    for ct in types:
        tid = ct["sys"]["id"]
        fields = {f["id"]: f for f in ct["fields"]}
        if tid in current:
            have = {s["source"] for s in current[tid].get("fields", [])}
            for missing in sorted(set(fields) - have):
                proposal["gaps"].append(f"{tid}.{missing}: field exists in Contentful but is not in the mapping")
            for stale in sorted(have - set(fields)):
                proposal["gaps"].append(f"{tid}.{stale}: mapped, but the field no longer exists in Contentful")
            continue
        proposal["content_types"][tid] = propose_type(ct, comps, page_models)
        suggested = roles[tid]
        proposal["content_types"][tid]["suggested_role"] = suggested
        gap = not_yet_supported(suggested["role"])
        if gap:
            proposal["content_types"][tid]["not_yet_supported"] = gap
            proposal["decisions_needed"].append(f"{tid}: looks like a {suggested['role']} ({suggested['why']}). {gap}.")
    all_flags = {f for p in proposal["content_types"].values() for s in p["fields"] for f in s["flags"]}
    loc_file = ROOT / "exports" / "locales.json"
    multi_locale = loc_file.exists() and len(json.loads(loc_file.read_text())) > 1  # single-locale spaces: nothing to decide
    if multi_locale and (any(f.startswith("localized") for f in all_flags) or any(
            f.get("localized") for ct in types for f in ct["fields"])):
        proposal["decisions_needed"].append(
            "Localized Contentful fields exist. Choose how Builder represents other locales: "
            "(a) inline localization per field, or (b) one Builder entry per locale (content-entry localization).")
    proposal["decisions_needed"].append(
        "Page URL policy: which source slugs collide with live Builder pages (needs a Builder-side check).")
    (ROOT / "mapping" / "proposal.json").write_text(json.dumps(proposal, indent=2) + "\n")

    print(f"{'FRESH' if fresh else 'INCREMENTAL'} proposal: {len(proposal['content_types'])} type(s) proposed, "
          f"{len(proposal['gaps'])} gap(s) in the current mapping")
    for tid, p in proposal["content_types"].items():
        tgt = p.get("target_component") or p["target_model"]
        print(f"  {tid:<32} role={p['suggested_role']['role']:<17} -> {tgt:<22} [{p['confidence']}]" + (f" score={p['match_score']}" if "match_score" in p else "")
              + (f"  AMBIGUOUS with {', '.join(p['ambiguous_with'])}: human must choose" if p.get("ambiguous_with") else ""))
        for s in p["fields"]:
            extra = (" -> " + s["target"].split("options.")[-1]) if "target" in s else (f"  ({s.get('reason', '')})" if s["type"] == "dropped" else "")
            print(f"      {s['source']:<20} {s['type']:<12}{extra}  {', '.join(s['flags'])}")
    for g in proposal["gaps"]:
        print("  GAP:", g)
    for d in proposal["decisions_needed"]:
        print("  DECISION:", d)
    print("Wrote mapping/proposal.json (field-mapping.json untouched).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
