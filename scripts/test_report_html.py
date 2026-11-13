"""Tests for the static report site (scripts/build_report_html.py + scripts/report_site.py).

Run: python3 -m unittest scripts/test_report_html.py -v
Everything is built into temporary folders from synthetic data; the collect() integration test needs a local mapping
and exports/ and skips itself otherwise.
"""
import csv
import io
import json
import os
import re
import sys
import tempfile
import unittest
import zipfile
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "transform"))
HAVE_LOCAL = (ROOT / "mapping" / "field-mapping.json").exists() and (ROOT / "exports" / "entries.jsonl").exists()
EVIL = "<script>alert(1)</script>"


def page(i, status="verified", note="", ct="page", title=None, exceptions=None):
    sid = f"page-{i:04d}"
    row = {"source_id": sid, "content_type": ct, "status": status, "target_id": f"B{i:04d}", "retry_count": 0, "notes": note}
    return {"source_id": sid, "content_type": ct, "title": title or f"Page {i}", "row": row, "builder_id": f"B{i:04d}",
            "payload": {"name": f"Page {i}", "data": {"url": f"/p/{i}"}}, "exceptions": exceptions or [],
            "blocks": [{"component": "HeroCentered", "options": {"heading": f"Heading {i}", "items": [{"question": "q", "answerHtml": "<p>a</p>"}]},
                        "source_id": f"{sid}-c0", "content_type": "componentHero"}],
            "redirect": {"source_id": sid, "source_url": f"/p/{i}", "builder_url": f"/p/{i}", "status": "same", "url_changed": False, "note": ""}}


def ctx(pages=None, **over):
    pages = pages if pages is not None else [page(0, "verified"), page(1, "needs_human", "URL /p/1 already exists in Builder; not overwriting. Needs a human-chosen URL.")]
    rows = [p["row"] for p in pages]
    c = {
        "generated_at": "2026-01-01 00:00 UTC", "commit": "abc1234", "excluded": None,
        "mapping": {"version": 3, "source": {"space": "SRC", "environment": "master", "locale": "en-US"}, "target": {"space": "TGT"},
                    "content_types": {"page": {"target_model": "page"}, "componentHero": {"inline_into": "page", "target_component": "HeroCentered",
                                      "fields": [{"source": "preheader", "type": "dropped", "reason": "no input"}]}}},
        "mapping_state": {"approved": True}, "inventory": {"totals": {"entries": len(rows), "assets": 1, "content_types": 2, "locales": ["en-US"]}},
        "rows": rows, "summary": {}, "assets_referenced": 1,
        "asset_map": {"a1": {"builder_url": "https://cdn.example/x", "name": "x.jpg", "uploaded_at": "t"}},
        "writes": [{"timestamp": "t", "action": "create", "source_id": "page-0000", "target_id": "B0000"}],
        "dropped": [("componentHero", "preheader", "no input", 2)], "verification_runs": [], "reconcile": None,
        "seo": {"file": "seo-1.json", "redirects": [p["redirect"] for p in pages], "gaps": ["No SEO fields"]}, "pages": pages, "entries_count": len(rows)}
    c.update(over)
    return c


class Balanced(HTMLParser):
    VOID = {"meta", "br", "hr", "img", "input", "link"}

    def __init__(self):
        super().__init__()
        self.stack, self.err = [], []

    def handle_starttag(self, tag, attrs):
        if tag not in self.VOID:
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if tag in self.VOID:
            return
        if not self.stack or self.stack[-1] != tag:
            self.err.append(tag)
        else:
            self.stack.pop()


def build(c, **kw):
    import report_site as rs
    tmp = tempfile.TemporaryDirectory()
    out = Path(tmp.name) / "site"
    summary = rs.build_site(c, out, **kw)
    return tmp, out, summary


