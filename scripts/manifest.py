#!/usr/bin/env python3
"""Migration manifest: the single source of truth for per-entry state.

Agents never touch the SQLite DB directly; they call this CLI. Every command
prints JSON to stdout so agents can parse it. Errors go to stderr, exit 1.

Commands:
  seed           --file PATH|-            insert entries (JSON list of {source_id, content_type})
  claim          --status S --limit N     atomically claim entries -> in_progress
                 [--content-type T]
  release-stale  [--minutes M]            return stale in_progress claims to their prior status
  set ID         --status S               update an entry
                 [--target ID] [--notes TEXT]
                 [--expect-status S]      compare-and-set: fail (and change nothing) unless the row is currently S.
                                          Workers holding a claim pass `in_progress`, so a worker that was too slow
                                          (its claim released and re-claimed) cannot overwrite the new owner's row.
                                          `set` never sets `in_progress` (use `claim`) and never moves a
                                          needs_human/failed/skipped row straight to `verified` (requeue it first).
  get ID                                  show one entry
  list           [--status S] [--content-type T]   all rows as {"rows": [...]} (read-only)
  summary                                 counts by status and content type
  log-write      --source-id ID           append an audit line to logs/writes.jsonl
                 --action A [--target-id ID]
  requeue        [selectors] [--apply]    send needs_human/failed/skipped rows back to `pending` with the retry
                 [--keep-retries]         count reset (a plain `set --status pending` keeps it, so a row that was
                                          escalated would escalate again on its next failure)
  skip           --reason TEXT            mark pending/needs_human/failed rows `skipped`: deliberately NOT migrated,
                 [selectors] [--apply]    a human decision; they stop counting as work or as needing attention

Bulk selectors (requeue/skip): --from S[,S] (default: needs_human for requeue; pending,needs_human,failed for skip),
--content-type T, --notes-contains TEXT (case-insensitive), --ids-file PATH|- (a worklist CSV with a source_id column,
a JSON list, or one id per line), --limit N. With no selector you must pass --all. Both are DRY RUNS unless --apply is
given, and every applied bulk change is appended to logs/manifest-changes.jsonl. For the orchestrator/human only:
subagents must not run them.

In-process use (no subprocess per call; same code path as the CLI): `import manifest; manifest.run(["set", id, "--status", ...])`
returns the JSON the command prints and raises RuntimeError with the error text on failure.

Env overrides (read at call time; used by tests): MANIFEST_DB, WRITES_LOG, CHANGES_LOG.
"""
import argparse
import contextlib
import csv
import io
import json
import os
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def db_path() -> Path:  # read at call time so tests (and in-process callers) can point at a temporary database
    return Path(os.environ.get("MANIFEST_DB", ROOT / "state" / "manifest.db"))


def writes_log() -> Path:
    return Path(os.environ.get("WRITES_LOG", ROOT / "logs" / "writes.jsonl"))


def changes_log() -> Path:
    return Path(os.environ.get("CHANGES_LOG", ROOT / "logs" / "manifest-changes.jsonl"))


STATUSES = ("pending", "in_progress", "migrated", "verified", "failed", "needs_human", "skipped")
MAX_RETRIES = 3  # a 4th failure (retry_count > 3) escalates to needs_human

