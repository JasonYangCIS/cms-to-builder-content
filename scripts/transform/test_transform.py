"""Golden tests for the deterministic transformers. Run: python3 -m unittest discover -s scripts/transform -v"""
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

import localeval  # noqa: E402
import refs  # noqa: E402
import richtext  # noqa: E402
import slug  # noqa: E402


def doc(*blocks):
    return {"nodeType": "document", "data": {}, "content": list(blocks)}


def para(*inline):
    return {"nodeType": "paragraph", "data": {}, "content": list(inline)}


def text(v, *marks):
    return {"nodeType": "text", "value": v, "marks": [{"type": m} for m in marks], "data": {}}


class RichText(unittest.TestCase):
    def test_pilot_faq_answer(self):  # the exact shape the pilot seed used
        out, exc = richtext.convert(doc(para(text("Orders ship within two business days."))))
        self.assertEqual(out, "<p>Orders ship within two business days.</p>")
        self.assertEqual(exc, [])

    def test_marks_escape_and_link(self):
        d = doc(para(text("a < b ", "bold"), {"nodeType": "hyperlink", "data": {"uri": "https://x.io/?a=1&b=2"},
                                               "content": [text("link")]}))
        out, exc = richtext.convert(d)
        self.assertEqual(out, '<p><strong>a &lt; b </strong><a href="https://x.io/?a=1&amp;b=2">link</a></p>')
        self.assertEqual(exc, [])

    def test_lists(self):
        li = lambda t: {"nodeType": "list-item", "data": {}, "content": [para(text(t))]}
        d = doc({"nodeType": "unordered-list", "data": {}, "content": [li("a"), li("b")]})
        self.assertEqual(richtext.convert(d)[0], "<ul><li><p>a</p></li><li><p>b</p></li></ul>")

    def test_exceptions_not_guessed(self):
        d = doc({"nodeType": "embedded-entry-block", "data": {"target": {"sys": {"id": "e1"}}}, "content": []},
                para(text("x", "superscript")))
        out, exc = richtext.convert(d)
        reasons = " | ".join(e["reason"] for e in exc)
        self.assertIn("embedded-entry-block", reasons)
        self.assertIn("superscript", reasons)
        self.assertEqual(exc[0]["target"], "e1")

    def test_empty_and_invalid(self):
        self.assertEqual(richtext.convert(None), ("", []))
        self.assertEqual(richtext.convert("<p>x</p>")[1][0]["reason"], "not a Contentful rich-text document")


class Slug(unittest.TestCase):
    EXISTING = ["/", "/schedule", "/home-contentful-migration"]

    def test_free(self):
        self.assertEqual(slug.resolve("/seed-landing", None, self.EXISTING)[0], {"url": "/seed-landing", "status": "free"})

    def test_normalizes(self):
        self.assertEqual(slug.resolve("about//us/", None, [])[0]["url"], "/about/us")

    def test_collision_not_overwritten(self):
        r, exc = slug.resolve("/schedule", None, self.EXISTING)
        self.assertEqual(r["status"], "collision")
        self.assertEqual(len(exc), 1)

    def test_home_rewrite(self):
        r, exc = slug.resolve("/", {"/": "/home-contentful-migration"}, ["/"], own_url="/home-contentful-migration")
        self.assertEqual(r, {"url": "/home-contentful-migration", "status": "own"})
        self.assertEqual(exc, [])
        r, exc = slug.resolve("/", {"/": "/home-contentful-migration"}, self.EXISTING)
        self.assertEqual(r["status"], "rewritten-collision")

    def test_root_unmapped_collides(self):
        self.assertEqual(slug.resolve("/", None, ["/"])[0]["status"], "collision")


class RichTextLinks(unittest.TestCase):
    """CMS content must not be able to put javascript:/data: links into published pages."""

    def link(self, uri):
        d = doc(para({"nodeType": "hyperlink", "data": {"uri": uri}, "content": [text("click")]}))
        return richtext.convert(d)

    def test_safe_links_are_kept(self):
        for uri in ("https://x.io/a?b=1&c=2", "http://x.io", "mailto:a@b.io", "tel:+15551234", "/relative/path", "#section", "page.html", "  https://x.io  "):
            with self.subTest(uri=uri):
                out, exc = self.link(uri)
                self.assertIn("<a href=", out)
                self.assertEqual(exc, [])

    def test_unsafe_links_are_dropped_with_the_text_kept_and_reported(self):
        for uri in ("javascript:alert(1)", "JaVaScRiPt:alert(1)", " javascript:alert(1)", "java\tscript:alert(1)", "java\nscript:alert(1)",
                    "\x01javascript:alert(1)", "data:text/html,<script>alert(1)</script>", "vbscript:msgbox(1)", "file:///etc/passwd", "ftp://x.io/f", ""):
            with self.subTest(uri=uri):
                out, exc = self.link(uri)
                self.assertNotIn("<a ", out)
                self.assertNotIn("href", out)
                self.assertIn("click", out)  # the visible text survives
                self.assertTrue(exc and "unsafe link" in exc[0]["reason"])

    def test_attribute_breakout_is_escaped(self):
        out, _ = self.link('https://x.io/" onmouseover="alert(1)')
        self.assertNotIn('" onmouseover', out)
        self.assertIn("&quot;", out)


