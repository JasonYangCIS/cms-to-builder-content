"""Tests for scripts/guard.py (the PreToolUse hook): the decision rules and the hook's exit codes.

Run: python3 -m unittest scripts/test_guard.py -v
"""
import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import guard  # noqa: E402

B = guard.BUILDER
DRAFT = {"modelName": "page", "name": "x", "data": {}, "published": "draft"}


class Decide(unittest.TestCase):
    def blocked(self, tool, args=None):
        return guard.decide(tool, args if args is not None else {}) is not None

    def test_contentful_is_read_only_on_any_contentful_server(self):
        for tool in ("get_entry", "list_content_types", "search_entries", "semantic_search", "resolve_entry_references"):
            self.assertFalse(self.blocked(guard.CONTENTFUL + tool), tool)
        for tool in ("create_entry", "update_entry", "publish_entry", "delete_entry", "upload_asset", "create_upload_session", "invoke_ai_action"):
            self.assertTrue(self.blocked(guard.CONTENTFUL + tool), tool)
        self.assertTrue(self.blocked("mcp__contentful_eu__update_entry"))
        self.assertTrue(self.blocked("mcp__claude_ai_Contentful__create_entry"))

    def test_creating_builder_content_requires_an_explicit_draft(self):
        self.assertFalse(self.blocked(B + "create_builder_content", DRAFT))
        no_flag = {k: v for k, v in DRAFT.items() if k != "published"}
        self.assertTrue(self.blocked(B + "create_builder_content", no_flag), "must not rely on the default")
        for bad in ("published", "archived", "", None, True, "DRAFT"):
            self.assertTrue(self.blocked(B + "create_builder_content", {**DRAFT, "published": bad}), repr(bad))
        not_objects: list = [None, [], "x", 5]  # arguments that are not even an object
        for args in not_objects:
            self.assertTrue(self.blocked(B + "create_builder_content", args), repr(args))

    def test_symbols_are_blocked_because_they_publish_on_creation(self):
        self.assertTrue(self.blocked(B + "create_symbol", {"name": "x", "data": {}}))
        self.assertTrue(self.blocked(B + "create_builder_content", {**DRAFT, "modelName": "symbol"}))
        self.assertTrue(self.blocked(B + "create_builder_content", {**DRAFT, "modelName": "SYMBOL"}))

    def test_anything_named_publish_delete_or_archive_is_blocked(self):
        for name in ("publish_builder_content", "unpublish_builder_content", "publish_anything", "delete_content", "delete_builder_model", "archive_content"):
            self.assertTrue(self.blocked(B + name, {"id": "1"}), name)
        self.assertTrue(self.blocked("mcp__builder__delete_content", {}))
        self.assertTrue(self.blocked("mcp__my_builder_connector__publish_content", {}))

    def test_updates_cannot_publish(self):
        self.assertFalse(self.blocked(B + "update_content", {"contentId": "1", "data": {"title": "t"}}))
        self.assertFalse(self.blocked(B + "update_content", {"contentId": "1", "publishLive": False}))
        self.assertTrue(self.blocked(B + "update_content", {"contentId": "1", "publishLive": True}))
        self.assertTrue(self.blocked(B + "update_content", {"contentId": "1", "published": "published"}))

    def test_publish_flags_are_found_at_any_depth_and_inside_json_strings(self):
        deep = {"data": {"blocks": [{"children": [{"meta": {"published": "published"}}]}]}}
        self.assertTrue(self.blocked(B + "update_content", deep))
        for payload in ('{"published": "published"}', '  {"a": [{"publishLive": true}]}', '[{"published": "archived"}]'):
            self.assertTrue(self.blocked(B + "update_content", {"contentId": "1", "data": {"raw": payload}}), payload)
        self.assertFalse(self.blocked(B + "update_content", {"contentId": "1", "data": {"raw": '{"published": "draft"}'}}))
        self.assertFalse(self.blocked(B + "update_content", {"contentId": "1", "data": {"raw": "{not json"}}))

    def test_real_sized_block_trees_are_not_blocked_but_pathological_nesting_is(self):
        tree: dict = {"leaf": 1}
        for _ in range(40):  # a deep but legitimate page
            tree = {"children": [tree]}
        self.assertFalse(self.blocked(B + "create_builder_content", {**DRAFT, "data": {"blocks": [tree]}}))
        for _ in range(100):
            tree = {"children": [tree]}
        self.assertTrue(self.blocked(B + "create_builder_content", {**DRAFT, "data": {"blocks": [tree]}}))

    def test_reads_and_unrelated_servers_are_allowed(self):
        self.assertFalse(self.blocked(B + "get_content", {"modelName": "page"}))
        self.assertFalse(self.blocked(B + "search_assets", {"searchText": "x"}))
        self.assertFalse(self.blocked("mcp__slack__post_message", {"text": "hi"}))
        self.assertFalse(self.blocked("Bash", {"command": "ls"}))

    def test_a_server_name_only_matters_as_a_whole_segment(self):
        # the tool NAME may mention builder/contentful without the SERVER being one
        self.assertFalse(self.blocked("mcp__github__get_builder_config", {}))
        self.assertFalse(self.blocked("mcp__github__update_contentful_notes", {}))


class Hook(unittest.TestCase):
    """The hook protocol: exit 2 + reason on stderr blocks; exit 0 allows; unparseable input fails closed."""

    def run_hook(self, stdin):
        p = subprocess.run([sys.executable, str(ROOT / "scripts" / "guard.py")], input=stdin, text=True, capture_output=True)
        return p.returncode, p.stderr

    def test_blocked_call_exits_2_with_the_reason(self):
        code, err = self.run_hook(json.dumps({"tool_name": B + "delete_content", "tool_input": {"id": "1"}}))
        self.assertEqual(code, 2)
        self.assertIn("BLOCKED by guard.py", err)

    def test_allowed_call_exits_0(self):
        code, _ = self.run_hook(json.dumps({"tool_name": B + "create_builder_content", "tool_input": DRAFT}))
        self.assertEqual(code, 0)

    def test_malformed_input_fails_closed(self):
        for stdin in ("", "not json", "[1,2"):
            code, err = self.run_hook(stdin)
            self.assertEqual(code, 2, repr(stdin))
            self.assertIn("could not parse", err)

    def test_an_unexpected_error_in_the_rules_fails_closed_not_open(self):
        """Claude Code blocks only on exit 2; a crash (exit 1) would let the tool call through. Run the hook with decide() broken."""
        code = ("import sys, json; sys.path.insert(0, %r); import guard\n"
                "guard.decide = lambda tool, args: 1 / 0\n"
                "guard.main()\n") % str(ROOT / "scripts")
        p = subprocess.run([sys.executable, "-c", code], input=json.dumps({"tool_name": B + "get_content", "tool_input": {}}),
                           text=True, capture_output=True)
        self.assertEqual(p.returncode, 2, p.stderr)
        self.assertIn("fails closed", p.stderr)
        self.assertIn("ZeroDivisionError", p.stderr)

    def test_selftest_passes(self):
        p = subprocess.run([sys.executable, str(ROOT / "scripts" / "guard.py"), "--selftest"], capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stdout)


if __name__ == "__main__":
    unittest.main()