TABLE_SQL = f"""
CREATE TABLE IF NOT EXISTS entries (
    source_id    TEXT PRIMARY KEY,
    content_type TEXT NOT NULL,
    target_id    TEXT,
    status       TEXT NOT NULL DEFAULT 'pending'
                 CHECK (status IN ({", ".join(repr(s) for s in STATUSES)})),
    retry_count  INTEGER NOT NULL DEFAULT 0,
    claimed_at   TEXT,
    claimed_from TEXT,
    notes        TEXT,
    updated_at   TEXT NOT NULL
)"""
INDEX_SQL = "CREATE INDEX IF NOT EXISTS idx_entries_status ON entries(status)"
SCHEMA = TABLE_SQL + ";\n" + INDEX_SQL + ";\n"
COLUMNS = "source_id, content_type, target_id, status, retry_count, claimed_at, claimed_from, notes, updated_at"


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def migrate(conn: sqlite3.Connection) -> None:
    """Older databases carry a CHECK constraint that lacks newer statuses (e.g. `skipped`). SQLite cannot alter a CHECK,
    so rebuild the table inside one exclusive transaction; every row is preserved."""
    def stale() -> bool:
        row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='entries'").fetchone()
        return bool(row) and any(f"'{s}'" not in row[0] for s in STATUSES)

    if not stale():
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        if stale():  # another process may have migrated while we waited for the lock
            before = conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
            conn.execute("ALTER TABLE entries RENAME TO entries_old")
            conn.execute(TABLE_SQL)
            conn.execute(f"INSERT INTO entries ({COLUMNS}) SELECT {COLUMNS} FROM entries_old")
            after = conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
            if after != before:
                raise RuntimeError(f"manifest migration would lose rows ({before} -> {after}); rolled back")
            conn.execute("DROP TABLE entries_old")
            conn.execute(INDEX_SQL)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def connect() -> sqlite3.Connection:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.executescript(SCHEMA)
    migrate(conn)
    return conn


def out(obj) -> None:
    print(json.dumps(obj, indent=2, default=str))


def die(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)


def check_status(s: str) -> str:
    if s not in STATUSES:
        die(f"invalid status {s!r}; expected one of {', '.join(STATUSES)}")
    return s


def cmd_seed(args):
    raw = sys.stdin.read() if args.file == "-" else Path(args.file).read_text()
    items = json.loads(raw)
    if not isinstance(items, list):
        die("seed file must be a JSON list of {source_id, content_type}")
    conn = connect()
    inserted = skipped = 0
    conn.execute("BEGIN IMMEDIATE")
    for it in items:
        if not isinstance(it, dict) or "source_id" not in it or "content_type" not in it:
            conn.execute("ROLLBACK")
            die(f"each item needs source_id and content_type, got {it!r}")
        cur = conn.execute(
            "INSERT OR IGNORE INTO entries (source_id, content_type, status, updated_at) "
            "VALUES (?, ?, 'pending', ?)",
            (str(it["source_id"]), it["content_type"], now()),
        )
        inserted += cur.rowcount
        skipped += 1 - cur.rowcount
    conn.execute("COMMIT")
    out({"inserted": inserted, "already_present": skipped})


def cmd_claim(args):
    check_status(args.status)
    if args.status == "in_progress":
        die("cannot claim entries that are already in_progress")
    conn = connect()
    conn.execute("BEGIN IMMEDIATE")  # write lock: concurrent claimers can't overlap
    sql = "SELECT source_id FROM entries WHERE status = ?"
    params = [args.status]
    if args.content_type:
        sql += " AND content_type = ?"
        params.append(args.content_type)
    sql += " ORDER BY updated_at, source_id LIMIT ?"
    params.append(args.limit)
    ids = [r["source_id"] for r in conn.execute(sql, params)]
    ts = now()
    for sid in ids:
        conn.execute(
            "UPDATE entries SET status='in_progress', claimed_at=?, claimed_from=?, updated_at=? "
            "WHERE source_id=?",
            (ts, args.status, ts, sid),
        )
    rows = [
        dict(r)
        for r in conn.execute(
            f"SELECT * FROM entries WHERE source_id IN ({','.join('?' * len(ids))})", ids
        )
    ] if ids else []
    conn.execute("COMMIT")
    out({"claimed": rows})


def cmd_release_stale(args):
    conn = connect()
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=args.minutes)).isoformat(timespec="seconds")
    conn.execute("BEGIN IMMEDIATE")
    stale = list(
        conn.execute(
            "SELECT source_id, claimed_from FROM entries WHERE status='in_progress' AND (claimed_at IS NULL OR claimed_at < ?)",
            (cutoff,),
        )
    )
    ts = now()
    for r in stale:
        back = r["claimed_from"] if r["claimed_from"] in STATUSES else "pending"
        conn.execute(
            "UPDATE entries SET status=?, claimed_at=NULL, claimed_from=NULL, updated_at=? WHERE source_id=?",
            (back, ts, r["source_id"]),
        )
    conn.execute("COMMIT")
    out({"released": [{"source_id": r["source_id"], "returned_to": r["claimed_from"] or "pending"} for r in stale]})


