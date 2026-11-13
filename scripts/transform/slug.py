#!/usr/bin/env python3
"""Source slug -> Builder page URL, with collision detection (deterministic).

Policy (agreed with the user):
  1. A source URL that is free in Builder is used as-is.
  2. A source URL that already exists in Builder is NEVER overwritten: status `collision`, reported in
     `exceptions` so the entry goes to needs_human (the human picks a URL, usually via the mapping's
     `constant_map`, e.g. "/" -> "/home-contentful-migration").
  3. If the URL is already this entry's own draft (`own_url`), it is `own`, not a collision.
  4. No URL prefix.
  5. An empty/missing slug is an exception: it must never silently fall back to the home URL "/".

CLI:  echo '{"slug": "/x", "constant_map": {"/": "/home"}, "existing_urls": ["/a"], "own_url": null}' \
        | python3 scripts/transform/slug.py
      -> {"result": {"url": "/x", "status": "free|own|collision|rewritten-free|rewritten-collision"}, "exceptions": []}
"""
from __future__ import annotations

import json
import re
import sys
from collections.abc import Set


def normalize(slug: str) -> str:
    s = "/" + re.sub(r"/{2,}", "/", str(slug or "").strip()).strip("/")
    return s


def resolve(slug, constant_map=None, existing_urls=(), own_url=None) -> tuple[dict, list]:
    """`existing_urls`: a list/tuple of URLs (normalized here) OR a set / dict key view, which is trusted to hold already
    normalized URLs (callers that resolve thousands of pages pass `urls.keys()` so the set is not rebuilt per call)."""
    if not str(slug or "").strip():
        return {"url": "", "status": "empty"}, [{"reason": "source slug is empty; refusing to fall back to the home URL '/'", "source_slug": ""}]
    src = normalize(slug)
    url = normalize((constant_map or {}).get(slug, (constant_map or {}).get(src, src)))
    rewritten = url != src
    existing = existing_urls if isinstance(existing_urls, Set) else {normalize(u) for u in existing_urls}
    own = normalize(own_url) if own_url else None
    if own and url == own:
        status = "own"
    elif url in existing:
        status = "rewritten-collision" if rewritten else "collision"
    else:
        status = "rewritten-free" if rewritten else "free"
    exc = []
    if status.endswith("collision"):
        exc.append({"reason": f"URL {url} already exists in Builder; not overwriting. Needs a human-chosen URL.",
                    "source_slug": src})
    return {"url": url, "status": status}, exc


def main() -> int:
    a = json.load(sys.stdin)
    result, exceptions = resolve(a.get("slug"), a.get("constant_map"), a.get("existing_urls", []), a.get("own_url"))
    print(json.dumps({"result": result, "exceptions": exceptions}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
