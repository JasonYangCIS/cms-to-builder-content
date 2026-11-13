#!/usr/bin/env python3
"""Build the static HTML report site for the whole migration (all stages). Offline: reads local files only.

Inputs (whatever exists; missing stages are shown as "not run"): mapping/field-mapping.json, exports/ (entries,
content types, inventory, asset_map), the manifest (via manifest.py), logs/writes.jsonl and the latest reports/
(verification-*, reconcile-*, seo-redirect-*). It does not call Contentful or Builder, so it reflects the state of those
files at generation time (re-run the stage scripts first).

This module collects and normalizes that state (collect()) and provides shared HTML helpers; scripts/report_site.py
turns it into a paginated site with grouped attention items, CSV worklists and a small local filter/sort script.

Output: reports/site/ (git-ignored: it contains your content). One local script (assets/report.js) for filtering and
sorting; no external requests; all values HTML-escaped.

Usage: python3 scripts/build_report_html.py [--out-dir DIR] [--page-size 100] [--sample-per-type 5]
                                            [--exclude-prefix PREFIX ...] [--zip]
  --exclude-prefix hides manifest rows / pages / writes whose source id starts with PREFIX (e.g. test fixtures).
"""
from __future__ import annotations

import html
import json
import os
import re
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "transform"))
import mapping as mapmod  # noqa: E402
import reconcile as recon  # noqa: E402
import migrate_assets as ma  # noqa: E402
import run_migration as rm  # noqa: E402

STATUS_ORDER = ["verified", "migrated", "pending", "in_progress", "failed", "needs_human", "skipped"]


class Raw(str):
    """Pre-escaped HTML (everything else is escaped on output)."""


def esc(x) -> str:
    return html.escape("" if x is None else str(x), quote=True)


def short(x, n=160) -> str:
    s = "" if x is None else (x if isinstance(x, str) else json.dumps(x, ensure_ascii=False))
    return s if len(s) <= n else s[: n - 1] + "…"


def cell(x) -> str:
    return x if isinstance(x, Raw) else esc(x)


def table(headers, rows, cls="") -> Raw:
    if not rows:
        return Raw('<p class="muted">none</p>')
    head = "".join(f"<th>{esc(h)}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{cell(c)}</td>" for c in r) + "</tr>" for r in rows)
    return Raw(f'<div class="scroll"><table class="{esc(cls)}"><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>')


def badge(status) -> Raw:
    """shadcn-style Badge. Variants: success | secondary | outline | destructive | warning."""
    s = str(status or "")
    v = {"verified": "success", "same": "success", "pass": "success", "done": "success", "approved": "success",
         "migrated": "secondary", "in_progress": "secondary", "pending": "outline", "not-migrated": "outline", "skip": "outline", "skipped": "outline",
         "failed": "destructive", "needs_human": "destructive", "url-mismatch": "destructive", "target-missing": "destructive",
         "not approved": "destructive", "rewritten": "warning", "fuzzy": "warning", "URL changed": "warning"}.get(s, "outline")
    return Raw(f'<span class="badge {v}">{esc(s)}</span>')


def code(x) -> Raw:
    return Raw(f"<code>{esc(x)}</code>")


# ---------------------------------------------------------------------------------------------- exclusion filters

def filter_run(run: dict, skip) -> dict | None:
    """A verification-run report minus excluded entries. None if every row was excluded. Failure clusters carry no
    source ids, so a run that had excluded rows shows no clusters (with a note) rather than misleading ones."""
    rows = run.get("rows", [])
    kept = [r for r in rows if not skip(r.get("source_id"))]
    if len(kept) == len(rows):
        return run
    if not kept:
        return None
    out = dict(run, rows=kept, outcomes=dict(Counter(r.get("outcome") for r in kept)),
               fuzzy_items=[f for f in run.get("fuzzy_items", []) if not skip(f.get("source_id"))],
               failure_clusters=[], clusters_note="Failure clusters are hidden: this run included excluded entries.")
    sample = {ct: [s for s in v if not skip(s.get("source_id"))] for ct, v in run.get("sample_for_human_review", {}).items()}
    out["sample_for_human_review"] = {ct: v for ct, v in sample.items() if v}
    return out


