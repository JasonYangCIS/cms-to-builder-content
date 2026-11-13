#!/usr/bin/env python3
"""Contentful rich-text document -> HTML (deterministic).

CLI:  echo '{"document": {...}}' | python3 scripts/transform/richtext.py
      -> {"result": "<p>...</p>", "exceptions": [...]}
Anything this converter cannot represent faithfully is NOT guessed at: it is reported in `exceptions`
(embedded entries/assets, entry/asset hyperlinks, unknown node or mark types) for the migrator/human.
"""
from __future__ import annotations

import html
import json
import sys
from urllib.parse import urlsplit

BLOCKS = {
    "paragraph": "p", "heading-1": "h1", "heading-2": "h2", "heading-3": "h3", "heading-4": "h4",
    "heading-5": "h5", "heading-6": "h6", "unordered-list": "ul", "ordered-list": "ol",
    "list-item": "li", "blockquote": "blockquote",
}
MARKS = {"bold": "strong", "italic": "em", "underline": "u", "code": "code"}
SAFE_SCHEMES = {"http", "https", "mailto", "tel"}  # plus relative links and #fragments; never javascript:, data:, vbscript:, ...
EMBEDS = {"embedded-entry-block", "embedded-entry-inline", "embedded-asset-block",
          "entry-hyperlink", "asset-hyperlink"}


def esc(s: str) -> str:
    return html.escape(s, quote=False)


def safe_uri(uri: str) -> bool:
    """True for http(s)/mailto/tel links, relative links and #fragments. urlsplit drops the tab/newline tricks
    (java\\tscript:) browsers also ignore; control characters in the scheme are rejected outright."""
    u = uri.strip()
    if not u or any(ord(c) < 32 for c in u[:12]):
        return False
    scheme = urlsplit(u).scheme.lower()
    return scheme == "" or scheme in SAFE_SCHEMES


def render(node: dict, exc: list, path: str) -> str:
    nt = node.get("nodeType", "")
    if nt == "text":
        out = esc(node.get("value", ""))
        for m in node.get("marks", []):
            tag = MARKS.get(m.get("type"))
            if tag is None:
                exc.append({"path": path, "reason": f"unsupported mark {m.get('type')!r}"})
            else:
                out = f"<{tag}>{out}</{tag}>"
        return out
    inner = "".join(render(c, exc, f"{path}.{i}") for i, c in enumerate(node.get("content", [])))
    if nt == "document":
        return inner
    if nt in BLOCKS:
        tag = BLOCKS[nt]
        return f"<{tag}>{inner}</{tag}>"
    if nt == "hr":
        return "<hr>"
    if nt == "hyperlink":
        uri = str(node.get("data", {}).get("uri", "") or "")
        if not safe_uri(uri):
            exc.append({"path": path, "reason": f"unsafe link scheme in {uri[:60]!r}; link dropped, text kept"})
            return inner
        return f'<a href="{html.escape(uri.strip(), quote=True)}">{inner}</a>'
    if nt in EMBEDS:
        exc.append({"path": path, "reason": f"{nt} needs manual handling",
                    "target": node.get("data", {}).get("target", {}).get("sys", {}).get("id")})
        return inner
    exc.append({"path": path, "reason": f"unknown node type {nt!r}"})
    return inner


def convert(document) -> tuple[str, list]:
    exc: list = []
    if document in (None, "", {}):
        return "", exc
    if not isinstance(document, dict) or document.get("nodeType") != "document":
        return "", [{"path": "", "reason": "not a Contentful rich-text document"}]
    return render(document, exc, "0"), exc


def main() -> int:
    doc = json.load(sys.stdin).get("document")
    result, exceptions = convert(doc)
    print(json.dumps({"result": result, "exceptions": exceptions}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