class SiteStructure(unittest.TestCase):
    def setUp(self):
        hostile = [page(0, "verified", title=EVIL), page(1, "needs_human", EVIL, title=EVIL, exceptions=[{"reason": EVIL}]), page(2)]
        c = ctx(hostile)
        c["verification_runs"] = [{"file": "v.json", "outcomes": {"verified": 1}, "failure_clusters": [{"content_type": "x", "check": "c", "field": "f", "diff": EVIL, "count": 1}],
                                   "fuzzy_items": [{"source_id": "page-0000", "entry": "e", "field": "f", "diff": EVIL}], "sample_for_human_review": {}}]
        c["reconcile"] = {"file": "r.json", "reference_coverage": {"covered": 1, "dropped_by_mapping": 0, "uncovered": [{"entry": "page-0002", "field": "x", "target": "t", "why": EVIL}]},
                          "manifest_updates": [], "shared_components": [], "orphan_rows": []}
        self.tmp, self.out, self.summary = build(c)
        self.addCleanup(self.tmp.cleanup)

    def htmls(self):
        return sorted(self.out.rglob("*.html"))

    def test_expected_files_exist(self):
        for rel in ("index.html", "attention.html", "pages-1.html", "progress.html", "details.html", "assets/report.css", "assets/report.js",
                    "worklists/all-attention.csv", "data/manifest.csv", "data/redirects.csv"):
            self.assertTrue((self.out / rel).exists(), rel)
        self.assertTrue(list((self.out / "attention").glob("*.html")))
        self.assertTrue(list((self.out / "page").glob("*.html")))

    def test_hostile_content_is_escaped_in_every_page(self):
        found = False
        for f in self.htmls():
            t = f.read_text()
            self.assertNotIn(EVIL, t, f.name)
            found = found or "&lt;script&gt;alert(1)&lt;/script&gt;" in t
        self.assertTrue(found)

    def test_exactly_one_known_script_per_page_and_no_external_requests(self):
        for f in self.htmls():
            t = f.read_text()
            self.assertEqual(len(re.findall(r"<script\b", t)), 1, f.name)
            self.assertRegex(t, r"<script src='(\.\./)*assets/report\.js' defer></script>")
            self.assertFalse(re.search(r"(src|href)=['\"]https?://", t), f.name)
            self.assertNotIn("@import", t)

    def test_every_page_is_well_formed(self):
        for f in self.htmls():
            p = Balanced()
            p.feed(f.read_text())
            self.assertEqual((p.err, p.stack), ([], []), f.name)

    def test_js_makes_no_network_calls_and_never_uses_html_injection(self):
        js = (self.out / "assets" / "report.js").read_text()
        for bad in ("fetch(", "XMLHttpRequest", "WebSocket", "eval(", "new Function", "innerHTML", "outerHTML", "document.write", "http", "import("):
            self.assertNotIn(bad, js, bad)

    def test_relative_links_resolve_inside_the_site(self):
        for f in self.htmls():
            for href in re.findall(r"(?:href|src)='([^'#]+)'", f.read_text()):
                if href.startswith(("http", "mailto")):
                    continue
                self.assertTrue((f.parent / href.split("#")[0]).resolve().exists(), f"{f.name}: {href}")

    def test_nav_counts_and_verdict(self):
        t = (self.out / "index.html").read_text()
        self.assertIn("Needs attention", t)
        self.assertRegex(t, r"1 of 3 pages verified|[0-9]+ of 3 pages verified")
        self.assertIn("this pipeline never publishes", t)

    def test_summary_numbers(self):
        self.assertEqual(self.summary["pages"], 3)
        self.assertGreaterEqual(self.summary["groups"], 1)


