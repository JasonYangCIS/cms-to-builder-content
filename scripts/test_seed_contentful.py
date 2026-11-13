"""Tests for scripts/seed_contentful.py (the only script that can write to Contentful): target selection, dry run, draft-only,
idempotency, publish scope. The Contentful API is MOCKED; nothing is sent anywhere.

Run: python3 -m unittest scripts/test_seed_contentful.py -v
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import seed_contentful as sc  # noqa: E402

TOKEN = "CFPAT-super-secret-token-value"
SEED = json.loads((ROOT / "scripts" / "seed_data.json").read_text())
SEED_ENTRY_IDS = [e["id"] for e in SEED["entries"]]


class Seed(unittest.TestCase):
    def setUp(self):
        self.real_call = sc.call  # kept so a test can check the real request function directly
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        d = Path(self.tmp.name)
        env = mock.patch.dict(os.environ, {"CONTENTFUL_MANAGEMENT_TOKEN": TOKEN, "CONTENTFUL_SEED_SPACE": "sandbox-space",
                                           "CONTENTFUL_SEED_ENVIRONMENT": "seed-env"})
        env.start()
        self.addCleanup(env.stop)
        (d / "mapping.json").write_text(json.dumps({"source": {"space": "source-space", "environment": "master"}}))
        self.calls: list = []
        self.existing: set = set()  # api paths that already exist (GET -> 200)
        for name, val in (("LOG", d / "seed.jsonl"), ("MAPPING", d / "mapping.json"), ("call", self.fake_call), ("load_env", lambda: None)):
            p = mock.patch.object(sc, name, val)
            p.start()
            self.addCleanup(p.stop)

    def fake_call(self, method, path, token, body=None, headers=None):
        assert method in ("GET", "PUT"), "the seed script must never use another method"
        assert token == TOKEN
        self.calls.append((method, path, body, headers or {}))
        if method == "GET":
            if path not in self.existing:
                return 404, {}
            # an existing entry is an unpublished draft (version 3, never published); a content type is published and unchanged
            return (200, {"sys": {"version": 3}}) if "/entries/" in path else (200, {"sys": {"version": 5, "publishedVersion": 4}})
        return 201, {"sys": {"version": 1}}

    def run_main(self, *args):
        old = sys.argv
        sys.argv = ["seed_contentful.py", *args]
        out, err = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = sc.main()
        finally:
            sys.argv = old
        return code, out.getvalue(), err.getvalue()

    def puts(self):
        return [c for c in self.calls if c[0] == "PUT"]

    # ---- where it is allowed to write
    def test_the_target_must_be_named_explicitly(self):
        for missing in ("CONTENTFUL_SEED_SPACE", "CONTENTFUL_SEED_ENVIRONMENT"):
            with self.subTest(missing=missing), mock.patch.dict(os.environ):
                os.environ.pop(missing)
                code, _, err = self.run_main("--apply")
                self.assertEqual(code, 1)
                self.assertIn("explicitly", err)
                self.assertEqual(self.calls, [], "must not even read before the target is named")

    def test_the_old_implicit_defaults_are_gone(self):
        with mock.patch.dict(os.environ, {"CONTENTFUL_SPACE_ID": "some-space"}):  # the old variable no longer selects a target
            os.environ.pop("CONTENTFUL_SEED_SPACE")
            self.assertEqual(self.run_main("--apply")[0], 1)
            self.assertEqual(self.calls, [])

    def test_it_refuses_the_migration_source_unless_forced(self):
        with mock.patch.dict(os.environ, {"CONTENTFUL_SEED_SPACE": "source-space", "CONTENTFUL_SEED_ENVIRONMENT": "master"}):
            code, _, err = self.run_main("--apply")
            self.assertEqual(code, 1)
            self.assertIn("SOURCE", err)
            self.assertEqual(self.calls, [])
            self.assertEqual(self.run_main("--apply", "--allow-source")[0], 0)  # an explicit, informed override
            self.assertTrue(self.puts())

    def test_the_same_space_in_a_different_environment_is_allowed(self):
        with mock.patch.dict(os.environ, {"CONTENTFUL_SEED_SPACE": "source-space", "CONTENTFUL_SEED_ENVIRONMENT": "sandbox-env"}):
            self.assertEqual(self.run_main()[0], 0)

    def test_a_placeholder_space_is_rejected(self):
        with mock.patch.dict(os.environ, {"CONTENTFUL_SEED_SPACE": "YOUR_SANDBOX_SPACE"}):
            self.assertEqual(self.run_main("--apply")[0], 1)
            self.assertEqual(self.calls, [])

    def test_a_missing_token_is_rejected(self):
        with mock.patch.dict(os.environ):
            os.environ.pop("CONTENTFUL_MANAGEMENT_TOKEN")
            self.assertEqual(self.run_main("--apply")[0], 1)
            self.assertEqual(self.calls, [])

    # ---- what it does
    def test_dry_run_only_reads_and_prints_the_target(self):
        code, out, _ = self.run_main()
        self.assertEqual(code, 0)
        self.assertEqual(self.puts(), [])
        self.assertTrue(all(c[0] == "GET" for c in self.calls))
        self.assertIn("space=sandbox-space env=seed-env", out)
        self.assertIn("DRY RUN", out)
        self.assertFalse(Path(sc.LOG).exists())

    def test_every_request_goes_to_the_named_target_only(self):
        self.run_main("--apply")
        for _, path, _, _ in self.calls:
            self.assertTrue(path.startswith("/spaces/sandbox-space/environments/seed-env/"), path)

    def test_apply_creates_types_then_publishes_them_then_creates_entries_as_drafts(self):
        self.run_main("--apply")
        paths = [c[1] for c in self.puts()]
        type_puts = [p for p in paths if "/content_types/" in p]
        entry_puts = [p for p in paths if "/entries/" in p]
        self.assertEqual(len(entry_puts), len(SEED["entries"]))
        self.assertTrue(all(not p.endswith("/published") for p in entry_puts), "entries are never published without --publish")
        self.assertTrue(any(p.endswith("/published") for p in type_puts), "content types must be published before entries can use them")
        self.assertLess(paths.index(next(p for p in type_puts if p.endswith("/published"))), paths.index(entry_puts[0]))
        for _, path, _body, headers in self.puts():
            if "/entries/" in path:
                self.assertIn("X-Contentful-Content-Type", headers)
        self.assertEqual(len(Path(sc.LOG).read_text().splitlines()), len(SEED["content_types"]) + len(SEED["entries"]))

    def test_existing_items_are_skipped_never_modified(self):
        base = "/spaces/sandbox-space/environments/seed-env"
        self.existing = {f"{base}/content_types/{ct['id']}" for ct in SEED["content_types"]} | {f"{base}/entries/{i}" for i in SEED_ENTRY_IDS}
        code, out, _ = self.run_main("--apply")
        self.assertEqual((code, self.puts()), (0, []))
        self.assertIn("skip", out)

    def test_publish_only_touches_seed_entries_in_dependency_order(self):
        base = "/spaces/sandbox-space/environments/seed-env"
        self.existing = {f"{base}/entries/{i}" for i in SEED_ENTRY_IDS}
        self.run_main("--publish", "--apply")
        published = [c[1].split("/entries/")[1].split("/")[0] for c in self.puts()]
        self.assertEqual(published, [i for i in SEED_ENTRY_IDS if i.startswith("seed-")])
        self.assertTrue(all(c[1].endswith("/published") and c[3].get("X-Contentful-Version") == "3" for c in self.puts()))

    def test_publish_without_apply_is_a_dry_run(self):
        base = "/spaces/sandbox-space/environments/seed-env"
        self.existing = {f"{base}/entries/{i}" for i in SEED_ENTRY_IDS}
        self.run_main("--publish")
        self.assertEqual(self.puts(), [])

    def test_the_token_is_never_printed(self):
        code, out, err = self.run_main("--apply")
        self.assertNotIn(TOKEN, out + err)
        self.assertNotIn(TOKEN, Path(sc.LOG).read_text())

    def test_the_real_request_function_refuses_every_method_but_get_and_put(self):
        for method in ("DELETE", "POST", "PATCH"):
            with self.assertRaises(AssertionError):
                self.real_call(method, "/spaces/x/environments/y/entries/z", TOKEN)  # refused before any network access

    def test_entry_conversion_links_and_rich_text(self):
        body = sc.entry_body({"fields": {"items": ["a", "b"], "answer": "Hi", "title": "T"}}, "en-US")["fields"]
        self.assertEqual(body["items"]["en-US"][0], {"sys": {"type": "Link", "linkType": "Entry", "id": "a"}})
        self.assertEqual(body["answer"]["en-US"]["nodeType"], "document")
        self.assertEqual(body["title"], {"en-US": "T"})


if __name__ == "__main__":
    unittest.main()