def filter_reconcile(rec: dict | None, skip, coverage: dict) -> dict | None:
    """A reconcile report minus excluded entries; reference coverage is recomputed from the kept entries."""
    if not rec:
        return rec
    return dict(rec, reference_coverage=coverage,
                manifest_updates=[a for a in rec.get("manifest_updates", []) if not skip(a.get("source_id"))],
                orphan_rows=[o for o in rec.get("orphan_rows", []) if not skip(o.get("source_id"))],
                shared_components=[s for s in rec.get("shared_components", []) if not skip(s.get("component"))])


def filter_seo(seo: dict | None, skip) -> dict | None:
    if not seo:
        return seo
    return dict(seo, redirects=[r for r in seo.get("redirects", []) if not skip(r.get("source_id"))])


# ---------------------------------------------------------------------------------------------- collect

def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default


def read_jsonl(path, warnings: list | None = None) -> list:
    """One JSON object per line. Damaged lines (e.g. a half-written last line) are skipped and COUNTED into `warnings`,
    never silently turned into an empty file."""
    try:
        lines = Path(path).read_text().splitlines()
    except OSError:
        return []
    out, bad = [], 0
    for ln in lines:
        if not ln.strip():
            continue
        try:
            out.append(json.loads(ln))
        except ValueError:
            bad += 1
    if bad and warnings is not None:
        warnings.append(f"{Path(path).name}: {bad} damaged line(s) skipped; the numbers below may be incomplete")
    return out


def load_runs(reports: Path, skip) -> tuple[list, int]:
    """The verification-run reports on record. Dry-run previews are not runs (they decided nothing) and damaged files are
    ignored; runs that only covered excluded entries are counted as hidden."""
    runs, hidden = [], 0
    for p in sorted(reports.glob("verification-*.json")):
        d = read_json(p)
        if not isinstance(d, dict) or d.get("dry_run"):
            continue
        d = filter_run({"file": p.name, **d}, skip)
        if d is None:
            hidden += 1
        else:
            runs.append(d)
    return runs, hidden


def collect(reports_dir: Path | None = None, exclude: tuple = ()) -> dict:
    reports = reports_dir or Path(os.environ.get("REPORTS_DIR") or ROOT / "reports")
    mapping = read_json(ROOT / "mapping" / "field-mapping.json")
    if mapping is None:
        raise SystemExit("mapping/field-mapping.json not found: nothing to report. Copy mapping/field-mapping.example.json first.")
    locale = mapping["source"]["locale"]
    warnings: list = []
    entries = {}
    for e in read_jsonl(ROOT / "exports" / "entries.jsonl", warnings):
        entries[e["sys"]["id"]] = e
    try:
        rows = rm.manifest("list")["rows"]
        summary = rm.manifest("summary")
    except (RuntimeError, ValueError):
        rows, summary = [], {}
    skip = lambda sid: any(str(sid).startswith(p) for p in exclude)  # e.g. synthetic test fixtures
    n_rows_total = len(rows)
    rows = [r for r in rows if not skip(r["source_id"])]
    entries = {k: v for k, v in entries.items() if not skip(k)}
    rows_by_id = {r["source_id"]: r for r in rows}
    runs, hidden_runs = load_runs(reports, skip)

    def latest(prefix):
        files = sorted(reports.glob(f"{prefix}-*.json"))
        return {"file": files[-1].name, **(read_json(files[-1]) or {})} if files else None

    id_map = {k: v for k, v in (read_json(ROOT / "exports" / "id_map.json", {}) or {}).items() if not skip(k)}
    seo = filter_seo(latest("seo-redirect"), skip)
    reconcile = filter_reconcile(latest("reconcile"), skip, recon.reference_coverage(entries, mapping, locale, id_map))
    redirects = {r["source_id"]: r for r in (seo or {}).get("redirects", [])}
    try:
        state = mapmod.state(mapping)
    except Exception:  # noqa: BLE001
        state = {"approved": False}
    commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()

    top = {c for c, m in mapping["content_types"].items() if not m.get("inline_into")}
    pages = []
    for sid, e in sorted(entries.items()):
        ct = e["sys"]["contentType"]["sys"]["id"]
        if ct not in top:
            continue
        payload, exc, kids, _ = rm.build_page(e, entries, mapping, locale, [], None)
        ids = [rm.refs.link_id(x) for x in (rm.value_of(e, "componentsReference", locale) or [])]
        blocks = []
        for i, b in enumerate((payload or {}).get("data", {}).get("blocks", [])):
            child = entries.get(ids[i]) if i < len(ids) else None
            blocks.append({"component": b["component"]["name"], "options": b["component"]["options"],
                           "source_id": ids[i] if i < len(ids) else "", "content_type": child["sys"]["contentType"]["sys"]["id"] if child else ""})
        row = rows_by_id.get(sid, {})
        pages.append({"source_id": sid, "content_type": ct, "title": rm.value_of(e, "title", locale), "row": row,
                      "payload": payload, "exceptions": exc, "blocks": blocks, "redirect": redirects.get(sid),
                      "builder_id": row.get("target_id")})

    dropped = []
    for ct, m in mapping["content_types"].items():
        for spec in m.get("fields", []):
            if spec.get("type") == "dropped":
                n = sum(1 for e in entries.values() if e["sys"]["contentType"]["sys"]["id"] == ct
                        and rm.value_of(e, spec["source"], locale) not in (None, "", [], {}))
                dropped.append((ct, spec["source"], spec.get("reason", ""), n))
    asset_map = read_json(ROOT / "exports" / "asset_map.json", {})
    try:
        referenced = len(ma.referenced_asset_ids(list(entries.values())))
    except Exception:  # noqa: BLE001
        referenced = 0
    all_writes = read_jsonl(ROOT / "logs" / "writes.jsonl", warnings)
    excluded = {"prefixes": list(exclude), "rows": n_rows_total - len(rows), "writes": sum(1 for w in all_writes if skip(w.get("source_id"))),
                "runs_hidden": hidden_runs} if exclude else None
    return {"warnings": warnings, "excluded": excluded, "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"), "commit": commit, "mapping": mapping,
            "mapping_state": state, "inventory": read_json(ROOT / "exports" / "inventory.json"), "rows": rows, "summary": summary,
            "pages": pages, "dropped": dropped, "asset_map": asset_map, "assets_referenced": referenced,
            "writes": [w for w in all_writes if not skip(w.get("source_id"))], "verification_runs": runs, "reconcile": reconcile, "seo": seo,
            "entries_count": len(entries)}


