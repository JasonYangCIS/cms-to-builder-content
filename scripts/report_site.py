#!/usr/bin/env python3
"""Static multi-page migration report site that scales to thousands of entries. Used by build_report_html.py.

Design for scale:
  * Needs-attention items are GROUPED by cause (category + content type + normalized reason), so thousands of identical
    problems become one card with a count, one decision, and a few samples.
  * Every group has a paginated list (100 rows/page) and a CSV worklist for spreadsheets / ticketing.
  * Pages are listed in paginated tables (problems first). Per-page detail files exist only for pages that need
    attention plus a deterministic sample of verified pages (like the verifier's sample), so file size stays bounded.
  * A small script (assets/report.js: plain DOM, no network, no eval/innerHTML) adds filter-as-you-type and column
    sorting on the rows of the table you are looking at. Everything is still readable without JavaScript.
  * All values are HTML-escaped; CSV cells that could be read as spreadsheet formulas are neutralized.
Output is a folder (open index.html) that can be zipped and shared.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import zipfile
from collections import Counter, defaultdict
from pathlib import Path

import build_report_html as R
from build_report_html import Raw, alert, badge, code, content_type_target, esc, fmt_value, icon, short, sid, stage_rows, table, variant, STATUS_MEANING, STATUS_ORDER

PAGE_SIZE = 100
SAMPLE_PER_TYPE = 5
LIST_REASON_CHARS = 200

# category -> (title, the decision/action for a human, alert severity)
CATALOG = {
    "url_exists": ("URL already exists in Builder", "Choose a URL policy for these pages (rename, give them a prefix, or skip them), update the mapping, then re-run.", "destructive"),
    "archived": ("Draft archived in Builder", "Restore the draft in Builder, or drop these entries from the migration.", "destructive"),
    "failed": ("Failed a check", "Review the failing check, fix the mapping or the source, then re-run migration and verification.", "destructive"),
    "needs_human": ("Stopped for a human decision", "Read the note on each entry, resolve the cause, then re-run.", "destructive"),
    "url_problem": ("Builder URL or draft doesn't match", "Investigate: the draft's URL differs from the mapping, or the draft is missing or archived.", "destructive"),
    "published": ("Published in Builder (not by this pipeline)", "A human published these. Confirm that was intended: the pipeline only creates drafts and will not verify or edit them.", "warning"),
    "uncovered": ("Links that would not carry over", "Map the field, or accept that these links are dropped.", "destructive"),
    "awaiting_verification": ("Migrated, awaiting verification", "Run scripts/run_verification.py (or the verifier agent).", "warning"),
    "exceptions": ("Can't be migrated automatically yet", "Resolve the listed exception (for example an embedded entry in rich text) or accept dropping it, then re-run.", "warning"),
    "url_changed": ("Page URL changed", "Decide: redirect the old URL, replace the existing page there, or leave it as is.", "warning"),
    "fuzzy": ("Needs a human judgment", "Review the flagged items and accept or fix them.", "warning"),
}


# ------------------------------------------------------------------------------------------------ grouping

def reason_signature(text) -> str:
    """Normalize a reason so that 'the same problem on different entries' gets the same signature."""
    s = (text or "").lower()
    s = re.sub(r"https?://\S+", "<url>", s)
    s = re.sub(r"(?<![\w<])/[\w\-./%]+", "<path>", s)
    s = re.sub(r"\b[0-9a-f]{12,}\b", "<id>", s)
    s = re.sub(r"[\w-]*\d[\w-]*", "<n>", s)
    return re.sub(r"\s+", " ", s).strip()[:140]


def classify_row(r: dict) -> str:
    n = (r.get("notes") or "").lower()
    if r["status"] == "failed":
        return "failed"
    if "already exists in builder" in n:
        return "url_exists"
    if "published in builder" in n and "archived" not in n:
        return "published"  # the verifier / runner found a target that is no longer a draft
    if "archived" in n and "builder" in n:
        return "archived"
    return "needs_human"


def build_groups(c: dict):
    """-> (groups, infos). A group = one decision for many entries."""
    pages = {p["source_id"]: p for p in c["pages"]}
    reds = {r["source_id"]: r for r in (c["seo"] or {}).get("redirects", [])}
    exc = {sid_: p for sid_, p in pages.items() if p["exceptions"]}
    items = []

    def base(source_id, ct=None, status=None):
        p, rd = pages.get(source_id), reds.get(source_id, {})
        row = (p or {}).get("row") or {}
        return {"source_id": source_id, "title": (p or {}).get("title") or source_id, "content_type": ct or (p or {}).get("content_type", ""),
                "status": status or row.get("status", ""), "builder_id": row.get("target_id") or "", "source_url": rd.get("source_url", ""),
                "builder_url": rd.get("builder_url") or (((p or {}).get("payload") or {}).get("data", {}).get("url") or "")}

    for r in c["rows"]:
        s, b = r["source_id"], base(r["source_id"], r["content_type"], r["status"])
        ex_text = "; ".join(e.get("reason", "?") for e in exc[s]["exceptions"]) if s in exc else ""
        if r["status"] in ("needs_human", "failed"):
            items.append({**b, "category": classify_row(r), "reason": " | ".join(x for x in ((r.get("notes") or ""), ex_text) if x)})
        elif r["status"] == "migrated":
            items.append({**b, "category": "awaiting_verification", "reason": r.get("notes") or ""})
        elif ex_text and r["status"] != "skipped":
            items.append({**b, "category": "exceptions", "reason": ex_text})
    for rd in (c["seo"] or {}).get("redirects", []):
        if rd["status"] in ("url-mismatch", "target-missing"):
            items.append({**base(rd["source_id"]), "category": "url_problem", "reason": rd.get("note", "")})
        elif rd.get("url_changed") and rd["status"] == "rewritten":
            items.append({**base(rd["source_id"]), "category": "url_changed",
                          "reason": f"{rd['source_url']} → {rd['builder_url']}. " + (rd.get("note") or "")})
    if c["verification_runs"]:
        for f in c["verification_runs"][-1].get("fuzzy_items", []):
            items.append({**base(f["source_id"]), "category": "fuzzy", "reason": f"{f.get('entry', '')}.{f.get('field', '')}: {f.get('diff', '')}"})
    if c["verification_runs"]:  # the verifier records each checked entry's published state
        already = {i["source_id"] for i in items if i["category"] == "published"}
        for r in c["verification_runs"][-1].get("rows", []):
            if r.get("published") == "published" and r["source_id"] not in already:
                items.append({**base(r["source_id"]), "category": "published", "reason": f"target {r.get('builder_id')} is published in Builder"})
    for u in (c["reconcile"] or {}).get("reference_coverage", {}).get("uncovered", []):
        items.append({**base(u["entry"]), "category": "uncovered", "reason": f"{u['field']} → {u['target']}: {u['why']}"})

    by = defaultdict(list)
    for it in items:
        by[(it["category"], it["content_type"], reason_signature(it["reason"]) if it["category"] in ("needs_human", "failed", "exceptions", "fuzzy", "uncovered", "archived") else "")].append(it)
    groups = []
    for (cat, ct, sig), its in by.items():
        title, decision, sev = CATALOG[cat]
        if cat in ("needs_human", "failed", "exceptions") and sig:
            nice = sig.replace("<url>", "URL").replace("<path>", "/…").replace("<id>", "…").replace("<n>", "…")
            title = f"{title}: {nice[:90]}"
        gid = f"{cat}-{re.sub(r'[^A-Za-z0-9]+', '', ct)[:24] or 'x'}-{hashlib.sha1(repr((cat, ct, sig)).encode()).hexdigest()[:6]}"
        its = sorted(its, key=lambda i: i["source_id"])
        groups.append({"id": gid, "category": cat, "title": title, "decision": decision, "severity": sev, "content_type": ct,
                       "count": len(its), "items": its, "example": its[0]["reason"]})
    groups.sort(key=lambda g: (0 if g["severity"] == "destructive" else 1, -g["count"], g["title"]))

    infos = []
    lost = [(f, n) for _ct, f, _r, n in c["dropped"] if n]
    if lost:
        infos.append(("Content that exists in Contentful but was deliberately not migrated",
                      "; ".join(f"{f} ({n:,} entr{'y' if n == 1 else 'ies'})" for f, n in lost)))
    skipped = [r for r in c["rows"] if r["status"] == "skipped"]
    if skipped:
        why = Counter((re.match(r"\[skipped: (.*?)\]", r.get("notes") or "") or [None, "no reason recorded"])[1] for r in skipped)
        infos.append((f"{len(skipped):,} entries are skipped on purpose",
                      "; ".join(f"{n:,} × {reason}" for reason, n in why.most_common(3)) + (" …" if len(why) > 3 else "")))
    pending = sum(1 for r in c["rows"] if r["status"] == "pending")
    if pending:
        infos.append((f"{pending:,} entries are pending", "Not migrated yet; they will be picked up by the next run."))
    infos += [(g, "") for g in (c["seo"] or {}).get("gaps", [])]
    return groups, infos


def select_detail(c: dict, attn_ids: set, per_type: int) -> set:
    """Pages that get their own detail file: every page needing attention + a deterministic sample of verified pages."""
    chosen = {p["source_id"] for p in c["pages"] if p["source_id"] in attn_ids}
    by_type = defaultdict(list)
    for p in c["pages"]:
        if p["source_id"] not in chosen and p["row"].get("status") == "verified":
            by_type[p["content_type"]].append((hashlib.sha256(p["source_id"].encode()).hexdigest(), p["source_id"]))
    for rows in by_type.values():
        chosen |= {s for _, s in sorted(rows)[:per_type]}
    return chosen


# ------------------------------------------------------------------------------------------------ small helpers

def slug(s) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", str(s))[:48].strip("_") or "x"


def detail_name(source_id) -> str:
    return f"{slug(source_id)}-{hashlib.sha1(str(source_id).encode()).hexdigest()[:6]}.html"


def safe_cell(v) -> str:
    """Neutralize spreadsheet formula injection (=, +, -, @ at the start of a cell)."""
    s = "" if v is None else str(v)
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r", "\n") else s


def csv_text(header, rows) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(header)
    for r in rows:
        w.writerow([safe_cell(x) for x in r])
    return buf.getvalue()


def pager(name_fn, i: int, n: int) -> str:
    if n <= 1:
        return ""
    nums = sorted({1, n} | set(range(max(1, i - 2), min(n, i + 2) + 1)))
    out, last = [], 0
    if i > 1:
        out.append(f"<a href='{name_fn(i - 1)}'>← Previous</a>")
    for k in nums:
        if k - last > 1:
            out.append("<span class='gap'>…</span>")
        out.append(f"<span class='cur'>{k}</span>" if k == i else f"<a href='{name_fn(k)}'>{k}</a>")
        last = k
    if i < n:
        out.append(f"<a href='{name_fn(i + 1)}'>Next →</a>")
    return "<nav class='pager' aria-label='Pagination'>" + "".join(out) + "</nav>"


def chunks(seq, size):
    return [seq[k:k + size] for k in range(0, len(seq), size)] or [[]]


# ------------------------------------------------------------------------------------------------ assets

SITE_CSS = """
a{color:hsl(var(--info))}a:hover{text-decoration:underline}
.nav{display:flex;gap:4px;margin:0 0 20px;padding:4px;border-radius:calc(var(--radius) + 2px);background:hsl(var(--muted));width:fit-content;max-width:100%;overflow-x:auto}
.nav a{display:inline-flex;align-items:center;gap:7px;padding:6px 14px;border-radius:var(--radius);color:hsl(var(--muted-foreground));font-weight:500;text-decoration:none;white-space:nowrap}
.nav a:hover{color:hsl(var(--foreground))}.nav a.on{background:hsl(var(--background));color:hsl(var(--foreground));box-shadow:0 1px 3px rgb(0 0 0/.12)}
.nav a .badge{padding:0 7px}.crumb{font-size:13px;margin-bottom:12px}h2{margin:0 0 6px;font-size:20px;font-weight:650;letter-spacing:-.02em}
.toolbar{display:flex;gap:12px;align-items:center;flex-wrap:wrap;margin:10px 0 4px}
input.filter{flex:1;min-width:200px;max-width:380px;padding:7px 11px;border:1px solid hsl(var(--border));border-radius:var(--radius);background:hsl(var(--background));color:inherit;font:inherit}
th.sortable{cursor:pointer;user-select:none}th[aria-sort=ascending]::after{content:" \\25B2";font-size:10px}th[aria-sort=descending]::after{content:" \\25BC";font-size:10px}
.pager{display:flex;gap:6px;align-items:center;flex-wrap:wrap;margin:16px 0;font-size:13px}
.pager a,.pager .cur{padding:3px 11px;border:1px solid hsl(var(--border));border-radius:var(--radius);text-decoration:none}.pager .cur{background:hsl(var(--primary));color:hsl(var(--primary-foreground));border-color:transparent}
.group{border:1px solid hsl(var(--border));border-radius:calc(var(--radius) + 4px);padding:16px 18px;margin:12px 0;background:hsl(var(--card))}
.group h3{display:flex;gap:10px;align-items:flex-start}.group h3 .n{margin-left:auto;font-weight:700;font-size:20px;line-height:1.2}
.group .decision{margin:6px 0 8px}.group .samples{font-size:13px;color:hsl(var(--muted-foreground))}.group .actions{display:flex;gap:16px;font-size:13px;margin-top:10px}
.cell-reason{max-width:340px;color:hsl(var(--muted-foreground))}
td:first-child code{white-space:nowrap}
"""

JS = """/* Filter and sort for tables on the current page. Plain DOM only: no network access, no dynamic code, no HTML injection. */
(function () {
  "use strict";
  function cellKey(row, idx) {
    var c = row.cells[idx];
    return ((c && (c.getAttribute("data-sort") || c.textContent)) || "").trim();
  }
  Array.prototype.forEach.call(document.querySelectorAll("table.data"), function (table) {
    var tbody = table.tBodies[0];
    if (!tbody || tbody.rows.length < 2) return;
    var wrap = table.closest(".scroll") || table;
    var bar = document.createElement("div");
    bar.className = "toolbar";
    var box = document.createElement("input");
    box.type = "search";
    box.className = "filter";
    box.placeholder = "Filter these " + tbody.rows.length + " rows (press / to focus)";
    box.setAttribute("aria-label", "Filter rows");
    var count = document.createElement("span");
    count.className = "muted xs";
    bar.appendChild(box);
    bar.appendChild(count);
    wrap.parentNode.insertBefore(bar, wrap);
    var rows = Array.prototype.slice.call(tbody.rows);
    box.addEventListener("input", function () {
      var q = box.value.trim().toLowerCase(), shown = 0;
      rows.forEach(function (r) {
        var ok = !q || r.textContent.toLowerCase().indexOf(q) !== -1;
        r.hidden = !ok;
        if (ok) shown++;
      });
      count.textContent = q ? shown + " of " + rows.length + " shown" : "";
    });
    var heads = table.tHead ? table.tHead.rows[0].cells : [];
    Array.prototype.forEach.call(heads, function (th, idx) {
      th.classList.add("sortable");
      th.tabIndex = 0;
      function sortBy() {
        var dir = th.getAttribute("aria-sort") === "ascending" ? "descending" : "ascending";
        Array.prototype.forEach.call(heads, function (h) { h.removeAttribute("aria-sort"); });
        th.setAttribute("aria-sort", dir);
        var f = dir === "ascending" ? 1 : -1;
        rows.sort(function (a, b) {
          var x = cellKey(a, idx), y = cellKey(b, idx), nx = parseFloat(x), ny = parseFloat(y);
          if (!isNaN(nx) && !isNaN(ny) && String(nx) === x && String(ny) === y) return (nx - ny) * f;
          return x.localeCompare(y, undefined, { numeric: true }) * f;
        });
        rows.forEach(function (r) { tbody.appendChild(r); });
      }
      th.addEventListener("click", sortBy);
      th.addEventListener("keydown", function (e) {
        if (e.key === "Enter" || e.key === " ") { e.preventDefault(); sortBy(); }
      });
    });
  });
  document.addEventListener("keydown", function (e) {
    if (e.key === "/" && !/^(input|textarea|select)$/i.test(document.activeElement.tagName)) {
      var f = document.querySelector("input.filter");
      if (f) { e.preventDefault(); f.focus(); }
    }
  });
})();
"""


# ------------------------------------------------------------------------------------------------ the site

class Site:
    def __init__(self, c: dict, page_size: int = PAGE_SIZE, sample_per_type: int = SAMPLE_PER_TYPE):
        self.c, self.page_size = c, page_size
        self.groups, self.infos = build_groups(c)
        self.attn_ids = {i["source_id"] for g in self.groups for i in g["items"]}
        self.page_by_id = {p["source_id"]: p for p in c["pages"]}
        self.detail_ids = select_detail(c, self.attn_ids, sample_per_type)
        self.issues = defaultdict(list)
        for g in self.groups:
            for i in g["items"]:
                self.issues[i["source_id"]].append((g, i))
        self.counts = Counter(r["status"] for r in c["rows"])
        self.tone = "success" if not self.groups else ("destructive" if any(g["severity"] == "destructive" for g in self.groups) else "warning")
        self.files: dict[str, str] = {}

    # -- links
    def detail_href(self, source_id, depth=0):
        return ("../" * depth) + f"page/{detail_name(source_id)}" if source_id in self.detail_ids and source_id in self.page_by_id else None

    # -- shared layout
    def layout(self, title, body, depth=0, active="", full_header=False, crumb=""):
        pre, c, m = "../" * depth, self.c, self.c["mapping"]
        att = f" <span class='badge {'destructive' if self.tone == 'destructive' else 'warning'}'>{len(self.attn_ids):,}</span>" if self.attn_ids else ""
        on = lambda key: " class='on'" if key == active else ""
        nav = "".join(f"<a href='{pre}{href}'{on(key)}>{label}{extra}</a>" for href, label, key, extra in (
            ("index.html", "Overview", "overview", ""), ("attention.html", "Needs attention", "attention", att),
            ("pages-1.html", "Pages", "pages", f" <span class='badge outline'>{len(c['pages']):,}</span>"),
            ("progress.html", "Progress", "progress", ""), ("details.html", "Details", "details", "")))
        meta = (f"<div class='meta'><span>Generated {esc(c['generated_at'])}</span>{'<span>repo ' + esc(c['commit']) + '</span>' if c['commit'] else ''}"
                f"<span>{sid(m['source']['space'], 4)} / {esc(m['source']['environment'])} → {sid(m['target']['space'], 4)}</span></div>")
        ex = c.get("excluded")
        note = ""
        if ex:
            note = (f"<div class='note'>{icon('info')}<div><b>Filtered view.</b> Hidden everywhere in this report: entries whose source id starts with "
                    + ", ".join(f"<code>{esc(p)}</code>" for p in ex["prefixes"]) + f" ({ex['rows']} manifest row(s), {ex['writes']} logged write(s), "
                    f"{ex['runs_hidden']} verification run(s) that covered only hidden entries). Counts, tables and reference coverage all exclude them.</div></div>")
        if full_header and c.get("warnings"):
            note += alert("warning", "Some input files were damaged", "; ".join(c["warnings"]))
        h1 = "Contentful → Builder.io migration"
        return (f"<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
                f"<title>{esc(title)} · Migration report</title><link rel='stylesheet' href='{pre}assets/report.css'>"
                f"<script src='{pre}assets/report.js' defer></script></head><body><div class='wrap'>"
                f"<div class='head'><h1>{h1 if full_header else esc(title)}</h1>{meta if full_header else ''}</div>"
                f"{note if full_header else ''}<nav class='nav' aria-label='Report sections'>{nav}</nav>{crumb}{body}"
                "<div class='foot'>Generated from local files only (no live Contentful/Builder calls): it reflects the manifest, exports and stage reports at "
                "generation time. Static site: one small script for filtering and sorting, no external requests. Rebuild with "
                "<code>python3 scripts/build_report_html.py</code>.</div></div></body></html>")

    def verdict(self):
        pages = [p for p in self.c["pages"] if p["row"].get("status") != "skipped"]  # skipped pages are deliberately out of scope
        skipped_n = len(self.c["pages"]) - len(pages)
        done = sum(1 for p in pages if p["row"].get("status") == "verified")
        n, g = len(self.attn_ids), len(self.groups)
        tail = f" {skipped_n:,} skipped on purpose." if skipped_n else ""
        if not pages:
            return "destructive" if g else "success", "No pages to migrate." + tail if skipped_n else "No pages found in the export yet."
        if not g:
            return "success", f"All {len(pages):,} page{'s are' if len(pages) != 1 else ' is'} migrated as drafts and verified.{tail} Drafts only: this pipeline never publishes."
        return self.tone, (f"{done:,} of {len(pages):,} page{'s' if len(pages) != 1 else ''} verified. {n:,} entr{'y needs' if n == 1 else 'ies need'} "
                           f"attention in {g} group{'s' if g != 1 else ''}.{tail} Drafts only: this pipeline never publishes.")

    # -- fragments
    def stages(self):
        out = []
        for n, s, d in stage_rows(self.c):
            ok = re.sub("<[^>]+>", "", str(s)) in ("done", "approved")
            out.append(f"<li class='{'ok' if ok else 'off'}'>{icon('check' if ok else 'minus')}<div><div class='t' style='color:hsl(var(--foreground))'>{esc(n)}</div>"
                       f"<div class='d'>{esc(d)}</div></div><span style='margin-left:auto'>{s}</span></li>")
        return "<ul class='list' id='stages'>" + "".join(out) + "</ul>"

    def status_bar(self):
        total = max(len(self.c["rows"]), 1)
        bar = "".join(f"<span class='seg {variant(s)}' style='width:{100 * self.counts[s] / total:.1f}%' title='{esc(s)}: {self.counts[s]:,}'></span>"
                      for s in STATUS_ORDER if self.counts.get(s))
        legend = " ".join(f"{badge(s)} {self.counts[s]:,}" for s in STATUS_ORDER if self.counts.get(s))
        return f"<div class='progress'>{bar}</div><p class='sm muted'>{legend}</p>"

    @staticmethod
    def card(title, desc, body):
        return (f"<div class='card'><div class='card-h'><div class='card-t'>{esc(title)}</div>" + (f"<div class='card-d'>{esc(desc)}</div>" if desc else "")
                + f"</div><div class='card-c'>{body}</div></div>")

    # -- pages
    def index(self):
        kind, text = self.verdict()
        n_rows = len(self.c["rows"])
        body = [alert(kind, text, f"{n_rows:,} entries tracked · drafts only: this pipeline never publishes", big=True)]
        if self.groups:
            top = "".join(f"<li>{icon('x' if g['severity'] == 'destructive' else 'alert')}<div><a class='t' href='attention.html#{esc(g['id'])}'>{esc(g['title'])}</a>"
                          f"<div class='d'>{esc(g['decision'])}</div></div><span class='badge {g['severity']}' style='margin-left:auto'>{g['count']:,}</span></li>"
                          for g in self.groups[:5])
            more = f"<p class='sm' style='margin-top:10px'><a href='attention.html'>See all {len(self.groups)} groups →</a></p>" if len(self.groups) > 5 else \
                "<p class='sm' style='margin-top:10px'><a href='attention.html'>Open the worklists →</a></p>"
            body.append(self.card("Biggest issues", "Grouped by cause; each group is one decision.", f"<ul class='list'>{top}</ul>{more}"))
        body.append(self.card("Entries by status", f"{n_rows:,} entries tracked.", self.status_bar()))
        body.append(self.card("Pipeline", "Each stage of the migration.", self.stages()))
        return self.layout("Overview", "".join(body), 0, "overview", full_header=True)

    def attention(self):
        body = []
        if not self.groups:
            body.append(alert("success", "Nothing needs your action", "Every entry is migrated and verified, with no open decisions."))
        else:
            body.append(f"<p class='muted sm'>{len(self.attn_ids):,} distinct entr{'y' if len(self.attn_ids) == 1 else 'ies'} in {len(self.groups)} group(s). "
                        "Each group is a single decision; open its list or download the CSV worklist.</p>")
            for g in self.groups:
                sample = ", ".join(f"<code>{esc(i['source_id'])}</code>" for i in g["items"][:3]) + (" …" if g["count"] > 3 else "")
                lst = f"attention/{g['id']}-1.html"
                body.append(f"<div class='group' id='{esc(g['id'])}'><h3><span class='badge {g['severity']}'>{esc(g['category'].replace('_', ' '))}</span>"
                            f"<span>{esc(g['title'])}</span><span class='n'>{g['count']:,}</span></h3>"
                            f"<div class='decision'><b>Decision:</b> {esc(g['decision'])}</div>"
                            + (f"<div class='samples'>Example: {esc(short(g['example'], 220))}</div>" if g["example"] else "")
                            + f"<div class='samples'>{('Type: <code>' + esc(g['content_type']) + '</code> · ') if g['content_type'] else ''}Samples: {sample}</div>"
                            f"<div class='actions'><a href='{lst}'>View all {g['count']:,} →</a><a href='worklists/{esc(g['id'])}.csv'>Download CSV</a></div></div>")
            body.append(f"<p class='sm'><a href='worklists/all-attention.csv'>Download every attention item as CSV</a> ({sum(g['count'] for g in self.groups):,} rows)</p>")
        if self.infos:
            body.append(self.card("Worth knowing", "Information only; no action required.", "<ul class='list'>" + "".join(
                f"<li class='off'>{icon('info')}<div><div class='t' style='color:hsl(var(--foreground))'>{esc(t)}</div>" + (f"<div class='d'>{esc(d)}</div>" if d else "") + "</div></li>"
                for t, d in self.infos) + "</ul>"))
        return self.layout("Needs attention", "".join(body), 0, "attention")

    def entry_cell(self, source_id, depth):
        href = self.detail_href(source_id, depth)
        return Raw(f"<a href='{esc(href)}'><code>{esc(source_id)}</code></a>") if href else code(source_id)

    def group_pages(self, g):
        chunk_list = chunks(g["items"], self.page_size)
        out = {}
        for n, ch in enumerate(chunk_list, 1):
            rows = [(self.entry_cell(i["source_id"], 1), i["title"], i["content_type"], badge(i["status"]), Raw(f"<span class='cell-reason'>{esc(short(i['reason'], LIST_REASON_CHARS))}</span>"),
                     code(i["builder_url"]) if i["builder_url"] else "") for i in ch]
            first = (n - 1) * self.page_size + 1
            body = (f"<h2>{esc(g['title'])}</h2><p class='decision'><b>Decision:</b> {esc(g['decision'])}</p>"
                    f"<p class='muted sm'>{g['count']:,} entries · showing {first:,}–{first + len(ch) - 1:,} · <a href='../worklists/{esc(g['id'])}.csv'>Download CSV worklist</a></p>"
                    + str(table(["Entry", "Title", "Type", "Status", "Reason", "Builder URL"], rows, "data"))
                    + pager(lambda k: f"{g['id']}-{k}.html", n, len(chunk_list)))
            crumb = "<div class='crumb'><a href='../attention.html'>← Needs attention</a></div>"
            out[f"attention/{g['id']}-{n}.html"] = self.layout(g["title"], body, 1, "attention", crumb=crumb)
        return out

    def sorted_pages(self):
        def key(p):
            st = p["row"].get("status")
            tier = 0 if p["source_id"] in self.attn_ids else (3 if st == "skipped" else (1 if st != "verified" else 2))
            return (tier, p["source_id"])
        return sorted(self.c["pages"], key=key)

    def pages_lists(self):
        pages = self.sorted_pages()
        chunk_list = chunks(pages, self.page_size)
        out = {}
        for n, ch in enumerate(chunk_list, 1):
            rows = []
            for p in ch:
                r, url = p["redirect"] or {}, (p["payload"] or {}).get("data", {}).get("url")
                comps = [b["component"] for b in p["blocks"]]
                href = self.detail_href(p["source_id"])
                title = Raw(f"<a href='{esc(href)}'>{esc(p['title'] or p['source_id'])}</a>") if href else (p["title"] or p["source_id"])
                route = Raw(f"<code>{esc(r.get('source_url') or '?')}</code> → <code>{esc(url or '?')}</code>" + (str(badge('URL changed')) if r.get("url_changed") else ""))
                rows.append((title, route, ", ".join(comps[:4]) + (f" +{len(comps) - 4}" if len(comps) > 4 else ""), badge(p["row"].get("status", "not seeded")),
                             Raw(("<span class='badge destructive'>needs attention</span>" if p["source_id"] in self.attn_ids else ""))))
            first = (n - 1) * self.page_size + 1
            first_id = ch[0]["source_id"] if ch else ""
            body = (f"<h2>Pages</h2><p class='muted sm'>{len(pages):,} pages, problems first · showing {first:,}–{first + len(ch) - 1:,}"
                    + (f" (from <code>{esc(first_id)}</code>)" if ch else "") + "</p>"
                    + str(table(["Title", "URL (Contentful → Builder)", "Components", "Status", "Flag"], rows, "data"))
                    + pager(lambda k: f"pages-{k}.html", n, len(chunk_list))
                    + f"<p class='xs muted'>Detail pages exist for pages needing attention and a sample of verified pages ({len(self.detail_ids):,} total).</p>")
            out[f"pages-{n}.html"] = self.layout("Pages", body, 0, "pages")
        return out

    def detail_page(self, p):
        row, r, m = p["row"], p["redirect"] or {}, self.c["mapping"]
        url = (p["payload"] or {}).get("data", {}).get("url")
        parts = [f"<div class='phead' style='display:flex;gap:10px;align-items:center;flex-wrap:wrap'><h2 style='margin:0'>{esc(p['title'] or p['source_id'])}</h2>{badge(row.get('status', 'not seeded'))}</div>",
                 f"<div class='route' style='margin:6px 0 14px'><code>{esc(r.get('source_url') or '?')}</code>{icon('arrow')}<code>{esc(url or '?')}</code>"
                 + (str(badge("URL changed")) if r.get("url_changed") else "") + "</div>"]
        for g, i in self.issues.get(p["source_id"], []):
            parts.append(alert({"destructive": "destructive", "warning": "warning"}[g["severity"]], g["title"], short(i["reason"], 300) or g["decision"]))
        if p["exceptions"]:
            parts.append(alert("warning", "Would need a human before it can migrate", "; ".join(e.get("reason", "?") for e in p["exceptions"])))
        for k, b in enumerate(p["blocks"]):
            parts.append(f"<div class='block'><h4>{k + 1}. {esc(b['component'])} <span class='muted' style='font-weight:400'>from {esc(b['content_type'])}</span></h4><dl>"
                         + "".join(f"<dt>{esc(a)}</dt><dd>{fmt_value(v)}</dd>" for a, v in b["options"].items()) + "</dl>")
            drop = [s for s in m["content_types"].get(b["content_type"], {}).get("fields", []) if s.get("type") == "dropped"]
            if drop:
                parts.append("<p class='xs muted' style='margin-top:8px'>Not migrated: " + "; ".join(f"{esc(s['source'])} ({esc(short(s.get('reason'), 90))})" for s in drop) + "</p>")
            parts.append("</div>")
        meta = [("Contentful entry", code(p["source_id"])), ("Content type", code(p["content_type"])),
                ("Builder entry", code(p["builder_id"]) if p["builder_id"] else "not created"), ("Builder name", (p["payload"] or {}).get("name") or "-"),
                ("Manifest notes", short(row.get("notes"), 400))]
        if r:
            meta.append(("Redirect check", Raw(f"{badge(r['status'])} {esc(r.get('note', ''))}")))
        parts.append("<details class='acc'><summary class='sm muted'>IDs and technical details" + str(icon("chev", "i chev")) + "</summary><div class='body'>"
                     + str(table(["", ""], meta)) + "</div></details>")
        crumb = "<div class='crumb'><a href='../pages-1.html'>← Pages</a></div>"
        return self.layout(p["title"] or p["source_id"], "".join(parts), 1, "pages", crumb=crumb)

    def progress(self):
        rows = self.c["rows"]
        by_ct: defaultdict[str, Counter[str]] = defaultdict(Counter)
        for r in rows:
            by_ct[r["content_type"]][r["status"]] += 1
        m = self.c["mapping"]
        types = table(["Content type", "Becomes", "Entries", "Status"], [(code(ct), content_type_target(m["content_types"].get(ct)), f"{sum(cnt.values()):,}",
                                                                          Raw(" ".join(f"{badge(s)} {n:,}" for s, n in cnt.items()))) for ct, cnt in sorted(by_ct.items())])
        status = table(["Status", "Entries", "Meaning"], [(badge(s), f"{self.counts[s]:,}", STATUS_MEANING[s]) for s in STATUS_ORDER if self.counts.get(s)])
        body = (self.card("Pipeline", "Each stage of the migration.", self.stages())
                + self.card("Entries by status", f"{len(rows):,} entries tracked.", self.status_bar() + str(status))
                + self.card("Content types", "How each Contentful type is migrated.", types))
        return self.layout("Progress", body, 0, "progress")

    def details(self):
        c, counts = self.c, self.counts

        def acc(anchor, title, summary, body):
            return (f"<details class='acc' id='{anchor}'><summary><span class='ttl'>{esc(title)}</span><span class='muted sm' style='font-weight:400'>{esc(summary)}</span>"
                    f"{icon('chev', 'i chev')}</summary><div class='body'>{body}</div></details>")
        ver = c["verification_runs"]
        v = [f"<p>Manifest: {counts.get('verified', 0):,} verified, {counts.get('failed', 0):,} failed, {counts.get('needs_human', 0):,} need a human, "
             f"{counts.get('migrated', 0):,} migrated and not yet verified. Entries may have been verified by the verifier agent or by "
             "<code>scripts/run_verification.py</code>; only the script writes a run report.</p>"]
        if not ver:
            v.append("<p class='muted'>No <code>run_verification.py</code> run reports on record (after any filtering).</p>")
        v.append(str(table(["Run", "Outcomes", "Failure clusters", "Fuzzy items"], [(r["file"], Raw(" ".join(f"{badge(k)} {n}" for k, n in r.get("outcomes", {}).items())) or "-",
                                                                                      len(r.get("failure_clusters", [])), len(r.get("fuzzy_items", []))) for r in ver])))
        if ver:
            last = ver[-1]
            if last.get("clusters_note"):
                v.append(f"<p class='muted'>{esc(last['clusters_note'])}</p>")
            if last.get("failure_clusters"):
                v.append("<h4>Failure clusters (latest run)</h4>" + str(table(["Content type", "Check", "Field", "Diff", "Count"],
                         [(f["content_type"], f["check"], f["field"], f["diff"], f["count"]) for f in last["failure_clusters"][:100]])))
            sample = last.get("sample_for_human_review", {})
            if sample:
                v.append("<h4>Suggested human review sample (latest run)</h4>" + str(table(["Content type", "Entries"],
                         [(code(ct), Raw(", ".join(f"<code>{esc(s['source_id'])}</code>" for s in vv))) for ct, vv in sample.items()])))
        rec = c["reconcile"]
        if rec:
            cov = rec.get("reference_coverage", {})
            rbody = str(table(["Metric", "Value"], [("Entry links inlined/resolved", cov.get("covered")), ("Dropped by mapping", cov.get("dropped_by_mapping")),
                                                   ("Uncovered links", len(cov.get("uncovered", []))), ("Shared components", len(rec.get("shared_components", []))),
                                                   ("Orphan manifest rows", len(rec.get("orphan_rows", []))), ("Manifest updates (latest run)", len(rec.get("manifest_updates", []))),
                                                   ("Report", rec["file"])]))
            if cov.get("uncovered"):
                rbody += "<h4>Uncovered references (first 100)</h4>" + str(table(["Entry", "Field", "Target", "Why"], [(u["entry"], u["field"], u["target"], u["why"]) for u in cov["uncovered"][:100]]))
        else:
            rbody = "<p class='muted'>not run</p>"
        seo = c["seo"]
        if seo:
            reds = seo.get("redirects", [])
            changed = [r for r in reds if r["url_changed"] or r["status"] not in ("same",)]
            sbody = (f"<p class='muted sm'>{len(reds):,} URLs checked; {len(changed):,} differ or have a problem"
                     + (" (first 200 shown; all in <a href='data/redirects.csv'>redirects.csv</a>)" if len(changed) > 200 else " (all rows in <a href='data/redirects.csv'>redirects.csv</a>)") + ".</p>"
                     + str(table(["Source URL", "Builder URL", "Status", "URL changed", "Note"], [(code(r["source_url"]), code(r["builder_url"]), badge(r["status"]),
                                                                                                      "yes" if r["url_changed"] else "no", short(r.get("note", ""), 200)) for r in changed[:200]], "data"))
                     + "<h4 style='margin-top:14px'>SEO gaps</h4>" + ("<ul class='plain'>" + "".join(f"<li>{esc(g)}</li>" for g in seo.get("gaps", [])) + "</ul>" if seo.get("gaps") else "<p class='muted'>none</p>"))
        else:
            sbody = "<p class='muted'>not run</p>"
        by_ct: defaultdict[str, Counter[str]] = defaultdict(Counter)
        for r in c["rows"]:
            by_ct[r["content_type"]][r["status"]] += 1
        w = c["writes"]
        am = list(c["asset_map"].items())
        det = "".join([
            acc("dropped", "Deliberately not migrated", f"{len(c['dropped'])} field(s)", "<p class='muted sm'>Fields the approved mapping drops, and how many entries actually had a value.</p>"
                + str(table(["Content type", "Field", "Reason", "Entries with a value"], [(code(a), code(b), r, f"{n:,}") for a, b, r, n in c["dropped"]]))),
            acc("verification", "Verification", f"{counts.get('verified', 0):,} verified · {len(ver)} script run(s)", "".join(v)),
            acc("reconcile", "References and reconciliation", "links between entries", rbody),
            acc("seo", "Redirects & SEO", f"{len((seo or {}).get('redirects', [])):,} URL(s) checked", sbody),
            acc("assets", "Assets", f"{len(am):,} uploaded", table(["Contentful asset", "Builder URL", "File", "Uploaded"], [(code(k), code(v.get("builder_url")), v.get("name"), v.get("uploaded_at")) for k, v in am[:200]], "data")
                + (f"<p class='muted sm'>Showing 200 of {len(am):,}.</p>" if len(am) > 200 else "")),
            acc("manifest", "Manifest", f"{len(c['rows']):,} rows", "<p class='muted sm'>Full manifest: <a href='data/manifest.csv'>manifest.csv</a></p>"
                + str(table(["Content type", "Entries", "By status"], [(code(ct), f"{sum(cnt.values()):,}", Raw(" ".join(f"{badge(s)} {n:,}" for s, n in cnt.items()))) for ct, cnt in sorted(by_ct.items())]))),
            acc("writes", "Write log", f"{len(w):,} write(s)", "<p>" + ", ".join(f"{esc(k)} × {n:,}" for k, n in Counter(x.get('action') for x in w).items()) + "</p>"
                + str(table(["Time", "Action", "Source id", "Target"], [(x.get("timestamp"), x.get("action"), code(x.get("source_id")), code(x.get("target_id"))) for x in w[-20:]])))])
        return self.layout("Details", f"<div class='card'><div class='card-c' style='padding-top:4px;padding-bottom:4px'>{det}</div></div>", 0, "details")

    # -- data files
    def data_files(self):
        out, header = {}, ["group", "severity", "category", "source_id", "content_type", "title", "status", "builder_id", "source_url", "builder_url", "reason", "suggested_action", "detail_page"]
        every = []
        for g in self.groups:
            rows = [[g["id"], g["severity"], g["category"], i["source_id"], i["content_type"], i["title"], i["status"], i["builder_id"], i["source_url"], i["builder_url"],
                     i["reason"], g["decision"], self.detail_href(i["source_id"]) or ""] for i in g["items"]]
            out[f"worklists/{g['id']}.csv"] = csv_text(header, rows)
            every += rows
        out["worklists/all-attention.csv"] = csv_text(header, every)
        out["data/manifest.csv"] = csv_text(["source_id", "content_type", "status", "target_id", "retry_count", "notes"],
                                            [[r["source_id"], r["content_type"], r["status"], r.get("target_id") or "", r.get("retry_count", 0), r.get("notes") or ""] for r in self.c["rows"]])
        out["data/redirects.csv"] = csv_text(["source_id", "title", "source_url", "builder_url", "status", "url_changed", "note"],
                                             [[r["source_id"], (self.page_by_id.get(r["source_id"]) or {}).get("title", ""), r["source_url"], r["builder_url"], r["status"], r["url_changed"], r.get("note", "")]
                                              for r in (self.c["seo"] or {}).get("redirects", [])])
        return out

    def build(self):
        f = self.files
        f["assets/report.css"] = R.CSS + SITE_CSS
        f["assets/report.js"] = JS
        f["index.html"], f["attention.html"], f["progress.html"], f["details.html"] = self.index(), self.attention(), self.progress(), self.details()
        for g in self.groups:
            f.update(self.group_pages(g))
        f.update(self.pages_lists())
        for sid_ in sorted(self.detail_ids):
            if sid_ in self.page_by_id:
                f[f"page/{detail_name(sid_)}"] = self.detail_page(self.page_by_id[sid_])
        f.update(self.data_files())
        return f


def build_site(c: dict, out_dir: Path, page_size: int = PAGE_SIZE, sample_per_type: int = SAMPLE_PER_TYPE, make_zip: bool = False) -> dict:
    """Write the site into out_dir. Only files this tool wrote earlier (listed in .report-files.json) are ever removed."""
    out_dir = Path(out_dir)
    S = Site(c, page_size, sample_per_type)
    files = S.build()
    out_dir.mkdir(parents=True, exist_ok=True)
    marker = out_dir / ".report-files.json"
    old = []
    try:
        old = json.loads(marker.read_text())
    except (OSError, ValueError):
        pass
    root = out_dir.resolve()
    for rel, text in files.items():
        dest = (out_dir / rel)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text, encoding="utf-8")
    for rel in old:  # stale files from a previous (larger) build
        if rel not in files:
            p = (out_dir / rel).resolve()
            if root in p.parents and p.is_file():
                p.unlink()
    marker.write_text(json.dumps(sorted(files)))
    zip_path = None
    if make_zip:
        zip_path = out_dir.parent / "migration-report.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
            for rel in sorted(files):
                z.write(out_dir / rel, f"migration-report/{rel}")
    return {"files": len(files), "groups": len(S.groups), "attention_entries": len(S.attn_ids), "pages": len(c["pages"]), "detail_pages": len(S.detail_ids),
            "bytes": sum(len(t.encode()) for t in files.values()), "zip": str(zip_path) if zip_path else None}
