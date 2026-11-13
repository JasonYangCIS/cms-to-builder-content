"""Tests for scripts/run_migration.py: the Builder HTTP layer (urlopen mocked) and the apply loop (temp manifest DB, MOCKED
Builder; no network, no writes anywhere real).

Run: python3 -m unittest scripts/test_run_migration.py -v
`Runner` needs exports/entries.jsonl (run scripts/export_contentful.py first); `Synthetic` and `HttpLayer` do not.
"""
from __future__ import annotations

import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
HAVE_EXPORT = (ROOT / "exports" / "entries.jsonl").exists()
HAVE_MAPPING = (ROOT / "mapping" / "field-mapping.json").exists()

ROWS = [
    {"source_id": "seed-page-landing", "content_type": "page"},
    {"source_id": "seed-hero-field-report", "content_type": "componentHero"},
    {"source_id": "seed-hero-split", "content_type": "componentHeroSplit"},
    {"source_id": "seed-product-grid", "content_type": "componentProductGridSelected"},
    {"source_id": "seed-faq", "content_type": "componentFaq"},
    {"source_id": "seed-faq-item-1", "content_type": "componentFaqItem"},
]


def page_entry(eid, slug, title="T"):
    fields: dict = {"componentsReference": {"en-US": []}}
    if title is not None:
        fields["title"] = title if isinstance(title, dict) else {"en-US": title}
    if slug is not None:
        fields["slug"] = slug if isinstance(slug, dict) else {"en-US": slug}
    return {"sys": {"id": eid, "contentType": {"sys": {"id": "page"}}}, "fields": fields}