# ---------------------------------------------------------------------------------------------- render

CSS = """
:root{--background:0 0% 100%;--foreground:240 10% 3.9%;--card:0 0% 100%;--muted:240 4.8% 95.9%;--muted-foreground:240 3.8% 46.1%;
--border:240 5.9% 90%;--primary:240 5.9% 10%;--primary-foreground:0 0% 98%;--secondary:240 4.8% 95.9%;--destructive:0 72% 51%;
--success:142 72% 29%;--warning:32 95% 36%;--info:221 83% 53%;--ring:240 5% 64.9%;--radius:.5rem}
@media (prefers-color-scheme:dark){:root{--background:240 10% 3.9%;--foreground:0 0% 98%;--card:240 10% 3.9%;--muted:240 3.7% 15.9%;
--muted-foreground:240 5% 64.9%;--border:240 3.7% 15.9%;--primary:0 0% 98%;--primary-foreground:240 5.9% 10%;--secondary:240 3.7% 15.9%;
--destructive:0 84% 60%;--success:142 70% 45%;--warning:38 92% 55%;--info:217 91% 65%;--ring:240 4.9% 83.9%}}
*{box-sizing:border-box;border-color:hsl(var(--border))}html{scroll-behavior:smooth}
body{margin:0;background:hsl(var(--background));color:hsl(var(--foreground));font:14px/1.6 ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
-webkit-font-smoothing:antialiased}
.wrap{max-width:900px;margin:0 auto;padding:40px 20px 64px}
h1{margin:0;font-size:26px;font-weight:700;letter-spacing:-.025em;line-height:1.2}h3{margin:0;font-size:15px;font-weight:600;letter-spacing:-.01em}
h4{margin:0 0 6px;font-size:14px;font-weight:600}p{margin:0}
.muted{color:hsl(var(--muted-foreground))}.sm{font-size:13px}.xs{font-size:12px}
code{font:12px ui-monospace,SFMono-Regular,Menlo,monospace;background:hsl(var(--muted));padding:1px 6px;border-radius:5px;word-break:normal;overflow-wrap:anywhere}
svg.i{width:16px;height:16px;flex:none;fill:none;stroke:currentColor;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
.head{display:flex;flex-direction:column;gap:6px;margin-bottom:22px}.meta{display:flex;flex-wrap:wrap;gap:4px 14px;font-size:13px;color:hsl(var(--muted-foreground))}
/* Alert */
.alert{display:grid;grid-template-columns:20px 1fr;gap:2px 12px;padding:14px 16px;border:1px solid hsl(var(--border));border-radius:calc(var(--radius) + 2px);
margin:10px 0;background:hsl(var(--card))}.alert>svg{margin-top:2px;grid-row:span 2}
.alert .t{font-weight:600;letter-spacing:-.01em}.alert .d{color:hsl(var(--muted-foreground));font-size:13px}
.alert.success{background:hsl(var(--success)/.08);border-color:hsl(var(--success)/.35)}.alert.success>svg{color:hsl(var(--success))}
.alert.warning{background:hsl(var(--warning)/.09);border-color:hsl(var(--warning)/.4)}.alert.warning>svg{color:hsl(var(--warning))}
.alert.destructive{background:hsl(var(--destructive)/.08);border-color:hsl(var(--destructive)/.4)}.alert.destructive>svg{color:hsl(var(--destructive))}
.alert.lg .t{font-size:17px}.alert.lg{padding:18px 20px}
/* Badge */
.badge{display:inline-flex;align-items:center;border-radius:6px;border:1px solid transparent;padding:1px 8px;font-size:12px;font-weight:600;line-height:1.5;white-space:nowrap}
.badge.default{background:hsl(var(--primary));color:hsl(var(--primary-foreground))}.badge.secondary{background:hsl(var(--secondary));color:hsl(var(--foreground))}
.badge.outline{border-color:hsl(var(--border));color:hsl(var(--muted-foreground))}.badge.destructive{background:hsl(var(--destructive));color:#fff}
.badge.success{background:hsl(var(--success)/.12);color:hsl(var(--success));border-color:hsl(var(--success)/.3)}
.badge.warning{background:hsl(var(--warning)/.13);color:hsl(var(--warning));border-color:hsl(var(--warning)/.35)}
/* Card */
.card{border:1px solid hsl(var(--border));border-radius:calc(var(--radius) + 4px);background:hsl(var(--card));box-shadow:0 1px 2px rgb(0 0 0/.05);margin:14px 0}
.card-h{padding:18px 20px 4px}.card-t{font-size:15px;font-weight:600;letter-spacing:-.01em}.card-d{font-size:13px;color:hsl(var(--muted-foreground))}
.card-c{padding:10px 20px 18px}
.ok{color:hsl(var(--success))}.off{color:hsl(var(--muted-foreground))}
/* Table */
.scroll{overflow-x:auto}table{width:100%;border-collapse:collapse;font-size:13.5px}
th{height:38px;padding:0 10px;text-align:left;font-weight:500;color:hsl(var(--muted-foreground));white-space:nowrap;border-bottom:1px solid hsl(var(--border))}
td{padding:9px 10px;border-bottom:1px solid hsl(var(--border));vertical-align:top}tr:last-child td{border-bottom:0}tbody tr:hover{background:hsl(var(--muted)/.5)}
/* Accordion */
.acc{border-bottom:1px solid hsl(var(--border))}.acc:last-child{border-bottom:0}
.acc>summary{display:flex;align-items:center;gap:14px;padding:15px 2px;cursor:pointer;list-style:none;font-weight:500}
.acc>summary::-webkit-details-marker{display:none}.acc>summary:hover .ttl{text-decoration:underline}
.acc>summary>svg.chev{margin-left:auto;color:hsl(var(--muted-foreground));transition:transform .15s}.acc[open]>summary>svg.chev{transform:rotate(180deg)}
.acc .body{padding:2px 2px 18px}article.page{border-bottom:1px solid hsl(var(--border))}article.page:last-child{border-bottom:0}article.page>.acc{border-bottom:0}
.row-main{display:flex;flex-direction:column;gap:3px;min-width:0;flex:1}.route{font-size:12.5px;color:hsl(var(--muted-foreground));display:flex;align-items:center;gap:6px;flex-wrap:wrap}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin-top:2px}.chip{border:1px solid hsl(var(--border));border-radius:6px;padding:0 8px;font-size:12px;color:hsl(var(--muted-foreground))}
.block{border:1px solid hsl(var(--border));border-radius:var(--radius);padding:12px 14px;margin:10px 0}
dl{display:grid;grid-template-columns:150px 1fr;gap:5px 16px;margin:8px 0 0;font-size:13.5px}dt{color:hsl(var(--muted-foreground))}dd{margin:0;word-break:break-word}
ul.plain{margin:0;padding-left:18px}.list{list-style:none;margin:0;padding:0}
.list li{display:flex;gap:12px;padding:11px 0;border-bottom:1px solid hsl(var(--border))}.list li:last-child{border-bottom:0}.list li>svg{margin-top:3px}
.list .t{font-weight:500}.list .d{color:hsl(var(--muted-foreground));font-size:13px}
.progress{display:flex;height:8px;border-radius:99px;overflow:hidden;background:hsl(var(--primary)/.12);margin:6px 0 14px}
.seg.success{background:hsl(var(--success))}.seg.secondary{background:hsl(var(--info))}.seg.outline{background:hsl(var(--muted-foreground)/.5)}.seg.destructive{background:hsl(var(--destructive))}.seg.warning{background:hsl(var(--warning))}
.note{display:flex;gap:8px;align-items:flex-start;font-size:13px;color:hsl(var(--muted-foreground));margin:2px 0 14px}.note svg{margin-top:2px}
.foot{margin-top:34px;padding-top:16px;border-top:1px solid hsl(var(--border));font-size:12.5px;color:hsl(var(--muted-foreground))}
@media (max-width:600px){dl{grid-template-columns:1fr}dt{margin-top:6px}.wrap{padding:24px 14px 48px}}
@media print{.nav,.toolbar,.pager{display:none}details>*{display:block}}
"""


