import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import propose_mapping as pm  # noqa: E402


def ct(tid, fields):
    return {"sys": {"id": tid}, "name": tid, "fields": fields}


def link(i):
    return {"sys": {"type": "Link", "linkType": "Entry", "id": i}}


def entry(i, tid, **fields):
    return {"sys": {"id": i, "contentType": {"sys": {"id": tid}}}, "fields": {k: {"en-US": v} for k, v in fields.items()}}


TYPES = [
    ct("page", [{"id": "slug", "type": "Symbol"}, {"id": "parts", "type": "Array", "items": {"type": "Link", "linkType": "Entry"}}]),
    ct("hero", [{"id": "header", "type": "Symbol"}]),
    ct("banner", [{"id": "text", "type": "Symbol"}]),
    ct("author", [{"id": "name", "type": "Symbol"}]),
    ct("post", [{"id": "slug", "type": "Symbol"}, {"id": "author", "type": "Link", "linkType": "Entry"}]),
]
ENTRIES = [
    entry("p1", "page", slug="a", parts=[link("h1"), link("b1")]),
    entry("p2", "page", slug="b", parts=[link("h2"), link("b1")]),
    entry("h1", "hero"), entry("h2", "hero"), entry("b1", "banner"), entry("a1", "author"),
    entry("x1", "post", slug="x", author=link("a1")),
]


class Roles(unittest.TestCase):
    def test_roles_from_the_reference_graph(self):
        r = pm.classify_roles(TYPES, ENTRIES)
        self.assertEqual(r["page"]["role"], "page")
        self.assertEqual(r["hero"]["role"], "inline-component")      # each hero has one parent
        self.assertEqual(r["banner"]["role"], "shared-component")    # b1 is used by two pages
        self.assertEqual(r["author"]["role"], "data-record")         # single-value link from a post
        self.assertEqual(r["post"]["role"], "page")                  # has a slug, nothing links to it

    def test_without_entries_the_graph_is_unknown_and_confidence_is_low(self):
        r = pm.classify_roles(TYPES, [])
        self.assertTrue(all(v["confidence"] == "low" for v in r.values()))
        self.assertEqual(r["page"]["role"], "page")

    def test_symbols_are_never_proposed(self):
        roles = {v["role"] for v in pm.classify_roles(TYPES, ENTRIES).values()}
        self.assertNotIn("symbol", roles)

    def test_unsupported_roles_are_called_out(self):
        self.assertIn("section models", pm.not_yet_supported("shared-component"))
        self.assertIn("reference", pm.not_yet_supported("data-record"))
        self.assertEqual(pm.not_yet_supported("page"), "")


if __name__ == "__main__":
    unittest.main()
