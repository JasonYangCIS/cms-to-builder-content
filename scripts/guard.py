#!/usr/bin/env python3
"""PreToolUse safety hook for MCP calls (reads the hook JSON on stdin).

Blocks (exit 2, reason on stderr) what permission rules can't express:
  * any Contentful tool that isn't clearly read-only (allowlist by name prefix)
  * any Builder tool whose name says delete / publish / archive, and `create_symbol` (symbols are published on creation)
  * `create_builder_content` unless it passes published="draft" EXPLICITLY (never rely on a default) and is not a symbol
  * any Builder call whose arguments publish content: published="published", publishLive=true, or a `published` value
    other than "draft", at any depth, including inside JSON-encoded string arguments

Servers are recognized by name, not by one hard-coded connector: any MCP server whose name contains "contentful" gets the
read-only rule and any whose name contains "builder" gets the Builder rules, so a renamed or second connector is covered.
Everything else is allowed (exit 0). Fail closed on malformed input AND on any unexpected error: Claude Code blocks a call only
on exit code 2, so a crash (exit 1) would otherwise let it through.
The hook only sees MCP tool calls; scripts that hold tokens are governed by their own --apply flags and the permission rules.
Run `python3 scripts/guard.py --selftest` (and `make test`) to exercise the rules.
"""
from __future__ import annotations

import json
import sys

CONTENTFUL = "mcp__contentful__"
BUILDER = "mcp__claude_ai_Builder_io_Publish__"
CONTENTFUL_READ_PREFIXES = ("get_", "list_", "search_", "semantic_search", "resolve_")
BUILDER_BLOCKED_NAME_PARTS = ("delete", "publish", "archive")  # matches publish_*, unpublish_*, *_archive, delete_*
BUILDER_BLOCKED_TOOLS = ("create_symbol",)  # symbols are published automatically on creation
MAX_DEPTH = 200  # counts every nested dict AND list; real Builder block trees reach ~100, only pathological nesting is refused


def split_tool(tool: str) -> tuple[str, str]:
    """'mcp__<server>__<name>' -> (server lowercased, name). Non-MCP tools -> ('', '')."""
    parts = tool.split("__", 2)
    return (parts[1].lower(), parts[2]) if tool.startswith("mcp__") and len(parts) == 3 else ("", "")


def publishes(obj, depth: int = 0) -> str | None:
    """Return a reason if any nested arg publishes content (also looks inside JSON-encoded strings)."""
    if depth > MAX_DEPTH:
        return "arguments nested too deeply to inspect"
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "publishLive" and v is True:
                return "publishLive=true"
            if k == "published" and v not in (None, False, "draft"):
                return f"published={v!r}"
            r = publishes(v, depth + 1)
            if r:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = publishes(v, depth + 1)
            if r:
                return r
    elif isinstance(obj, str) and obj.lstrip()[:1] in ("{", "["):
        try:
            return publishes(json.loads(obj), depth + 1)
        except ValueError:
            return None
    return None


def decide(tool: str, args) -> str | None:
    """Return a block reason, or None to allow."""
    server, name = split_tool(tool)
    if "contentful" in server:
        if not name.startswith(CONTENTFUL_READ_PREFIXES):
            return f"Contentful is read-only; '{name}' is not a read tool"
        return None
    if "builder" in server:
        low = name.lower()
        if any(p in low for p in BUILDER_BLOCKED_NAME_PARTS):
            return f"Builder tool '{name}' is blocked (no deletes/publishing/archiving in this pipeline)"
        if low in BUILDER_BLOCKED_TOOLS:
            return f"Builder tool '{name}' is blocked (it publishes on creation)"
        if low == "create_builder_content":
            a = args if isinstance(args, dict) else {}
            if str(a.get("modelName", "")).lower() == "symbol":
                return "creating a Builder symbol is blocked (symbols are published on creation)"
            if a.get("published") != "draft":
                return 'create_builder_content must pass published="draft" explicitly (never rely on a default)'
        reason = publishes(args)
        if reason:
            return f"Builder write would publish ({reason}); drafts only unless the user explicitly says to publish"
    return None


def selftest() -> int:
    draft = {"modelName": "page", "name": "x", "data": {}, "published": "draft"}
    cases = [
        (CONTENTFUL + "get_entry", {}, False),
        (CONTENTFUL + "search_entries", {}, False),
        (CONTENTFUL + "update_entry", {}, True),
        (CONTENTFUL + "delete_entry", {}, True),
        (CONTENTFUL + "publish_entry", {}, True),
        (CONTENTFUL + "create_upload_session", {}, True),
        ("mcp__contentful_eu__update_entry", {}, True),                      # a renamed/second Contentful server is covered too
        (BUILDER + "create_builder_content", draft, False),
        (BUILDER + "create_builder_content", {"modelName": "page", "name": "x", "data": {}}, True),  # no explicit draft: blocked
        (BUILDER + "create_builder_content", {**draft, "published": "published"}, True),
        (BUILDER + "create_builder_content", {**draft, "modelName": "symbol"}, True),
        (BUILDER + "create_symbol", {"name": "x", "data": {}}, True),
        (BUILDER + "update_content", {"contentId": "1", "publishLive": True}, True),
        (BUILDER + "update_content", {"contentId": "1", "publishLive": False}, False),
        (BUILDER + "update_content", {"contentId": "1", "data": {"args": json.dumps({"published": "published"})}}, True),  # inside a JSON string
        (BUILDER + "publish_builder_content", {"contentId": "1"}, True),
        (BUILDER + "unpublish_builder_content", {"contentId": "1"}, True),
        (BUILDER + "archive_content", {"contentId": "1"}, True),
        ("mcp__builder__delete_content", {"id": "1"}, True),                  # another Builder server name
        ("mcp__my_builder__create_builder_content", {"modelName": "page"}, True),
        (BUILDER + "get_content", {"modelName": "page"}, False),
        ("mcp__slack__post_message", {"text": "hi"}, False),                  # unrelated servers are not our business
        ("Bash", {"command": "ls"}, False),
    ]
    bad = 0
    for tool, args, should_block in cases:
        blocked = decide(tool, args) is not None
        ok = blocked == should_block
        bad += not ok
        print(("ok  " if ok else "FAIL"), "block" if blocked else "allow", tool.split("__")[-1], json.dumps(args)[:60])
    return 1 if bad else 0


def main():
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    try:
        event = json.load(sys.stdin)
        tool = event.get("tool_name", "")
        args = event.get("tool_input", {})
    except Exception as e:  # malformed input: fail closed for MCP tools only
        print(f"guard.py: could not parse hook input: {e}", file=sys.stderr)
        sys.exit(2)
    try:
        reason = decide(tool, args)
    except BaseException as e:  # noqa: BLE001
        # Claude Code only blocks on exit code 2; any other failure (a crash exits 1) would let the call through.
        # So an unexpected error in the rules must fail CLOSED, never open.
        print(f"BLOCKED by guard.py: the guard hit an unexpected error ({type(e).__name__}: {e}) and fails closed", file=sys.stderr)
        sys.exit(2)
    if reason:
        print(f"BLOCKED by guard.py: {reason}", file=sys.stderr)
        sys.exit(2)
    sys.exit(0)


if __name__ == "__main__":
    main()