def stage_rows(c: dict) -> list:
    rows_by = Counter(r["status"] for r in c["rows"])
    inv = c["inventory"]
    ver = c["verification_runs"]
    rec, seo = c["reconcile"], c["seo"]
    uncovered = len((rec or {}).get("reference_coverage", {}).get("uncovered", []))
    redirects = (seo or {}).get("redirects", [])
    return [
        ("1 Export", badge("done") if inv else badge("not run"),
         f"{inv['totals']['entries']} entries, {inv['totals']['assets']} assets, {inv['totals']['content_types']} content types, "
         f"locales {', '.join(inv['totals']['locales'])}" if inv else "run scripts/export_contentful.py --export"),
        ("2 Mapping", badge("approved") if c["mapping_state"].get("approved") else badge("not approved"),
         f"version {c['mapping'].get('version')}, {len(c['mapping']['content_types'])} content types mapped"),
        ("3 Assets", badge("done") if c["asset_map"] else badge("not run"),
         f"{len(c['asset_map'])} of {c['assets_referenced']} referenced asset(s) uploaded to Builder"),
        ("4 Migration", badge("done") if rows_by["migrated"] + rows_by["verified"] else badge("not run"),
         f"{rows_by['migrated'] + rows_by['verified']} of {len(c['rows']) - rows_by['skipped']} manifest rows migrated"
         + (f" ({rows_by['skipped']} skipped on purpose)" if rows_by["skipped"] else "") + "; "
         f"{sum(1 for w in c['writes'] if w.get('action') in ('create', 'update'))} Builder page write(s) logged"),
        ("5 Verification", badge("done") if (ver or rows_by["verified"]) else badge("not run"),
         f"{len(ver)} script run(s) on record; {rows_by['verified']} verified, {rows_by['failed']} failed, {rows_by['needs_human']} need a human"),
        ("6 Reconcile / references", badge("done") if rec else badge("not run"),
         (f"{len((rec or {}).get('manifest_updates', []))} manifest update(s), {uncovered} uncovered reference(s)") if rec else "run scripts/reconcile.py"),
        ("7 Redirects / SEO", badge("done") if seo else badge("not run"),
         f"{len(redirects)} page URL(s) checked, {sum(1 for r in redirects if r.get('url_changed'))} changed, {len((seo or {}).get('gaps', []))} SEO gap(s)" if seo else "run scripts/seo_redirect_check.py"),
    ]