class Grouping(unittest.TestCase):
    def setUp(self):
        import report_site as rs
        self.rs = rs

    def test_signature_strips_ids_urls_and_numbers(self):
        a = self.rs.reason_signature("URL /seed-landing already exists in Builder; target 27dc6c90f0864dbea0d335943968406c at https://x.io/a?b=1 (page-00012)")
        b = self.rs.reason_signature("URL /other already exists in Builder; target ffffffffffffffffffffffffffffffff at https://y.io/z (page-99999)")
        self.assertEqual(a, b)
        self.assertNotRegex(a, r"\d")

    def test_classification(self):
        cls = self.rs.classify_row
        self.assertEqual(cls({"status": "failed", "notes": "x"}), "failed")
        self.assertEqual(cls({"status": "needs_human", "notes": "URL /a already exists in Builder"}), "url_exists")
        self.assertEqual(cls({"status": "needs_human", "notes": "target T is archived in Builder (human action)"}), "archived")
        self.assertEqual(cls({"status": "needs_human", "notes": "something else"}), "needs_human")

    def test_hundreds_of_identical_problems_become_one_group_with_a_csv(self):
        pages = [page(i, "needs_human", f"URL /p/{i} already exists in Builder; not overwriting. Needs a human-chosen URL.") for i in range(250)]
        tmp, out, s = build(ctx(pages))
        self.addCleanup(tmp.cleanup)
        self.assertEqual((s["groups"], s["attention_entries"]), (1, 250))
        gid = next((out / "worklists").glob("url_exists-*.csv"))
        rows = list(csv.reader(io.StringIO(gid.read_text())))
        self.assertEqual(len(rows) - 1, 250)
        self.assertEqual(rows[0][:5], ["group", "severity", "category", "source_id", "content_type"])
        # 250 items -> 100 / 100 / 50 across three list pages
        lists = sorted((out / "attention").glob("url_exists-*.html"))
        self.assertEqual(len(lists), 3)
        self.assertEqual([len(re.findall(r"<tr>", f.read_text())) - 1 for f in lists], [100, 100, 50])
        self.assertIn("Next →", lists[0].read_text())
        self.assertIn("← Previous", lists[2].read_text())

    def test_different_causes_stay_separate_and_destructive_sorts_first(self):
        pages = [page(0, "migrated", "created"), page(1, "failed", "verify: boom"), page(2, "needs_human", "target T is archived in Builder")]
        tmp, out, s = build(ctx(pages))
        self.addCleanup(tmp.cleanup)
        t = (out / "attention.html").read_text()
        self.assertEqual(s["groups"], 3)
        self.assertLess(t.index("Failed a check"), t.index("awaiting verification"))

    def test_verdict_wording_at_scale(self):
        pages = [page(i, "verified") for i in range(8)] + [page(100 + i, "failed", "verify: x") for i in range(2)]
        tmp, out, s = build(ctx(pages))
        self.addCleanup(tmp.cleanup)
        self.assertIn("8 of 10 pages verified. 2 entries need attention in 1 group.", (out / "index.html").read_text())

    def test_all_clear_when_nothing_needs_action(self):
        c = ctx([page(0), page(1)], dropped=[], seo=None)
        tmp, out, s = build(c)
        self.addCleanup(tmp.cleanup)
        self.assertEqual(s["groups"], 0)
        self.assertIn("All 2 pages are migrated as drafts and verified", (out / "index.html").read_text())
        self.assertIn("Nothing needs your action", (out / "attention.html").read_text())


class PagesAndDetails(unittest.TestCase):
    def test_pages_are_paginated_with_problems_first(self):
        pages = [page(i) for i in range(250)] + [page(900, "failed", "verify: x")]
        tmp, out, s = build(ctx(pages))
        self.addCleanup(tmp.cleanup)
        lists = sorted(out.glob("pages-*.html"))
        self.assertEqual(len(lists), 3)
        first = lists[0].read_text()
        self.assertLess(first.index("page-0900") if "page-0900" in first else first.index("Page 900"), first.index("Page 0"))
        self.assertEqual([len(re.findall(r"<tr>", f.read_text())) - 1 for f in lists], [100, 100, 51])

    def test_detail_pages_only_for_attention_and_a_bounded_verified_sample(self):
        pages = [page(i) for i in range(60)] + [page(500 + i, "failed", "verify: x") for i in range(7)]
        tmp, out, s = build(ctx(pages), sample_per_type=5)
        self.addCleanup(tmp.cleanup)
        details = list((out / "page").glob("*.html"))
        self.assertEqual(len(details), 7 + 5)  # every problem page + 5 verified samples
        self.assertEqual(s["detail_pages"], 12)
        # a page row links to its detail file only when that file exists
        for f in sorted(out.glob("pages-*.html")):
            for href in re.findall(r"href='(page/[^']+)'", f.read_text()):
                self.assertTrue((out / href).exists())
        again = build(ctx(pages), sample_per_type=5)  # the sample is deterministic
        self.addCleanup(again[0].cleanup)
        self.assertEqual(sorted(p.name for p in details), sorted(p.name for p in (again[1] / "page").glob("*.html")))

    def test_detail_page_lists_its_open_issues_and_blocks(self):
        tmp, out, s = build(ctx())
        self.addCleanup(tmp.cleanup)
        t = next((out / "page").glob("page-0001-*.html")).read_text()
        self.assertIn("URL already exists in Builder", t)
        self.assertIn("HeroCentered", t)
        self.assertIn("Heading 1", t)


