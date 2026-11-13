"""Tests for scripts/export_contentful.py: the API client (urlopen mocked) and the staged, crash-safe export. No network.

Run: python3 -m unittest scripts/test_export_contentful.py -v
"""
import contextlib
import io
import json
import os
import socket
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import export_contentful as ex  # noqa: E402

TYPES = [{"sys": {"id": "page"}, "name": "page", "fields": [{"id": "title", "name": "Title", "type": "Symbol", "localized": True}]}]
ENTRIES = [{"sys": {"id": f"e{i}", "contentType": {"sys": {"id": "page"}}}, "fields": {"title": {"en-US": f"T{i}"}}} for i in range(3)]
ASSETS = [{"sys": {"id": "a1"}, "fields": {}}]


class FakeApi:
    instances: list = []

    def __init__(self, host, space, env, token):
        self.host, self.space, self.env = host, space, env
        FakeApi.instances.append(self)

    def get(self, path, **params):
        if path == "/entries":
            return {"total": len(ENTRIES)}
        if path == "/assets":
            return {"total": len(ASSETS)}
        if path == "/locales":
            return {"items": [{"code": "en-US"}]}
        raise AssertionError(path)

    def paged(self, path, **params):
        data: dict = {"/content_types": TYPES, "/entries": ENTRIES, "/assets": ASSETS}
        yield from data[path]


