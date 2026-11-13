"""Tests for Stage 7: scripts/reconcile.py and scripts/seo_redirect_check.py (temp manifest, mocked Builder, no network).

Run: python3 -m unittest scripts/test_stage7.py -v
The reconcile integration tests need exports/entries.jsonl; the pure-function tests do not.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "transform"))
HAVE_EXPORT = (ROOT / "exports" / "entries.jsonl").exists()


def cli(*args, stdin=None):
    p = subprocess.run([sys.executable, str(ROOT / "scripts" / "manifest.py"), *args], input=stdin, text=True, capture_output=True)
    return json.loads(p.stdout) if p.stdout.strip() else {}


def link(i):
    return {"sys": {"type": "Link", "linkType": "Entry", "id": i}}


def ent(eid, ct, **fields):
    return {"sys": {"id": eid, "contentType": {"sys": {"id": ct}}}, "fields": {k: {"en-US": v} for k, v in fields.items()}}


MAPPING = {"content_types": {
    "page": {"target_model": "page", "fields": [
        {"source": "title", "target": "data.title", "type": "text"},
        {"source": "slug", "target": "data.url", "type": "slug", "constant_map": {"/": "/home-new"}},
        {"source": "componentsReference", "target": "data.blocks", "type": "inline"},
        {"source": "related", "type": "dropped", "reason": "x"},
        {"source": "author", "target": "data.author", "type": "reference"}]},
    "hero": {"inline_into": "page", "target_component": "HeroCentered", "fields": []}}}


class ReferenceCoverage(unittest.TestCase):
    def test_inlined_dropped_reference_and_uncovered(self):
        import reconcile
        entries = {
            "p": ent("p", "page", componentsReference=[link("h")], related=[link("h")], author=link("a1"), stray=link("h")),
            "h": ent("h", "hero"), "a1": ent("a1", "person")}
        cov = reconcile.reference_coverage(entries, MAPPING, "en-US", id_map={})
        self.assertEqual((cov["covered"], cov["dropped_by_mapping"]), (1, 1))
        why = {u["field"]: u["why"] for u in cov["uncovered"]}
        self.assertIn("not in the mapping" if False else "field not in mapping", why["stray"])
        self.assertIn("id map", why["author"])
        cov2 = reconcile.reference_coverage(entries, MAPPING, "en-US", id_map={"a1": "B1"})
        self.assertEqual(cov2["covered"], 2)


class SeoRedirect(unittest.TestCase):
    def setUp(self):
        import seo_redirect_check as s
        self.s = s
        self.entries = {"home": ent("home", "page", title="Home", slug="/"), "about": ent("about", "page", title="About", slug="/about"),
                        "faq": ent("faq", "page", title=None, slug="/faq"), "gone": ent("gone", "page", title="Gone", slug="/gone")}
        self.urls = {"/": {"id": "live-home"}, "/home-new": {"id": "B-home"}, "/about": {"id": "B-about"}, "/schedule": {"id": "x"}}

    def test_statuses(self):
        rows = {"home": {"target_id": "B-home"}, "about": {"target_id": "B-about"}, "gone": {"target_id": "B-gone"}}
        res = {r["source_id"]: r for r in self.s.analyze(self.entries, MAPPING, "en-US", rows, self.urls)}
        self.assertEqual(res["home"]["status"], "rewritten")
        self.assertTrue(res["home"]["url_changed"])
        self.assertIn("already served by Builder page live-home", res["home"]["note"])
        self.assertEqual(res["about"]["status"], "same")
        self.assertEqual(res["faq"]["status"], "not-migrated")
        self.assertEqual(res["gone"]["status"], "target-missing")

    def test_mismatch(self):
        rows = {"about": {"target_id": "B-about"}}
        urls = {"/about-us": {"id": "B-about"}}
        r = self.s.analyze({"about": self.entries["about"]}, MAPPING, "en-US", rows, urls)[0]
        self.assertEqual(r["status"], "url-mismatch")

    def test_not_migrated_collision_is_reported(self):
        e = {"sch": ent("sch", "page", title="S", slug="/schedule")}
        r = self.s.analyze(e, MAPPING, "en-US", {}, self.urls)[0]
        self.assertEqual(r["status"], "not-migrated")
        self.assertIn("collision", r["note"])

    def test_seo_field_detection(self):
        types = [{"sys": {"id": "page"}, "fields": [{"id": "title", "name": "Title"}, {"id": "seoTitle", "name": "SEO"},
                                                  {"id": "metaDescription", "name": "Meta"}, {"id": "ogImage", "name": "OG"},
                                                  {"id": "slug", "name": "slug"}]}]
        found = self.s.seo_fields(types, MAPPING)["page"]
        self.assertEqual([h["field"] for h in found], ["seoTitle", "metaDescription", "ogImage"])
        self.assertTrue(all(not h["mapped"] for h in found))

    def test_analysing_thousands_of_pages_against_tens_of_thousands_of_urls_is_fast(self):
        import time
        n = 2000
        entries = {f"p{i}": ent(f"p{i}", "page", title=f"T{i}", slug=f"/p/{i}") for i in range(n)}
        urls = {f"/u/{i}": {"id": f"B{i}"} for i in range(20_000)}
        t0 = time.time()
        res = self.s.analyze(entries, MAPPING, "en-US", {}, urls)
        self.assertEqual(len(res), n)
        self.assertLess(time.time() - t0, 3.0)  # was ~16 s: a 20k-entry list was rebuilt and re-normalized for every page


@unittest.skipUnless(HAVE_EXPORT, "exports/ not present")
class ReconcileIntegration(unittest.TestCase):
    ROWS = [("seed-page-landing", "page"), ("seed-hero-field-report", "componentHero"), ("seed-hero-split", "componentHeroSplit"),
            ("seed-product-grid", "componentProductGridSelected"), ("seed-faq", "componentFaq"), ("seed-faq-item-1", "componentFaqItem"),
            ("orphan-row", "componentHero")]

    def setUp(self):
        from unittest import mock
        _env = mock.patch.dict(os.environ)  # restore MANIFEST_DB etc. afterwards so tests don't leak into each other
        _env.start()
        self.addCleanup(_env.stop)
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["MANIFEST_DB"] = str(Path(self.tmp.name) / "m.db")
        os.environ["WRITES_LOG"] = str(Path(self.tmp.name) / "w.jsonl")
        os.environ["REPORTS_DIR"] = str(Path(self.tmp.name) / "reports")
        os.environ["BUILDER_CONTENT_MCP_PK"] = "k"
        cli("seed", "--file", "-", stdin=json.dumps([{"source_id": a, "content_type": b} for a, b in self.ROWS]))
        import reconcile
        import run_migration as rm
        self.rc, self.rm = reconcile, rm
        mapping = json.loads((ROOT / "mapping" / "field-mapping.json").read_text())
        entries = {}
        for ln in (ROOT / "exports" / "entries.jsonl").read_text().splitlines():
            if ln.strip():
                e = json.loads(ln)
                entries[e["sys"]["id"]] = e
        payload, _, _, _ = rm.build_page(entries["seed-page-landing"], entries, mapping, "en-US", [], None)
        self.doc = {"id": "T1", "name": payload["name"], "published": "draft", "data": payload["data"]}
        rm.http = lambda method, url, key, body=None: {"results": [self.doc]}
        self.id_backup = [(ROOT / "exports" / n).read_text() if (ROOT / "exports" / n).exists() else None for n in ("id_map.json", "inline_map.json")]

    def tearDown(self):
        for n, old in zip(("id_map.json", "inline_map.json"), self.id_backup):  # these tests write real exports/ files: restore
            p = ROOT / "exports" / n
            p.write_text(old) if old is not None else p.unlink(missing_ok=True)
        self.tmp.cleanup()

    def run_main(self, *a):
        old = sys.argv
        sys.argv = ["reconcile.py", *a]
        try:
            return self.rc.main()
        finally:
            sys.argv = old

    def test_verified_parent_verifies_children_when_live_check_clean(self):
        cli("set", "seed-page-landing", "--status", "verified", "--target", "T1")
        self.run_main("--apply")
        for kid in ("seed-hero-field-report", "seed-faq", "seed-faq-item-1", "seed-product-grid"):
            self.assertEqual(cli("get", kid)["status"], "verified")
        self.assertEqual(cli("get", "orphan-row")["status"], "pending")  # not referenced: untouched, reported

    def test_dirty_live_check_blocks_verification(self):
        cli("set", "seed-page-landing", "--status", "verified", "--target", "T1")
        self.doc["data"]["blocks"][0]["component"]["options"]["heading"] = "WRONG"
        self.run_main("--apply")
        self.assertEqual(cli("get", "seed-hero-field-report")["status"], "migrated")  # advanced, but NOT verified
        self.assertEqual(cli("get", "seed-page-landing")["status"], "verified")  # parent is left for a human/re-verify

    def test_migrated_parent_only_migrates_children_and_dry_run_changes_nothing(self):
        cli("set", "seed-page-landing", "--status", "migrated", "--target", "T1")
        self.run_main()  # dry run
        self.assertEqual(cli("get", "seed-faq")["status"], "pending")
        self.run_main("--apply")
        self.assertEqual(cli("get", "seed-faq")["status"], "migrated")

    def test_inlined_components_follow_a_skipped_page(self):
        cli("set", "seed-page-landing", "--status", "skipped", "--notes", "[skipped: out of scope]")
        self.run_main("--apply")
        for kid in ("seed-hero-field-report", "seed-faq", "seed-faq-item-1", "seed-product-grid"):
            self.assertEqual(cli("get", kid)["status"], "skipped")
        self.assertEqual(cli("get", "orphan-row")["status"], "pending")
        self.assertIn("skipped with parent page", cli("get", "seed-faq")["notes"])

    def test_a_row_that_changed_since_it_was_read_is_left_alone(self):
        cli("set", "seed-page-landing", "--status", "migrated", "--target", "T1")
        real = self.rm.manifest

        def race(*args):
            if args[:1] == ("set",) and args[1] == "seed-faq" and "--expect-status" in args:
                real("set", "seed-faq", "--status", "failed")  # someone else changed it between our read and our write
            return real(*args)
        from unittest import mock
        with mock.patch.object(self.rm, "manifest", race):
            self.run_main("--apply")
        self.assertEqual(cli("get", "seed-faq")["status"], "failed")           # not overwritten
        self.assertEqual(cli("get", "seed-hero-split")["status"], "migrated")  # the others proceed

    def test_a_published_parent_does_not_verify_its_components(self):
        cli("set", "seed-page-landing", "--status", "verified", "--target", "T1")
        self.doc["published"] = "published"  # a human published the draft
        self.run_main("--apply")
        for kid in ("seed-hero-field-report", "seed-faq", "seed-product-grid"):
            self.assertNotEqual(cli("get", kid)["status"], "verified", kid)

    def test_never_touches_failed_rows(self):
        cli("set", "seed-page-landing", "--status", "verified", "--target", "T1")
        cli("set", "seed-faq", "--status", "failed")
        self.run_main("--apply")
        self.assertEqual(cli("get", "seed-faq")["status"], "failed")


if __name__ == "__main__":
    unittest.main()