class RunnerBase(unittest.TestCase):
    """A temp manifest, a mocked Builder, and helpers; subclasses decide which exports the runner reads."""
    exports_dir: Path | None = None  # None = the real exports/

    def setUp(self):
        env = mock.patch.dict(os.environ)  # restore MANIFEST_DB etc. afterwards so tests don't leak into each other
        env.start()
        self.addCleanup(env.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        d = Path(self.tmp.name)
        os.environ.update(MANIFEST_DB=str(d / "m.db"), WRITES_LOG=str(d / "w.jsonl"), RUNNER_UNSAVED_LOG=str(d / "unsaved.jsonl"),
                          BUILDER_CONTENT_MCP_PK="test-key")
        if self.exports_dir:
            os.environ["EXPORTS_DIR"] = str(self.exports_dir)
        import run_migration as rm
        self.rm = rm
        self.calls: list = []
        self.urls = {"/": {"id": "live-home", "model": "page"}}
        self.post_reply = {"id": "NEW123"}
        self.post_exc = None
        self.patch_exc = None
        self.states = {"EXISTING1": "draft"}  # id -> published state, for the "is it still a draft?" read
        self.drafts: list = []                # what a lookup-by-URL finds
        for name, val in (("existing_urls", lambda key, space: self.urls), ("http", self.fake_http)):
            p = mock.patch.object(rm, name, val)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(rm.time, "sleep", mock.Mock())
        p.start()
        self.addCleanup(p.stop)

    def fake_http(self, method, url, key, body=None):
        if body is not None:
            assert body.get("published", "draft") == "draft"
        self.calls.append((method, url.split("?")[0], body, url))
        if "/content/" in url:  # GET lookups
            if "query.id=" in url:
                tid = url.split("query.id=")[1].split("&")[0]
                return {"results": [{"id": tid, "published": self.states[tid]}]} if tid in self.states else {"results": []}
            return {"results": self.drafts}
        if method == "POST":
            if self.post_exc:
                raise self.post_exc
            return self.post_reply
        if self.patch_exc:
            raise self.patch_exc
        return {"id": "ok"}

    def writes(self, method):
        return [c for c in self.calls if c[0] == method]

    def seed(self, rows):
        subprocess.run([sys.executable, str(ROOT / "scripts" / "manifest.py"), "seed", "--file", "-"], input=json.dumps(rows),
                       text=True, check=True, capture_output=True, env=os.environ)

    def status(self, sid):
        return self.rm.manifest("get", sid)

    def run_main(self, *args):
        old = sys.argv
        sys.argv = ["run_migration.py", *args]
        try:
            return self.rm.main()
        finally:
            sys.argv = old


@unittest.skipUnless(HAVE_EXPORT, "exports/ not present")
class Runner(RunnerBase):
    def setUp(self):
        super().setUp()
        self.seed(ROWS)

    def test_dry_run_writes_nothing_and_releases_claims(self):
        self.assertEqual(self.run_main("--content-type", "page"), 0)
        self.assertEqual(self.writes("POST") + self.writes("PATCH"), [])
        self.assertEqual(self.status("seed-page-landing")["status"], "pending")

    def test_apply_creates_draft_and_marks_children(self):
        self.assertEqual(self.run_main("--apply", "--content-type", "page"), 0)
        posts = self.writes("POST")
        self.assertEqual(len(posts), 1)
        self.assertTrue(posts[0][1].endswith("/write/page"))
        body = posts[0][2]
        self.assertEqual(body["published"], "draft")
        self.assertEqual(body["data"]["url"], "/seed-landing")
        self.assertEqual([b["component"]["name"] for b in body["data"]["blocks"]], ["HeroCentered", "HeroSplit", "ProductGridSelected", "FaqList"])
        page = self.status("seed-page-landing")
        self.assertEqual((page["status"], page["target_id"]), ("migrated", "NEW123"))
        for kid in ("seed-hero-field-report", "seed-faq", "seed-faq-item-1"):
            self.assertEqual(self.status(kid)["status"], "migrated")
        self.assertTrue(Path(os.environ["WRITES_LOG"]).read_text().count("seed-page-landing"))

    def test_url_collision_goes_to_needs_human_without_writing(self):
        self.urls["/seed-landing"] = {"id": "someone-else", "model": "page"}
        self.run_main("--apply", "--content-type", "page")
        self.assertEqual(self.writes("POST") + self.writes("PATCH"), [])
        row = self.status("seed-page-landing")
        self.assertEqual(row["status"], "needs_human")
        self.assertIn("already exists", row["notes"])

    def test_clearly_rejected_create_marks_failed(self):
        self.post_exc = self.rm.HttpError("HTTP 400 POST x: b'bad'")
        self.run_main("--apply", "--content-type", "page")
        self.assertEqual(self.status("seed-page-landing")["status"], "failed")

    def _make_update_row(self):
        self.rm.manifest("claim", "--status", "pending", "--limit", "1", "--content-type", "page")
        self.rm.manifest("set", "seed-page-landing", "--status", "failed", "--target", "EXISTING1", "--notes", "x")
        self.urls["/seed-landing"] = {"id": "EXISTING1", "model": "page"}  # its own draft: not a collision

    def test_update_patches_a_draft_and_never_sends_published(self):
        self._make_update_row()
        self.run_main("--apply", "--content-type", "page")
        patches = self.writes("PATCH")
        self.assertEqual(len(patches), 1)
        self.assertTrue(patches[0][1].endswith("/write/page/EXISTING1"))
        self.assertNotIn("published", patches[0][2])
        self.assertEqual(self.status("seed-page-landing")["status"], "migrated")

    def test_update_refuses_a_target_that_is_published_archived_or_missing(self):
        for state in ("published", "archived", None):
            with self.subTest(state=state):
                self.calls.clear()
                self.setUp()  # fresh manifest per subtest
                self._make_update_row()
                if state is None:
                    self.states.pop("EXISTING1")
                else:
                    self.states["EXISTING1"] = state
                self.run_main("--apply", "--content-type", "page")
                self.assertEqual(self.writes("PATCH"), [], "must never edit live content")
                row = self.status("seed-page-landing")
                self.assertEqual(row["status"], "needs_human")
                self.assertIn(state or "missing", row["notes"])

    # -- crash windows: a create whose outcome is unknown must never be blindly retried or recreated
    def test_unknown_create_outcome_adopts_the_one_draft_found_by_url(self):
        self.post_exc = self.rm.AmbiguousWriteError("timed out")
        self.drafts = [{"id": "FOUND1", "name": "Seed Landing", "published": "draft", "data": {"url": "/seed-landing"}}]
        self.run_main("--apply", "--content-type", "page")
        self.assertEqual(len(self.writes("POST")), 1)  # never retried
        row = self.status("seed-page-landing")
        self.assertEqual((row["status"], row["target_id"]), ("migrated", "FOUND1"))
        self.assertIn("adopted", row["notes"])

    def test_unknown_create_outcome_with_nothing_found_stops_for_a_human(self):
        self.post_exc = self.rm.AmbiguousWriteError("connection reset")
        self.run_main("--apply", "--content-type", "page")
        self.assertEqual(len(self.writes("POST")), 1)
        row = self.status("seed-page-landing")
        self.assertEqual(row["status"], "needs_human")
        self.assertIn("outcome unknown", row["notes"])
        self.assertFalse(row["target_id"])

    def test_unknown_create_outcome_with_several_matches_is_not_guessed(self):
        self.post_exc = self.rm.AmbiguousWriteError("5xx")
        d = {"name": "Seed Landing", "published": "draft", "data": {"url": "/seed-landing"}}
        self.drafts = [{"id": "A", **d}, {"id": "B", **d}]
        self.run_main("--apply", "--content-type", "page")
        self.assertEqual(self.status("seed-page-landing")["status"], "needs_human")

    def test_a_reply_without_an_id_is_an_unknown_outcome_not_a_crash(self):
        self.post_reply = {}
        self.run_main("--apply", "--content-type", "page")
        self.assertEqual(self.status("seed-page-landing")["status"], "needs_human")

    def test_manifest_failure_after_a_write_stops_the_batch_and_leaves_a_breadcrumb(self):
        real = self.rm.manifest

        def flaky(*args):
            if args[:1] == ("set",) and "migrated" in args:
                raise RuntimeError("database is locked")
            return real(*args)
        with mock.patch.object(self.rm, "manifest", flaky), self.assertRaises(RuntimeError):
            self.run_main("--apply", "--content-type", "page")
        crumb = [json.loads(ln) for ln in Path(os.environ["RUNNER_UNSAVED_LOG"]).read_text().splitlines()]
        self.assertEqual((crumb[0]["kind"], crumb[0]["source_id"], crumb[0]["target_id"]), ("set", "seed-page-landing", "NEW123"))
        self.assertEqual(self.status("seed-page-landing")["status"], "pending")  # released, not left stuck in_progress

    def test_audit_log_failure_is_reported_but_the_state_is_still_saved(self):
        real = self.rm.manifest

        def flaky(*args):
            if args[:1] == ("log-write",):
                raise RuntimeError("disk full")
            return real(*args)
        with mock.patch.object(self.rm, "manifest", flaky):
            self.assertEqual(self.run_main("--apply", "--content-type", "page"), 0)
        self.assertEqual(self.status("seed-page-landing")["target_id"], "NEW123")
        self.assertEqual(json.loads(Path(os.environ["RUNNER_UNSAVED_LOG"]).read_text().splitlines()[0])["kind"], "log-write")

    def test_a_claim_lost_to_another_worker_is_not_overwritten(self):
        real = self.rm.manifest

        def steal(*args):
            if args[:1] == ("set",) and "needs_human" in args:  # someone else changed the row first
                real("set", "seed-page-landing", "--status", "pending", "--expect-status", "in_progress")
            return real(*args)
        self.urls["/seed-landing"] = {"id": "x", "model": "page"}  # force a needs_human write
        with mock.patch.object(self.rm, "manifest", steal):
            self.run_main("--apply", "--content-type", "page")
        self.assertEqual(self.status("seed-page-landing")["status"], "pending")  # our late write was refused


class Synthetic(RunnerBase):
    """Runs the real main() against small synthetic exports (EXPORTS_DIR), so batch-level behaviour can be tested."""

    _exports: tempfile.TemporaryDirectory

    @classmethod
    def setUpClass(cls):
        cls._exports = tempfile.TemporaryDirectory()
        cls.exports_dir = Path(cls._exports.name)
        entries = [page_entry("dup-a", "/dup", "A"), page_entry("dup-b", "/dup", "B"), page_entry("no-slug", None, "C"), page_entry("blank-slug", "   ", "D"),
                   page_entry("fr-title", "/fr-only", {"fr": "seulement"}), page_entry("fine", "/fine", "F")]
        (cls.exports_dir / "entries.jsonl").write_text("\n".join(json.dumps(e) for e in entries) + "\n")

    @classmethod
    def tearDownClass(cls):
        cls._exports.cleanup()

    def setUp(self):
        super().setUp()
        self.seed([{"source_id": s, "content_type": "page"} for s in ("dup-a", "dup-b", "no-slug", "blank-slug", "fr-title", "fine")])

    @unittest.skipUnless(HAVE_MAPPING, "needs the local approved mapping")
    def test_batch_level_safety(self):
        self.run_main("--apply", "--content-type", "page", "--limit", "20")
        posted = {c[2]["data"]["url"] for c in self.writes("POST")}
        self.assertEqual(posted, {"/dup", "/fine"}, "exactly one of the two entries sharing a URL is created")
        st = {s: self.status(s) for s in ("dup-a", "dup-b", "no-slug", "blank-slug", "fr-title", "fine")}
        self.assertEqual(st["dup-a"]["status"], "migrated")
        self.assertEqual(st["dup-b"]["status"], "needs_human")  # the URL was taken earlier in the SAME batch
        self.assertIn("already exists", st["dup-b"]["notes"])
        for empty in ("no-slug", "blank-slug"):
            self.assertEqual(st[empty]["status"], "needs_human")
            self.assertIn("slug is empty", st[empty]["notes"])  # never defaults to the home URL
        self.assertEqual(st["fr-title"]["status"], "needs_human")  # no value in the configured locale: not the whole {"fr": ...} map
        self.assertEqual(st["fine"]["status"], "migrated")
        self.assertFalse([c for c in self.writes("POST") if "fr" in json.dumps(c[2]["data"]).replace("/fr-only", "")])


class HttpLayer(unittest.TestCase):
    """http(): classification of failures and which calls may be retried."""

    def setUp(self):
        import run_migration as rm
        self.rm = rm
        self.sleeps: list = []
        for obj, name, val in ((rm.time, "sleep", lambda s: self.sleeps.append(s)),):
            p = mock.patch.object(obj, name, val)
            p.start()
            self.addCleanup(p.stop)

    def run_http(self, method, outcomes, body=None):
        """outcomes: a list of exceptions to raise / bytes to return, consumed one per urlopen call."""
        calls = []

        def fake_urlopen(req, timeout=None):
            calls.append(timeout)
            o = outcomes[min(len(calls) - 1, len(outcomes) - 1)]
            if isinstance(o, Exception):
                raise o
            resp = mock.MagicMock()
            resp.read.return_value = o
            resp.__enter__.return_value = resp
            return resp
        with mock.patch.object(self.rm.urllib.request, "urlopen", fake_urlopen):
            try:
                result = self.rm.http(method, "https://builder.io/api/v1/write/page?x=1", "k", body)
                return result, None, calls
            except Exception as e:  # noqa: BLE001
                return None, e, calls

    @staticmethod
    def http_error(code):
        return urllib.error.HTTPError("https://x", code, "err", {}, io.BytesIO(b"body"))  # type: ignore[arg-type]

    def test_post_network_failures_are_unknown_outcomes_and_are_never_retried(self):
        for exc in (urllib.error.URLError("reset"), socket.timeout("slow"), ConnectionResetError("reset")):
            with self.subTest(exc=type(exc).__name__):
                _, err, calls = self.run_http("POST", [exc])
                self.assertIsInstance(err, self.rm.AmbiguousWriteError)
                self.assertEqual(len(calls), 1)

    def test_post_5xx_is_an_unknown_outcome_not_a_retry(self):
        _, err, calls = self.run_http("POST", [self.http_error(503), b"{}"])
        self.assertIsInstance(err, self.rm.AmbiguousWriteError)
        self.assertEqual(len(calls), 1)

    def test_post_429_is_retried_because_nothing_was_processed(self):
        result, err, calls = self.run_http("POST", [self.http_error(429), self.http_error(429), b'{"id": "A"}'])
        self.assertEqual((result, err, len(calls), self.sleeps), ({"id": "A"}, None, 3, [2, 5]))

    def test_post_4xx_is_a_clear_failure(self):
        _, err, calls = self.run_http("POST", [self.http_error(400)])
        self.assertIsInstance(err, self.rm.HttpError)
        self.assertNotIsInstance(err, self.rm.AmbiguousWriteError)

    def test_post_with_an_unreadable_success_body_is_an_unknown_outcome(self):
        _, err, _ = self.run_http("POST", [b"<html>not json</html>"])
        self.assertIsInstance(err, self.rm.AmbiguousWriteError)

    def test_get_and_patch_retry_on_5xx_and_network_errors(self):
        for method in ("GET", "PATCH"):
            with self.subTest(method=method):
                self.sleeps.clear()
                result, err, calls = self.run_http(method, [self.http_error(502), urllib.error.URLError("x"), b'{"ok": 1}'])
                self.assertEqual((result, err, len(calls)), ({"ok": 1}, None, 3))

    def test_retries_give_up_after_the_backoff_schedule(self):
        _, err, calls = self.run_http("GET", [self.http_error(500)])
        self.assertIsInstance(err, self.rm.HttpError)
        self.assertEqual(len(calls), len(self.rm.BACKOFF) + 1)
        self.assertEqual(self.sleeps, list(self.rm.BACKOFF))

    def test_every_call_has_a_timeout(self):
        _, _, calls = self.run_http("GET", [b"{}"])
        self.assertEqual(calls, [self.rm.HTTP_TIMEOUT])
        self.assertGreater(self.rm.HTTP_TIMEOUT, 0)

    def test_it_can_only_ever_read_create_or_update_as_a_draft(self):
        with self.assertRaises(AssertionError):
            self.rm.http("DELETE", "https://builder.io/x", "k")
        with self.assertRaises(AssertionError):
            self.rm.http("POST", "https://builder.io/x", "k", {"published": "published"})

    def test_find_draft_requires_one_exact_match(self):
        d = {"name": "N", "published": "draft", "data": {"url": "/x"}}
        cases = [([{"id": "1", **d}], "1"), ([], None), ([{"id": "1", **d}, {"id": "2", **d}], None),
                 ([{"id": "1", **dict(d, published="published")}], None), ([{"id": "1", **dict(d, name="other")}], None),
                 ([{"id": "1", **dict(d, data={"url": "/y"})}], None)]
        for results, expected in cases:
            with self.subTest(expected=expected, n=len(results)), mock.patch.object(self.rm, "http", lambda *a, results=results, **k: {"results": results}):
                self.assertEqual(self.rm.find_draft("k", "s", "page", "N", "/x"), expected)
        with mock.patch.object(self.rm, "http", mock.Mock(side_effect=self.rm.HttpError("down"))):
            self.assertIsNone(self.rm.find_draft("k", "s", "page", "N", "/x"))


if __name__ == "__main__":
    unittest.main()