class Csv(unittest.TestCase):
    def test_formula_injection_is_neutralized_and_round_trips(self):
        import report_site as rs
        text = rs.csv_text(["a", "b"], [["=HYPERLINK(\"http://x\")", "+1"], ["-2", "@SUM(A1)"], ['has,comma "quote"\nnewline', "ok"]])
        rows = list(csv.reader(io.StringIO(text)))
        self.assertEqual(rows[1], ["'=HYPERLINK(\"http://x\")", "'+1"])
        self.assertEqual(rows[2], ["'-2", "'@SUM(A1)"])
        self.assertEqual(rows[3], ['has,comma "quote"\nnewline', "ok"])

    def test_worklist_csv_carries_reason_action_and_detail_link(self):
        tmp, out, s = build(ctx())
        self.addCleanup(tmp.cleanup)
        rows = list(csv.DictReader(io.StringIO((out / "worklists" / "all-attention.csv").read_text())))
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual((r["source_id"], r["category"], r["severity"]), ("page-0001", "url_exists", "destructive"))
        self.assertIn("already exists", r["reason"])
        self.assertTrue(r["suggested_action"])
        self.assertTrue(r["detail_page"].startswith("page/"))
        self.assertTrue((out / r["detail_page"]).exists())


class Cleanup(unittest.TestCase):
    def test_rebuild_removes_only_stale_generated_files(self):
        import report_site as rs
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "site"
            rs.build_site(ctx([page(i, "failed", "verify: x") for i in range(150)]), out)
            self.assertTrue((out / "pages-2.html").exists())
            (out / "my-notes.txt").write_text("keep me")
            rs.build_site(ctx([page(0)]), out)
            self.assertFalse((out / "pages-2.html").exists())
            self.assertTrue((out / "my-notes.txt").exists())
            self.assertEqual(len(list((out / "page").glob("*.html"))), 1)

    def test_marker_cannot_make_it_delete_files_outside_the_site(self):
        import report_site as rs
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "site"
            out.mkdir()
            victim = Path(d) / "victim.txt"
            victim.write_text("precious")
            (out / ".report-files.json").write_text(json.dumps(["../victim.txt"]))
            rs.build_site(ctx(), out)
            self.assertTrue(victim.exists())

    def test_zip_contains_the_whole_site(self):
        import report_site as rs
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "site"
            s = rs.build_site(ctx(), out, make_zip=True)
            with zipfile.ZipFile(s["zip"]) as z:
                names = z.namelist()
            self.assertIn("migration-report/index.html", names)
            self.assertIn("migration-report/assets/report.js", names)


class SkippedEntries(unittest.TestCase):
    """`skipped` = deliberately not migrated: out of scope for the verdict, never an attention item."""

    def build(self):
        pages = [page(0), page(1)] + [page(10 + i, "skipped", "[skipped: legacy campaign pages] URL /x already exists in Builder",
                                           exceptions=[{"reason": "embedded-entry-block needs manual handling"}]) for i in range(3)]
        tmp, out, s = build(ctx(pages, dropped=[], seo=None))
        self.addCleanup(tmp.cleanup)
        return out, s

    def test_verdict_and_attention_ignore_skipped_pages(self):
        out, s = self.build()
        self.assertEqual(s["groups"], 0)
        t = (out / "index.html").read_text()
        self.assertIn("All 2 pages are migrated as drafts and verified. 3 skipped on purpose.", t)

    def test_skipped_is_reported_as_information_with_its_reason(self):
        out, s = self.build()
        t = (out / "attention.html").read_text()
        self.assertIn("3 entries are skipped on purpose", t)
        self.assertIn("legacy campaign pages", t)
        self.assertIn("Nothing needs your action", t)

    def test_skipped_pages_sort_last_and_have_a_status_meaning(self):
        out, s = self.build()
        t = (out / "pages-1.html").read_text()
        self.assertLess(t.index("Page 0"), t.index("Page 10"))
        self.assertIn("Deliberately not migrated", (out / "progress.html").read_text())


