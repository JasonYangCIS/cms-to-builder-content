"""Tests for scripts/run_verification.py: temp manifest DB, MOCKED Builder (no network).

Run: python3 -m unittest scripts/test_run_verification.py -v
Needs exports/entries.jsonl (run scripts/export_contentful.py first); skipped otherwise.
"""
import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
HAVE_EXPORT = (ROOT / "exports" / "entries.jsonl").exists()
TID = "TARGET123"
ROWS = [
    {"source_id": "seed-page-landing", "content_type": "page"},
    {"source_id": "seed-hero-field-report", "content_type": "componentHero"},
    {"source_id": "seed-hero-split", "content_type": "componentHeroSplit"},
    {"source_id": "seed-product-grid", "content_type": "componentProductGridSelected"},
    {"source_id": "seed-faq", "content_type": "componentFaq"},
    {"source_id": "seed-faq-item-1", "content_type": "componentFaqItem"},
]


def cli(*args, stdin=None):
    p = subprocess.run([sys.executable, str(ROOT / "scripts" / "manifest.py"), *args], input=stdin, text=True, capture_output=True)
    return json.loads(p.stdout) if p.stdout.strip() else {}


@unittest.skipUnless(HAVE_EXPORT, "exports/ not present")
class Verification(unittest.TestCase):
    def setUp(self):
        from unittest import mock
        _env = mock.patch.dict(os.environ)  # restore MANIFEST_DB etc. afterwards so tests don't leak into each other
        _env.start()
        self.addCleanup(_env.stop)
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["MANIFEST_DB"] = str(Path(self.tmp.name) / "m.db")
        os.environ["WRITES_LOG"] = str(Path(self.tmp.name) / "w.jsonl")
        os.environ["BUILDER_CONTENT_MCP_PK"] = "test-key"
        os.environ["REPORTS_DIR"] = str(Path(self.tmp.name) / "reports")
        cli("seed", "--file", "-", stdin=json.dumps(ROWS))
        cli("set", "seed-page-landing", "--status", "migrated", "--target", TID, "--notes", "runner: created draft")
        import run_migration as rm
        import run_verification as rv
        self.rm, self.rv = rm, rv
        mapping = json.loads((ROOT / "mapping" / "field-mapping.json").read_text())
        self.entries = {}
        for ln in (ROOT / "exports" / "entries.jsonl").read_text().splitlines():
            if ln.strip():
                e = json.loads(ln)
                self.entries[e["sys"]["id"]] = e
        payload, exc, _, _ = rm.build_page(self.entries["seed-page-landing"], self.entries, mapping, "en-US", [], None)
        assert not exc
        pixel = {"@type": "@builder.io/sdk:Element", "id": "builder-pixel-abc", "tagName": "img"}
        self.doc = {"id": TID, "name": payload["name"], "published": "draft",
                    "data": {**payload["data"], "blocks": payload["data"]["blocks"] + [pixel], "state": {}}}
        self.result = {"results": [self.doc]}
        rm.http = lambda method, url, key, body=None: self.result
        self.report_dir = ROOT / "reports"

    def tearDown(self):
        self.tmp.cleanup()

    def run_main(self, *args, apply=True):
        old = sys.argv
        sys.argv = ["run_verification.py", "--content-type", "page", *(["--apply"] if apply else []), *args]
        try:
            return self.rv.main()
        finally:
            sys.argv = old

    def st(self, sid):
        return cli("get", sid)

    def test_good_page_verified_with_children_and_pixel_ignored(self):
        self.assertEqual(self.run_main(), 0)
        self.assertEqual(self.st("seed-page-landing")["status"], "verified")
        for kid in ("seed-hero-field-report", "seed-faq", "seed-faq-item-1", "seed-product-grid"):
            self.assertEqual(self.st(kid)["status"], "verified")
        self.assertIn("runner: created draft", self.st("seed-page-landing")["notes"])  # notes appended, not replaced

    def test_wrong_content_fails_with_specific_note(self):
        self.doc["data"]["blocks"][0]["component"]["options"]["heading"] = "WRONG"
        self.run_main()
        row = self.st("seed-page-landing")
        self.assertEqual(row["status"], "failed")
        self.assertIn("seed-hero-field-report", row["notes"])
        self.assertIn("header", row["notes"])  # the source field name

    def test_missing_block_fails(self):
        del self.doc["data"]["blocks"][3]
        self.run_main()
        self.assertEqual(self.st("seed-page-landing")["status"], "failed")
        self.assertIn("block", self.st("seed-page-landing")["notes"])

    def test_faq_item_difference_fails(self):
        self.doc["data"]["blocks"][3]["component"]["options"]["items"][0]["answerHtml"] = "<p>nope</p>"
        self.run_main()
        self.assertEqual(self.st("seed-page-landing")["status"], "failed")

    def test_archived_target_needs_human(self):
        self.doc["published"] = "archived"
        self.run_main()
        row = self.st("seed-page-landing")
        self.assertEqual(row["status"], "needs_human")
        self.assertIn("archived", row["notes"])

    def test_missing_target_fails(self):
        self.result = {"results": []}
        self.run_main()
        self.assertEqual(self.st("seed-page-landing")["status"], "failed")

    def test_dry_run_changes_nothing(self):
        self.doc["data"]["blocks"][0]["component"]["options"]["heading"] = "WRONG"
        self.run_main(apply=False)
        self.assertEqual(self.st("seed-page-landing")["status"], "migrated")

    def test_fuzzy_when_converter_flags_exceptions(self):
        entries = copy.deepcopy(self.entries)
        entries["seed-faq-item-1"]["fields"]["answer"]["en-US"]["content"].append(
            {"nodeType": "embedded-asset-block", "data": {"target": {"sys": {"id": "a1"}}}, "content": []})
        mapping = json.loads((ROOT / "mapping" / "field-mapping.json").read_text())
        doc = {**self.doc, "data": {**self.doc["data"], "blocks": self.doc["data"]["blocks"][:-1]}}  # no pixel (fetch strips it)
        checks = self.rv.verify_tree(entries["seed-page-landing"], entries, mapping, "en-US", doc)
        self.assertEqual([c["status"] for c in checks if c["status"] == "fuzzy"], ["fuzzy"])
        self.assertFalse([c for c in checks if c["status"] == "fail"])


    def test_a_bare_run_is_a_dry_run_and_only_apply_records_outcomes(self):
        old = sys.argv
        sys.argv = ["run_verification.py", "--content-type", "page"]  # no --apply
        try:
            self.assertEqual(self.rv.main(), 0)
        finally:
            sys.argv = old
        self.assertEqual(self.st("seed-page-landing")["status"], "migrated")
        self.run_main()
        self.assertEqual(self.st("seed-page-landing")["status"], "verified")

    def test_published_target_goes_to_needs_human_and_is_never_verified(self):
        self.doc["published"] = "published"
        self.run_main()
        row = self.st("seed-page-landing")
        self.assertEqual(row["status"], "needs_human")
        self.assertIn("PUBLISHED", row["notes"])
        self.assertIn("only creates drafts", row["notes"])
        self.assertNotEqual(self.st("seed-faq")["status"], "verified")  # its components are not verified through it either

    def test_the_published_state_of_every_checked_entry_is_in_the_run_report(self):
        self.run_main()
        reports = sorted(Path(os.environ["REPORTS_DIR"]).glob("verification-*.json"))
        rows = json.loads(reports[-1].read_text())["rows"]
        self.assertEqual([r["published"] for r in rows], ["draft"])

    def test_a_claim_lost_to_another_worker_is_not_overwritten(self):
        real = self.rv.rm.manifest

        def steal(*args):
            if args[:1] == ("set",) and "verified" in args and "seed-page-landing" in args:
                real("set", "seed-page-landing", "--status", "pending", "--expect-status", "in_progress")  # re-queued under us
            return real(*args)
        with mock.patch.object(self.rv.rm, "manifest", steal):
            self.run_main()
        self.assertEqual(self.st("seed-page-landing")["status"], "pending")  # our late 'verified' was refused


if __name__ == "__main__":
    unittest.main()
