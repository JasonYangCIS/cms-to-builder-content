#!/usr/bin/env python3
"""Approval gate for mapping/field-mapping.json.

The mapping carries `approved` + `approved_hash`. The hash covers everything
except the approval keys, so ANY edit to the mapping invalidates the approval.

  mapping.py check     exit 0 only if approved and hash matches; otherwise resets
                       `approved` to false in the file and exits 1 (agents use this)
  mapping.py approve   HUMAN ONLY: records the current hash and sets approved=true
  mapping.py status    print approval state as JSON
"""
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

PATH = Path(__file__).resolve().parent.parent / "mapping" / "field-mapping.json"
APPROVAL_KEYS = ("approved", "approved_hash", "approved_at")


def load():
    if not PATH.exists():
        sys.exit(f"{PATH.name} not found. Copy mapping/field-mapping.example.json to mapping/field-mapping.json, "
                 "fill in your space ids, review it, then run `python3 scripts/mapping.py approve`.")
    return json.loads(PATH.read_text())


def body_hash(m: dict) -> str:
    body = {k: v for k, v in m.items() if k not in APPROVAL_KEYS}
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def save(m: dict):
    PATH.write_text(json.dumps(m, indent=2) + "\n")


def state(m: dict) -> dict:
    current = body_hash(m)
    return {
        "approved": bool(m.get("approved")) and m.get("approved_hash") == current,
        "flag_set": bool(m.get("approved")),
        "hash_matches": m.get("approved_hash") == current,
        "current_hash": current,
    }


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    m = load()
    s = state(m)
    if cmd == "status":
        print(json.dumps(s, indent=2))
    elif cmd == "check":
        if s["approved"]:
            print(json.dumps(s))
            return
        if m.get("approved"):  # flag set but body changed -> reset approval
            m["approved"] = False
            m.pop("approved_hash", None)
            m.pop("approved_at", None)
            save(m)
            print("mapping changed since approval; approval reset", file=sys.stderr)
        else:
            print("mapping is not approved", file=sys.stderr)
        sys.exit(1)
    elif cmd == "approve":
        m["approved"] = True
        m["approved_hash"] = s["current_hash"]
        m["approved_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        save(m)
        print(json.dumps(state(m), indent=2))
    else:
        print(__doc__)
        sys.exit(2)


if __name__ == "__main__":
    main()