class StagedExport(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = Path(self.tmp.name) / "exports"
        env = mock.patch.dict(os.environ, {"CONTENTFUL_SPACE_ID": "sp", "CONTENTFUL_SOURCE_ENVIRONMENT": "master", "CONTENTFUL_DELIVERY_TOKEN": "d-tok"})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("CONTENTFUL_PREVIEW_TOKEN", None)
        for name, val in (("Api", FakeApi), ("OUT", self.out), ("load_env", lambda: None)):
            p = mock.patch.object(ex, name, val)
            p.start()
            self.addCleanup(p.stop)

    def run_main(self, *args):
        old = sys.argv
        sys.argv = ["export_contentful.py", *args]
        out, err = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = ex.main()
        finally:
            sys.argv = old
        return code, out.getvalue(), err.getvalue()

    def test_dry_run_writes_nothing(self):
        code, out, _ = self.run_main()
        self.assertEqual(code, 0)
        self.assertFalse(self.out.exists())
        self.assertIn("Dry run only", out)

    def test_export_writes_every_file_and_leaves_no_temporary_files(self):
        self.assertEqual(self.run_main("--export")[0], 0)
        names = sorted(p.name for p in self.out.iterdir())
        self.assertEqual(names, ["assets.jsonl", "content_types.json", "entries.jsonl", "inventory.json", "locales.json"])
        self.assertEqual(len(self.out.joinpath("entries.jsonl").read_text().splitlines()), 3)
        self.assertEqual(json.loads(self.out.joinpath("inventory.json").read_text())["totals"]["entries"], 3)

    def test_inventory_is_swapped_in_last_as_the_completion_marker(self):
        order: list = []
        real = os.replace
        def recording_replace(src, dst):
            order.append(Path(dst).name)
            real(src, dst)
        with mock.patch.object(ex.os, "replace", recording_replace):
            self.run_main("--export")
        self.assertEqual(order[-1], "inventory.json")
        self.assertEqual(sorted(order), ["assets.jsonl", "content_types.json", "entries.jsonl", "inventory.json", "locales.json"])

    def test_a_failure_while_staging_leaves_the_previous_export_untouched(self):
        self.run_main("--export")
        before = {p.name: p.read_text() for p in self.out.iterdir()}
        calls = []
        real_stage = ex.stage

        def flaky(final, write):
            calls.append(final.name)
            if len(calls) == 3:
                raise OSError("disk full")
            return real_stage(final, write)
        with mock.patch.object(ex, "stage", flaky), self.assertRaises(OSError):
            self.run_main("--export")
        self.assertEqual({p.name: p.read_text() for p in self.out.iterdir()}, before, "old files intact, no .tmp left behind")

    def test_a_failure_while_fetching_never_touches_existing_files(self):
        self.run_main("--export")
        before = (self.out / "entries.jsonl").read_text()

        class Boom(FakeApi):
            def paged(self, path, **params):
                if path == "/assets":
                    raise SystemExit("network error on GET /assets")
                yield from super().paged(path, **params)
        with mock.patch.object(ex, "Api", Boom), self.assertRaises(SystemExit):
            self.run_main("--export")
        self.assertEqual((self.out / "entries.jsonl").read_text(), before)

    def test_the_delivery_flag_forces_published_only_even_with_a_preview_token(self):
        FakeApi.instances.clear()
        with mock.patch.dict(os.environ, {"CONTENTFUL_PREVIEW_TOKEN": "p-tok"}):
            self.run_main("--delivery")
        self.assertTrue(FakeApi.instances)
        self.assertIn("preview.contentful.com", {i.host for i in FakeApi.instances})  # it still counts drafts for the dry-run delta
        # ...but the export source line says what it used
        with mock.patch.dict(os.environ, {"CONTENTFUL_PREVIEW_TOKEN": "p-tok"}):
            _, out, _ = self.run_main("--delivery")
        self.assertIn("delivery (published only)", out)

    def test_missing_configuration_is_reported_not_attempted(self):
        with mock.patch.dict(os.environ):
            os.environ.pop("CONTENTFUL_DELIVERY_TOKEN")
            code, _, err = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("Missing", err)


class ApiClient(unittest.TestCase):
    def setUp(self):
        self.sleeps: list = []
        p = mock.patch.object(ex.time, "sleep", lambda s: self.sleeps.append(s))
        p.start()
        self.addCleanup(p.stop)

    def run_get(self, outcomes):
        calls = []

        def fake_urlopen(req, timeout=None):
            calls.append((req.full_url, timeout))
            o = outcomes[min(len(calls) - 1, len(outcomes) - 1)]
            if isinstance(o, Exception):
                raise o
            resp = mock.MagicMock()
            resp.read.return_value = o
            resp.__enter__.return_value = resp
            return resp
        api = ex.Api("cdn.contentful.com", "my space/../x", "master", "tok")
        with mock.patch.object(ex.urllib.request, "urlopen", fake_urlopen):
            try:
                return api.get("/entries", limit=1), None, calls
            except BaseException as e:  # noqa: BLE001  (SystemExit is how the client gives up)
                return None, e, calls

    @staticmethod
    def http_error(code):
        return urllib.error.HTTPError("https://x", code, "err", {}, io.BytesIO(b"body"))  # type: ignore[arg-type]

    def test_space_and_environment_ids_are_url_quoted(self):
        _, _, calls = self.run_get([b"{}"])
        self.assertIn("/spaces/my%20space%2F..%2Fx/environments/master/", calls[0][0])

    def test_every_request_has_a_timeout(self):
        _, _, calls = self.run_get([b"{}"])
        self.assertEqual(calls[0][1], ex.Api.TIMEOUT)

    def test_429_5xx_and_network_errors_are_retried_with_backoff(self):
        result, err, calls = self.run_get([self.http_error(429), self.http_error(503), urllib.error.URLError("x"), socket.timeout("slow"), b'{"ok": 1}'])
        self.assertEqual((result, err, len(calls), self.sleeps), ({"ok": 1}, None, 5, [2, 5, 15, 60]))

    def test_a_client_error_is_not_retried(self):
        _, err, calls = self.run_get([self.http_error(401)])
        self.assertIsInstance(err, SystemExit)
        self.assertIn("HTTP 401", str(err))
        self.assertEqual(len(calls), 1)

    def test_it_gives_up_with_a_clear_message_after_the_schedule(self):
        _, err, calls = self.run_get([urllib.error.URLError("down")])
        self.assertIsInstance(err, SystemExit)
        self.assertIn("network error", str(err))
        self.assertEqual(len(calls), len(ex.Api.BACKOFF) + 1)

    def test_an_unreadable_reply_is_a_clear_failure(self):
        _, err, _ = self.run_get([b"<html>"])
        self.assertIsInstance(err, SystemExit)
        self.assertIn("unreadable", str(err))

    def test_paged_walks_every_page_by_skip(self):
        api = ex.Api("cdn.contentful.com", "s", "e", "t")
        seen = []

        def fake_get(path, **params):
            seen.append(params["skip"])
            n = min(ex.PAGE, 2500 - params["skip"])
            return {"items": [{"i": params["skip"] + k} for k in range(n)], "total": 2500}
        with mock.patch.object(api, "get", fake_get):
            items = list(api.paged("/entries"))
        self.assertEqual((len(items), seen), (2500, [0, 1000, 2000]))


if __name__ == "__main__":
    unittest.main()