def cmd_set(args):
    check_status(args.status)
    if args.status == "in_progress":
        die("`set` cannot set in_progress (that is what `claim` is for: it records who holds the row and since when)")
    if args.expect_status:
        check_status(args.expect_status)
    conn = connect()
    conn.execute("BEGIN IMMEDIATE")
    row = conn.execute("SELECT * FROM entries WHERE source_id=?", (args.id,)).fetchone()
    if row is None:
        conn.execute("ROLLBACK")
        die(f"unknown source_id {args.id!r}")
    if args.expect_status and row["status"] != args.expect_status:
        conn.execute("ROLLBACK")
        die(f"{args.id!r} is {row['status']!r}, not {args.expect_status!r}; nothing changed (its claim may have been released or re-claimed)")
    if args.status == "verified" and row["status"] in ("needs_human", "failed", "skipped"):
        conn.execute("ROLLBACK")
        die(f"{args.id!r} is {row['status']!r}; it cannot go straight to verified (requeue it and let it be migrated and verified)")
    status = args.status
    retry = row["retry_count"]
    notes = args.notes if args.notes is not None else row["notes"]
    if status == "failed":
        retry += 1
        if retry > MAX_RETRIES:
            status = "needs_human"
            notes = f"[escalated after {MAX_RETRIES} retries] {notes or ''}".strip()
    target = args.target if args.target is not None else row["target_id"]
    conn.execute(
        "UPDATE entries SET status=?, target_id=?, retry_count=?, notes=?, "
        "claimed_at=NULL, claimed_from=NULL, updated_at=? WHERE source_id=?",
        (status, target, retry, notes, now(), args.id),
    )
    conn.execute("COMMIT")
    out(dict(conn.execute("SELECT * FROM entries WHERE source_id=?", (args.id,)).fetchone()))


def cmd_get(args):
    row = connect().execute("SELECT * FROM entries WHERE source_id=?", (args.id,)).fetchone()
    if row is None:
        die(f"unknown source_id {args.id!r}")
    out(dict(row))


def cmd_list(args):
    """Read-only: every row, optionally filtered. (Used by the Stage 7 reconcile/SEO scripts.)"""
    sql, params = "SELECT * FROM entries WHERE 1=1", []
    if args.status:
        sql += " AND status = ?"; params.append(check_status(args.status))
    if args.content_type:
        sql += " AND content_type = ?"; params.append(args.content_type)
    out({"rows": [dict(r) for r in connect().execute(sql + " ORDER BY source_id", params)]})


NOTES_MAX = 2000
BULK_FROM_ALLOWED = {"requeue": ("needs_human", "failed", "skipped"), "skip": ("pending", "needs_human", "failed")}
BULK_FROM_DEFAULT = {"requeue": "needs_human", "skip": "pending,needs_human,failed"}


def read_ids(spec: str) -> list:
    """Ids from a worklist CSV (source_id column), a JSON list (strings or {source_id}), or one id per line."""
    text = (sys.stdin.read() if spec == "-" else Path(spec).read_text()).lstrip("\ufeff")
    first = text.splitlines()[0] if text.strip() else ""
    ids = []
    looks_json = first.lstrip().startswith(("[", "{"))
    if spec.lower().endswith(".csv") or (spec == "-" and not looks_json and "source_id" in first and "," in first):
        reader = csv.DictReader(io.StringIO(text))
        if not reader.fieldnames or "source_id" not in reader.fieldnames:
            die("the CSV needs a source_id column (worklists/*.csv have one)")
        ids = [r["source_id"].strip().lstrip("'") for r in reader]  # worklist CSVs prefix formula-looking cells with an apostrophe
    else:
        try:
            data = json.loads(text)
            ids = [str(x["source_id"] if isinstance(x, dict) else x) for x in data]
        except (ValueError, TypeError, KeyError):
            ids = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    seen, out_ids = set(), []
    for i in ids:
        i = i.strip()
        if i and i not in seen:
            seen.add(i)
            out_ids.append(i)
    return out_ids