class PublishedTargets(unittest.TestCase):
    """The report must not claim nothing is published; it must surface pages a human published."""

    def groups(self, c):
        import report_site as rs
        return rs.build_groups(c)[0]

    def test_a_needs_human_note_about_a_published_target_forms_the_published_group_once(self):
        pages = [page(0), page(1, "needs_human", "target B0001 is PUBLISHED in Builder. This pipeline only creates drafts, so a human published it")]
        groups = self.groups(ctx(pages))
        self.assertEqual([g["category"] for g in groups], ["published"])
        self.assertEqual([i["source_id"] for g in groups for i in g["items"]], ["page-0001"])

    def test_the_runners_refusal_to_patch_a_published_target_is_the_same_group(self):
        pages = [page(2, "needs_human", "runner: target B0002 is published in Builder, not a draft; refusing to edit it")]
        self.assertEqual([g["category"] for g in self.groups(ctx(pages))], ["published"])

    def test_archived_targets_stay_a_separate_cause(self):
        pages = [page(3, "needs_human", "target B0003 is archived in Builder (human action); decide whether to restore or drop")]
        self.assertEqual([g["category"] for g in self.groups(ctx(pages))], ["archived"])

    def test_published_state_from_the_latest_verification_run_is_surfaced_without_double_counting(self):
        c = ctx([page(0), page(1, "needs_human", "target B1 is PUBLISHED in Builder")])
        c["verification_runs"] = [{"file": "v.json", "outcomes": {}, "failure_clusters": [], "fuzzy_items": [], "sample_for_human_review": {},
                                   "rows": [{"source_id": "page-0000", "builder_id": "B0", "published": "published"},
                                            {"source_id": "page-0001", "builder_id": "B1", "published": "published"},
                                            {"source_id": "page-0002", "builder_id": "B2", "published": "draft"}]}]
        groups = self.groups(c)
        ids = sorted(i["source_id"] for g in groups if g["category"] == "published" for i in g["items"])
        self.assertEqual(ids, ["page-0000", "page-0001"])  # page-0001 appears once, page-0002 (a draft) not at all

    def test_the_report_no_longer_says_nothing_is_published(self):
        tmp, out, s = build(ctx())
        self.addCleanup(tmp.cleanup)
        for f in out.rglob("*.html"):
            self.assertNotIn("Nothing is published", f.read_text(), f.name)
            self.assertNotIn("nothing is published", f.read_text(), f.name)
        self.assertIn("this pipeline never publishes", (out / "index.html").read_text())


class DamagedInputs(unittest.TestCase):
    def test_a_damaged_line_is_skipped_and_counted_not_the_whole_file_dropped(self):
        import build_report_html as b
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "writes.jsonl"
            f.write_text('{"a": 1}\n{"a": 2}\n{"a": 3, "half-writ\n{"a": 4}\n\n')
            warnings: list = []
            self.assertEqual([r["a"] for r in b.read_jsonl(f, warnings)], [1, 2, 4])
            self.assertEqual(len(warnings), 1)
            self.assertIn("writes.jsonl: 1 damaged line(s) skipped", warnings[0])

    def test_clean_and_missing_files_produce_no_warning(self):
        import build_report_html as b
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "ok.jsonl"
            f.write_text('{"a": 1}\n')
            warnings: list = []
            self.assertEqual(b.read_jsonl(f, warnings), [{"a": 1}])
            self.assertEqual(b.read_jsonl(Path(d) / "missing.jsonl", warnings), [])
            self.assertEqual(warnings, [])

    def test_the_overview_shows_the_warning(self):
        tmp, out, s = build(ctx(warnings=["entries.jsonl: 3 damaged line(s) skipped; the numbers below may be incomplete"]))
        self.addCleanup(tmp.cleanup)
        t = (out / "index.html").read_text()
        self.assertIn("Some input files were damaged", t)
        self.assertIn("3 damaged line(s) skipped", t)

    def test_csv_cells_starting_with_a_newline_are_neutralized_too(self):
        import report_site as rs
        text = rs.csv_text(["a"], [["\n=SUM(A1)"], ["=1+1"], ["safe"]])
        rows = list(csv.reader(io.StringIO(text)))
        self.assertEqual([r[0] for r in rows[1:]], ["'\n=SUM(A1)", "'=1+1", "safe"])


class VerificationRuns(unittest.TestCase):
    def test_dry_runs_and_damaged_files_are_not_runs_on_record(self):
        import build_report_html as b
        with tempfile.TemporaryDirectory() as d:
            r = Path(d)
            (r / "verification-1.json").write_text(json.dumps({"dry_run": False, "outcomes": {"verified": 2}, "rows": [{"source_id": "a"}]}))
            (r / "verification-2.json").write_text(json.dumps({"dry_run": True, "outcomes": {}, "rows": []}))
            (r / "verification-3.json").write_text("{not json")
            (r / "verification-4.json").write_text("[1, 2]")
            runs, hidden = b.load_runs(r, lambda sid: False)
            self.assertEqual([x["file"] for x in runs], ["verification-1.json"])
            self.assertEqual(hidden, 0)

    def test_runs_that_only_covered_excluded_entries_are_counted_as_hidden(self):
        import build_report_html as b
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "verification-1.json").write_text(json.dumps({"outcomes": {"needs_human": 1}, "rows": [{"source_id": "fx-1", "outcome": "needs_human"}]}))
            runs, hidden = b.load_runs(Path(d), lambda sid: str(sid).startswith("fx-"))
            self.assertEqual((runs, hidden), ([], 1))


