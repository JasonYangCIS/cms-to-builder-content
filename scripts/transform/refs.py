#!/usr/bin/env python3
"""Resolve Contentful links to Builder ids via the manifest id map (second pass, deterministic).

CLI:  echo '{"links": [{"sys": {"id": "abc"}}, "def"], "id_map": {"abc": "b1"}}' | python3 scripts/transform/refs.py
      -> {"result": ["b1", null], "exceptions": [{"source_id": "def", "reason": "not in id map yet"}]}
Unresolved links stay null in `result` and are listed in `exceptions` (they are not guessed).
"""
from __future__ import annotations

import json
import sys


def link_id(x):
    return (x.get("sys", {}).get("id") or x.get("id")) if isinstance(x, dict) else x


def resolve(links, id_map) -> tuple[list, list]:
    items = links if isinstance(links, list) else ([links] if links else [])
    result, exc = [], []
    for x in items:
        sid = link_id(x)
        tid = id_map.get(sid)
        result.append(tid)
        if tid is None:
            exc.append({"source_id": sid, "reason": "not in id map yet"})
    return result, exc


def main() -> int:
    a = json.load(sys.stdin)
    result, exceptions = resolve(a.get("links"), a.get("id_map", {}))
    print(json.dumps({"result": result, "exceptions": exceptions}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