def bulk(args, action: str):
    allowed = BULK_FROM_ALLOWED[action]
    statuses = [s.strip() for s in (args.from_status or BULK_FROM_DEFAULT[action]).split(",") if s.strip()]
    bad = [s for s in statuses if s not in allowed]
    if bad:
        die(f"{action} can only act on rows that are {', '.join(allowed)}; not {', '.join(bad)}")
    if not (args.content_type or args.notes_contains or args.ids_file) and not args.all:
        die("refusing to act on every row: add a selector (--content-type, --notes-contains, --ids-file) or pass --all")
    reason = (getattr(args, "reason", None) or "").strip()
    if action == "skip" and not reason:
        die("skip needs --reason (recorded on every skipped row)")
    ids = read_ids(args.ids_file) if args.ids_file else None
    conn = connect()
    conn.execute("BEGIN IMMEDIATE")
    try:
        _bulk_locked(conn, args, action, statuses, reason, ids)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")  # all-or-nothing: a bulk change is never left half applied
        raise


def _bulk_locked(conn, args, action, statuses, reason, ids):
    sql = f"SELECT * FROM entries WHERE status IN ({','.join('?' * len(statuses))})"
    params = list(statuses)
    if args.content_type:
        sql += " AND content_type = ?"
        params.append(args.content_type)
    if args.notes_contains:
        sql += " AND instr(lower(coalesce(notes, '')), lower(?)) > 0"
        params.append(args.notes_contains)
    rows = [dict(r) for r in conn.execute(sql + " ORDER BY source_id", params)]
    not_matched = []
    if ids is not None:
        want = set(ids)
        rows = [r for r in rows if r["source_id"] in want]
        found = {r["source_id"] for r in rows}
        not_matched = [i for i in ids if i not in found]
    if args.limit:
        rows = rows[: args.limit]
    ts = now()
    result = {"action": action, "dry_run": not args.apply, "matched": len(rows), "changed": 0,
              "by_status": dict(Counter(r["status"] for r in rows)), "by_content_type": dict(Counter(r["content_type"] for r in rows)),
              "sample": [r["source_id"] for r in rows[:5]], "ids_not_matched": not_matched[:20], "ids_not_matched_count": len(not_matched)}
    if args.apply and rows:
        updates = []
        for r in rows:
            old = (r["notes"] or "").strip()
            if action == "requeue":
                new_status, retry = "pending", (r["retry_count"] if args.keep_retries else 0)
                note = f"[requeued {ts} from {r['status']}] {old}"
            else:
                new_status, retry = "skipped", r["retry_count"]
                note = f"[skipped: {reason}] {old}"
            updates.append((new_status, retry, note[:NOTES_MAX].rstrip(), ts, r["source_id"]))
        conn.executemany("UPDATE entries SET status=?, retry_count=?, notes=?, claimed_at=NULL, claimed_from=NULL, updated_at=? WHERE source_id=?", updates)
        log_path = changes_log()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a") as f:
            f.write(json.dumps({"timestamp": ts, "action": action, "from": statuses, "to": "pending" if action == "requeue" else "skipped",
                                "reason": reason or None, "keep_retries": bool(getattr(args, "keep_retries", False)),
                                "selectors": {"content_type": args.content_type, "notes_contains": args.notes_contains,
                                              "ids_file": bool(args.ids_file), "limit": args.limit, "all": bool(args.all)},
                                "count": len(rows), "ids": [r["source_id"] for r in rows]}) + "\n")
        result["changed"], result["log"] = len(rows), str(log_path)
    out(result)


def cmd_requeue(args):
    bulk(args, "requeue")


def cmd_skip(args):
    bulk(args, "skip")