class Exclusion(unittest.TestCase):
    """--exclude-prefix must apply to every section so the counts agree everywhere."""
    skip = staticmethod(lambda sid: str(sid).startswith("fx-"))

    def run_report(self):
        return {"file": "verification-1.json", "outcomes": {"needs_human": 1, "verified": 1},
                "rows": [{"source_id": "fx-1", "outcome": "needs_human"}, {"source_id": "real-1", "outcome": "verified"}],
                "failure_clusters": [{"content_type": "x", "check": "c", "field": "f", "diff": "d", "count": 1}],
                "fuzzy_items": [{"source_id": "fx-1"}, {"source_id": "real-1"}],
                "sample_for_human_review": {"page": [{"source_id": "fx-1"}, {"source_id": "real-1"}], "hero": [{"source_id": "fx-2"}]}}

    def test_filter_run_recomputes_outcomes_and_hides_aggregates_it_cannot_attribute(self):
        import build_report_html as b
        out = b.filter_run(self.run_report(), self.skip)
        assert out is not None
        self.assertEqual(out["outcomes"], {"verified": 1})
        self.assertEqual(out["failure_clusters"], [])
        self.assertIn("hidden", out["clusters_note"])
        self.assertEqual([f["source_id"] for f in out["fuzzy_items"]], ["real-1"])
        self.assertEqual(out["sample_for_human_review"], {"page": [{"source_id": "real-1"}]})

    def test_filter_run_drops_runs_that_only_covered_excluded_entries(self):
        import build_report_html as b
        run = dict(self.run_report(), rows=[{"source_id": "fx-1", "outcome": "needs_human"}])
        self.assertIsNone(b.filter_run(run, self.skip))

    def test_filter_run_untouched_when_nothing_excluded(self):
        import build_report_html as b
        run = dict(self.run_report(), rows=[{"source_id": "real-1", "outcome": "verified"}])
        self.assertIs(b.filter_run(run, self.skip), run)

    def test_filter_reconcile_and_seo(self):
        import build_report_html as b
        rec = {"manifest_updates": [{"source_id": "fx-1"}, {"source_id": "r"}], "orphan_rows": [{"source_id": "fx-2"}],
               "shared_components": [{"component": "fx-3"}, {"component": "ok"}], "reference_coverage": {"covered": 99}}
        out = b.filter_reconcile(rec, self.skip, {"covered": 8, "dropped_by_mapping": 0, "uncovered": []})
        assert out is not None
        self.assertEqual((len(out["manifest_updates"]), len(out["orphan_rows"]), len(out["shared_components"])), (1, 0, 1))
        self.assertEqual(out["reference_coverage"]["covered"], 8)
        seo = b.filter_seo({"redirects": [{"source_id": "fx-1"}, {"source_id": "p"}], "gaps": ["g"]}, self.skip)
        assert seo is not None
        self.assertEqual([r["source_id"] for r in seo["redirects"]], ["p"])
        self.assertIsNone(b.filter_seo(None, self.skip))

    def test_filtered_view_banner_is_shown_on_the_overview(self):
        tmp, out, s = build(ctx(excluded={"prefixes": ["fx-"], "rows": 4, "writes": 1, "runs_hidden": 1}))
        self.addCleanup(tmp.cleanup)
        self.assertIn("Filtered view", (out / "index.html").read_text())


@unittest.skipUnless(HAVE_LOCAL, "needs a local mapping and exports/")
class Collect(unittest.TestCase):
    def test_collect_and_build_from_local_files(self):
        import build_report_html as b
        from unittest import mock
        clean = {k: v for k, v in os.environ.items() if k not in ("MANIFEST_DB", "WRITES_LOG", "REPORTS_DIR")}
        with mock.patch.dict(os.environ, clean, clear=True):
            full, trimmed = b.collect(), b.collect(exclude=("seed-",))
            self.assertLess(len(trimmed["rows"]), len(full["rows"]))
            self.assertFalse([r for r in trimmed["rows"] if r["source_id"].startswith("seed-")])
            tmp, out, s = build(full)
            self.addCleanup(tmp.cleanup)
            self.assertTrue((out / "index.html").exists())
            for f in out.rglob("*.html"):
                p = Balanced()
                p.feed(f.read_text())
                self.assertEqual((p.err, p.stack), ([], []), f.name)


if __name__ == "__main__":
    unittest.main()