ICONS = {
    "check": '<circle cx="12" cy="12" r="10"/><path d="m9 12 2 2 4-4"/>',
    "alert": '<path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3"/><path d="M12 9v4"/><path d="M12 17h.01"/>',
    "x": '<circle cx="12" cy="12" r="10"/><path d="m15 9-6 6"/><path d="m9 9 6 6"/>',
    "info": '<circle cx="12" cy="12" r="10"/><path d="M12 16v-4"/><path d="M12 8h.01"/>',
    "chev": '<path d="m6 9 6 6 6-6"/>',
    "arrow": '<path d="M5 12h14"/><path d="m12 5 7 7-7 7"/>',
    "minus": '<circle cx="12" cy="12" r="10"/><path d="M8 12h8"/>',
}


def icon(name: str, cls: str = "i") -> Raw:
    return Raw(f'<svg class="{cls}" viewBox="0 0 24 24" aria-hidden="true">{ICONS[name]}</svg>')


def sid(x, keep: int = 6) -> Raw:
    """Shortened id with the full value on hover."""
    s = "" if x is None else str(x)
    return Raw(f'<code title="{esc(s)}">{esc(s if len(s) <= 14 else s[:keep] + "…" + s[-4:])}</code>')


def variant(status) -> str:
    m = re.search(r'badge (\w+)"', str(badge(status)))
    return m.group(1) if m else "outline"