def cmd_summary(_args):
    conn = connect()
    by_status = {s: 0 for s in STATUSES}
    for r in conn.execute("SELECT status, COUNT(*) c FROM entries GROUP BY status"):
        by_status[r["status"]] = r["c"]
    by_type: dict[str, dict[str, int]] = {}
    for r in conn.execute("SELECT content_type, status, COUNT(*) c FROM entries GROUP BY content_type, status"):
        by_type.setdefault(r["content_type"], {})[r["status"]] = r["c"]
    retried = [
        dict(r)
        for r in conn.execute(
            "SELECT source_id, content_type, status, retry_count, notes FROM entries "
            "WHERE retry_count > 0 ORDER BY retry_count DESC, source_id"
        )
    ]
    out({
        "total": sum(by_status.values()),
        "by_status": by_status,
        "by_content_type": by_type,
        "retried": retried,
        "needs_human": [r for r in retried if r["status"] == "needs_human"]
        + [
            dict(r)
            for r in conn.execute(
                "SELECT source_id, content_type, status, retry_count, notes FROM entries "
                "WHERE status='needs_human' AND retry_count = 0"
            )
        ],
    })


def cmd_log_write(args):
    log_path = writes_log()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    line = {"timestamp": now(), "source_id": args.source_id, "target_id": args.target_id, "action": args.action}
    with log_path.open("a") as f:
        f.write(json.dumps(line) + "\n")
    out(line)


def positive_int(text: str) -> int:
    n = int(text)
    if n < 1:
        raise argparse.ArgumentTypeError("must be 1 or more (a limit of 0 or less would mean 'no limit')")
    return n


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("seed"); s.add_argument("--file", required=True); s.set_defaults(fn=cmd_seed)
    s = sub.add_parser("claim")
    s.add_argument("--status", required=True)
    s.add_argument("--limit", type=positive_int, default=10)
    s.add_argument("--content-type")
    s.set_defaults(fn=cmd_claim)
    s = sub.add_parser("release-stale"); s.add_argument("--minutes", type=int, default=30); s.set_defaults(fn=cmd_release_stale)
    s = sub.add_parser("set")
    s.add_argument("id"); s.add_argument("--status", required=True)
    s.add_argument("--target"); s.add_argument("--notes")
    s.add_argument("--expect-status", help="only change the row if it currently has this status (compare-and-set)")
    s.set_defaults(fn=cmd_set)
    s = sub.add_parser("get"); s.add_argument("id"); s.set_defaults(fn=cmd_get)
    s = sub.add_parser("list")
    s.add_argument("--status"); s.add_argument("--content-type")
    s.set_defaults(fn=cmd_list)
    def bulk_args(s):
        s.add_argument("--from", dest="from_status", help="comma-separated statuses to act on")
        s.add_argument("--content-type"); s.add_argument("--notes-contains"); s.add_argument("--ids-file")
        s.add_argument("--limit", type=positive_int); s.add_argument("--all", action="store_true")
        s.add_argument("--apply", action="store_true", help="make the change (default: dry run)")
    s = sub.add_parser("requeue"); bulk_args(s)
    s.add_argument("--keep-retries", action="store_true", help="keep retry_count (default: reset to 0)")
    s.set_defaults(fn=cmd_requeue)
    s = sub.add_parser("skip"); bulk_args(s)
    s.add_argument("--reason", required=True, help="why these entries are deliberately not migrated")
    s.set_defaults(fn=cmd_skip)
    s = sub.add_parser("summary"); s.set_defaults(fn=cmd_summary)
    s = sub.add_parser("log-write")
    s.add_argument("--source-id", required=True); s.add_argument("--target-id"); s.add_argument("--action", required=True)
    s.set_defaults(fn=cmd_log_write)

    args = p.parse_args(argv)
    args.fn(args)


def run(argv: list) -> dict:
    """In-process equivalent of the CLI: returns the JSON the command prints; raises RuntimeError with the error text if it fails.
    Avoids a Python process start (~25 ms) per call, which adds up to hours at tens of thousands of entries."""
    buf, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
        try:
            main([str(a) for a in argv])
        except SystemExit as e:
            if e.code not in (0, None):
                raise RuntimeError(err.getvalue().strip() or f"manifest {' '.join(map(str, argv[:2]))} failed") from None
    text = buf.getvalue().strip()
    return json.loads(text) if text else {}


if __name__ == "__main__":
    main()