class SlugRobustness(unittest.TestCase):
    def test_empty_or_missing_slug_never_falls_back_to_the_home_url(self):
        for raw in (None, "", "   ", "\n"):
            with self.subTest(raw=raw):
                r, exc = slug.resolve(raw, {"/": "/home-new"}, [])
                self.assertEqual((r["url"], r["status"]), ("", "empty"))
                self.assertTrue(exc and "slug is empty" in exc[0]["reason"])

    def test_the_root_slug_itself_is_still_valid(self):
        self.assertEqual(slug.resolve("/", None, [])[0], {"url": "/", "status": "free"})

    def test_sets_and_key_views_are_trusted_as_normalized_and_lists_are_normalized(self):
        urls = {"/a": {"id": 1}}
        self.assertEqual(slug.resolve("/a", None, urls.keys())[0]["status"], "collision")
        self.assertEqual(slug.resolve("/a", None, frozenset({"/a"}))[0]["status"], "collision")
        self.assertEqual(slug.resolve("/a", None, ["a/", "//a"])[0]["status"], "collision")  # a list is normalized first

    def test_resolving_against_a_large_key_view_is_fast(self):
        import time
        urls = {f"/p/{i}": {"id": i} for i in range(100_000)}
        t0 = time.time()
        for i in range(200):
            slug.resolve(f"/new-{i}", None, urls.keys())
        self.assertLess(time.time() - t0, 0.5)  # was ~40 ms PER CALL (7 s here) before the set was no longer rebuilt per call


class LocaleUnwrap(unittest.TestCase):
    def test_value_for_the_configured_locale(self):
        self.assertEqual(localeval.unwrap({"en-US": "hi", "fr": "salut"}, "en-US"), "hi")

    def test_a_locale_map_without_the_configured_locale_is_no_value_not_the_whole_map(self):
        self.assertIsNone(localeval.unwrap({"fr": "salut"}, "en-US"))
        self.assertIsNone(localeval.unwrap({"fr": "a", "de-AT": "b"}, "en-US"))

    def test_flat_values_and_json_objects_pass_through(self):
        self.assertEqual(localeval.unwrap("plain", "en-US"), "plain")
        self.assertEqual(localeval.unwrap(["a"], "en-US"), ["a"])
        self.assertEqual(localeval.unwrap({"heading": "x", "items": [1]}, "en-US"), {"heading": "x", "items": [1]})  # not locale keys
        self.assertEqual(localeval.unwrap({}, "en-US"), {})
        self.assertIsNone(localeval.unwrap(None, "en-US"))

    def test_the_checker_uses_the_same_rule(self):
        import verify_checks as v
        self.assertIsNone(v.source_value({"fields": {"title": {"fr": "salut"}}}, "title", "en-US"))
        self.assertEqual(v.source_value({"fields": {"title": {"en-US": "hi"}}}, "title", "en-US"), "hi")
        self.assertEqual(v.source_value({"title": "flat"}, "title", "en-US"), "flat")


class Refs(unittest.TestCase):
    def test_resolve_and_unresolved(self):
        r, exc = refs.resolve([{"sys": {"id": "a"}}, "b"], {"a": "B1"})
        self.assertEqual(r, ["B1", None])
        self.assertEqual(exc[0]["source_id"], "b")

    def test_single_and_empty(self):
        self.assertEqual(refs.resolve({"sys": {"id": "a"}}, {"a": "B1"})[0], ["B1"])
        self.assertEqual(refs.resolve(None, {}), ([], []))


class VerifyIntegration(unittest.TestCase):
    def test_richtext_comparator_uses_converter(self):
        import verify_checks as v
        src = doc(para(text("Yes, returns are accepted within 30 days.")))
        self.assertEqual(v.check_richtext("answer", src, "<p>Yes, returns are accepted within 30 days.</p>")["status"], "pass")
        self.assertEqual(v.check_richtext("answer", src, "<p>different</p>")["status"], "fail")
        emb = doc({"nodeType": "embedded-asset-block", "data": {"target": {"sys": {"id": "a"}}}, "content": []})
        self.assertEqual(v.check_richtext("answer", emb, "")["status"], "fuzzy")


if __name__ == "__main__":
    unittest.main()