def fmt_value(v) -> Raw:
    if isinstance(v, list) and v and all(isinstance(x, dict) for x in v):
        items = "".join("<li>" + ", ".join(f"<b>{esc(k)}</b>: {esc(short(val, 160))}" for k, val in x.items()) + "</li>" for x in v)
        return Raw(f"<ul class='plain'>{items}</ul>")
    return Raw(esc(short(v, 400)))


def alert(kind: str, title: str, desc: str = "", ic: str | None = None, big: bool = False) -> str:
    ic = ic or {"success": "check", "warning": "alert", "destructive": "x"}.get(kind, "info")
    return (f"<div class='alert {kind}{' lg' if big else ''}'>{icon(ic)}<div class='t'>{esc(title)}</div>"
            + (f"<div class='d'>{esc(desc)}</div>" if desc else "<div></div>") + "</div>")


STATUS_MEANING = {"verified": "The Builder draft matches the Contentful source on every check.",
                  "migrated": "A draft exists in Builder but has not been verified yet.",
                  "pending": "Not migrated yet.", "in_progress": "Currently being processed.",
                  "failed": "A check failed. It is retried up to 3 times, then needs a human.",
                  "needs_human": "The pipeline stopped and will not guess (e.g. a URL already exists in Builder).",
                  "skipped": "Deliberately not migrated (a human decision). Not counted as work or as needing attention."}


def content_type_target(cm: dict | None) -> str:
    """Plain-language 'what this Contentful type becomes in Builder'."""
    if not cm:
        return "(unmapped)"
    parent = cm.get("inline_into")
    if cm.get("target_component"):
        return f"{cm['target_component']} block" + (f" on a {parent}" if parent else "")
    if parent and parent != "page":
        return f"item inside {parent}"
    return cm.get("target_model", "page")


def main() -> int:
    argv = sys.argv[1:]
    arg = lambda n, d=None: argv[argv.index(n) + 1] if n in argv else d
    reports = Path(os.environ.get("REPORTS_DIR") or ROOT / "reports")
    out_dir = Path(arg("--out-dir") or reports / "site")
    exclude = tuple(argv[i + 1] for i, a in enumerate(argv) if a == "--exclude-prefix" and i + 1 < len(argv))
    ctx = collect(exclude=exclude)
    import report_site
    s = report_site.build_site(ctx, out_dir, int(arg("--page-size", report_site.PAGE_SIZE)), int(arg("--sample-per-type", report_site.SAMPLE_PER_TYPE)), "--zip" in argv)
    print(f"Wrote {s['files']:,} files ({s['bytes'] / 1024 / 1024:.1f} MB) to {out_dir}: {s['pages']:,} page(s), {s['attention_entries']:,} entr"
          f"{'y' if s['attention_entries'] == 1 else 'ies'} needing attention in {s['groups']} group(s), {s['detail_pages']:,} detail page(s).")
    print(f"Open {out_dir / 'index.html'}" + (f"  (zip: {s['zip']})" if s["zip"] else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
