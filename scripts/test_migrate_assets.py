"""Tests for scripts/migrate_assets.py: which URLs may be downloaded, how upload outcomes are handled, idempotency. No network.

Run: python3 -m unittest scripts/test_migrate_assets.py -v
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
import migrate_assets as ma  # noqa: E402

GOOD_URL = "//images.ctfassets.net/space/asset1/hash/pic.jpg"


def asset(aid, url=GOOD_URL):
    return {"sys": {"id": aid}, "fields": {"title": {"en-US": "Pic"}, "description": {"en-US": "Alt text"},
                                           "file": {"en-US": {"url": url, "fileName": f"{aid}.jpg", "contentType": "image/jpeg"}}}}


def entry(eid, asset_ids):
    links = {f"img{i}": {"en-US": {"sys": {"type": "Link", "linkType": "Asset", "id": a}}} for i, a in enumerate(asset_ids)}
    return {"sys": {"id": eid}, "fields": links}


class UrlPolicy(unittest.TestCase):
    def test_only_https_on_the_contentful_asset_cdn_is_allowed(self):
        for url in ("https://images.ctfassets.net/a.jpg", "https://assets.ctfassets.net/a.jpg", "https://downloads.eu.ctfassets.net/a.pdf", "HTTPS://Images.CtfAssets.Net/a"):
            with self.subTest(url=url):
                self.assertEqual(ma.check_asset_url(url), url)

    def test_everything_else_is_refused(self):
        for url in ("http://images.ctfassets.net/a.jpg", "file:///etc/passwd", "ftp://images.ctfassets.net/a", "https://evil.com/images.ctfassets.net/a",
                    "https://images.ctfassets.net.evil.com/a", "https://evilctfassets.net/a", "https://ctfassets.net/a", "https://localhost/a",
                    "https://127.0.0.1/a", "//images.ctfassets.net/a", "", "javascript:alert(1)"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                ma.check_asset_url(url)


class Migrate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        d = Path(self.tmp.name)
        (d / "exports").mkdir()
        self.set_export([asset("a1"), asset("a2")], [entry("e1", ["a1"])])  # a2 is exported but unreferenced
        env = mock.patch.dict(os.environ, {"BUILDER_CONTENT_MCP_PK": "builder-secret-key", "MANIFEST_DB": str(d / "m.db"), "WRITES_LOG": str(d / "w.jsonl")})
        env.start()
        self.addCleanup(env.stop)
        patches = (mock.patch.object(ma, "EXPORTS", d / "exports"), mock.patch.object(ma, "MAP", d / "exports" / "asset_map.json"),
                   mock.patch.object(ma, "load_env", mock.Mock()))
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.requests: list = []
        self.upload_outcome = json.dumps({"status": 200, "url": "https://cdn.builder.io/api/v1/image/assets%2Fx%2Fabc"}).encode()
        opener = mock.patch.object(ma.urllib.request, "urlopen", self.fake_urlopen)
        opener.start()
        self.addCleanup(opener.stop)

    def set_export(self, assets, entries):
        d = Path(self.tmp.name) / "exports"
        (d / "assets.jsonl").write_text("\n".join(json.dumps(a) for a in assets) + "\n")
        (d / "entries.jsonl").write_text("\n".join(json.dumps(e) for e in entries) + "\n")

    def fake_urlopen(self, req, timeout=None):
        url = req if isinstance(req, str) else req.full_url
        self.requests.append((url, timeout, None if isinstance(req, str) else req.get_method()))
        out = b"IMAGEBYTES" if isinstance(req, str) else self.upload_outcome
        if isinstance(out, Exception):
            raise out
        resp = mock.MagicMock()
        resp.read.return_value = out
        resp.__enter__.return_value = resp
        return resp

    def run_main(self, *args):
        old = sys.argv
        sys.argv = ["migrate_assets.py", *args]
        out, err = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = ma.main()
        finally:
            sys.argv = old
        return code, out.getvalue(), err.getvalue()

    def uploads(self):
        return [r for r in self.requests if r[2] == "POST"]

    def test_dry_run_downloads_but_never_uploads(self):
        code, out, _ = self.run_main()
        self.assertEqual((code, self.uploads()), (0, []))
        self.assertIn("1 referenced asset(s) of 2", out)  # the unreferenced one is ignored
        self.assertFalse(ma.MAP.exists())

    def test_apply_uploads_records_the_map_and_audits(self):
        code, out, err = self.run_main("--apply")
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.uploads()), 1)
        amap = json.loads(ma.MAP.read_text())
        self.assertEqual(list(amap), ["a1"])
        self.assertTrue(amap["a1"]["builder_url"].startswith("https://cdn.builder.io/"))
        self.assertIn("upload-asset", (Path(os.environ["WRITES_LOG"])).read_text())

    def test_a_second_run_skips_what_is_already_uploaded(self):
        self.run_main("--apply")
        self.requests.clear()
        code, out, _ = self.run_main("--apply")
        self.assertEqual((code, self.uploads()), (0, []))
        self.assertIn("skip", out)

    def test_requests_have_timeouts_and_the_key_is_never_printed(self):
        code, out, err = self.run_main("--apply")
        self.assertTrue(all(t for _, t, _ in self.requests))
        self.assertNotIn("builder-secret-key", out + err)

    def test_a_non_allowlisted_source_url_is_refused_without_any_request(self):
        for bad in ("file:///etc/passwd", "https://evil.example/pic.jpg", "http://images.ctfassets.net/pic.jpg"):
            with self.subTest(url=bad):
                self.requests.clear()
                self.set_export([asset("a1", bad)], [entry("e1", ["a1"])])
                code, _, err = self.run_main("--apply")
                self.assertEqual(code, 1)
                self.assertIn("refusing to download", err)
                self.assertEqual(self.requests, [], "no request may be made for a refused URL")

    def test_a_server_error_on_upload_is_an_unknown_outcome_with_advice(self):
        self.upload_outcome = urllib.error.HTTPError("https://builder.io", 503, "x", {}, io.BytesIO(b"busy"))  # type: ignore[assignment,arg-type]
        code, _, err = self.run_main("--apply")
        self.assertEqual(code, 1)
        self.assertIn("outcome unknown", err)
        self.assertIn("Asset Library", err)
        self.assertEqual(len(self.uploads()), 1, "never retried")
        self.assertFalse(ma.MAP.exists())

    def test_a_client_error_on_upload_is_a_clear_rejection(self):
        self.upload_outcome = urllib.error.HTTPError("https://builder.io", 401, "x", {}, io.BytesIO(b"no"))  # type: ignore[assignment,arg-type]
        code, _, err = self.run_main("--apply")
        self.assertEqual(code, 1)
        self.assertIn("rejected", err)
        self.assertNotIn("Asset Library", err)

    def test_timeouts_and_resets_on_upload_are_unknown_outcomes(self):
        for exc in (socket.timeout("slow"), urllib.error.URLError("reset"), ConnectionResetError("reset")):
            with self.subTest(exc=type(exc).__name__):
                self.upload_outcome = exc  # type: ignore[assignment]
                code, _, err = self.run_main("--apply")
                self.assertEqual(code, 1)
                self.assertIn("outcome unknown", err)

    def test_a_reply_without_a_url_is_an_error_not_a_crash(self):
        self.upload_outcome = b'{"status": 200}'
        code, _, err = self.run_main("--apply")
        self.assertEqual(code, 1)
        self.assertIn("no url", err)

    def test_an_audit_failure_warns_but_keeps_the_recorded_upload(self):
        with mock.patch.object(ma.mf, "run", side_effect=RuntimeError("disk full")):
            code, _, err = self.run_main("--apply")
        self.assertEqual(code, 0)
        self.assertIn("audit line failed", err)
        self.assertIn("a1", json.loads(ma.MAP.read_text()))

    def test_a_referenced_but_missing_asset_stops_the_run(self):
        self.set_export([asset("other")], [entry("e1", ["a1"])])
        code, _, err = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("missing from exports", err)


if __name__ == "__main__":
    unittest.main()
