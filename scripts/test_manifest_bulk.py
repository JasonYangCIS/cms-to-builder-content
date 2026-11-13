"""Tests for the manifest's `skipped` status, the old-database migration, and the bulk `requeue` / `skip` commands.

All tests use temporary databases (MANIFEST_DB) and a temporary changes log; the real state/ is never touched.
Run: python3 -m unittest scripts/test_manifest_bulk.py -v
"""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = str(ROOT / "scripts" / "manifest.py")

OLD_SCHEMA = """
CREATE TABLE entries (
    source_id TEXT PRIMARY KEY, content_type TEXT NOT NULL, target_id TEXT,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','in_progress','migrated','verified','failed','needs_human')),
    retry_count INTEGER NOT NULL DEFAULT 0, claimed_at TEXT, claimed_from TEXT, notes TEXT, updated_at TEXT NOT NULL);
CREATE INDEX idx_entries_status ON entries(status);
"""


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        d = Path(self.tmp.name)
        self.env = {**os.environ, "MANIFEST_DB": str(d / "m.db"), "WRITES_LOG": str(d / "w.jsonl"), "CHANGES_LOG": str(d / "changes.jsonl")}
        self.changes = d / "changes.jsonl"

    def run_cli(self, *args, stdin=None, ok=True):
        p = subprocess.run([sys.executable, MANIFEST, *args], input=stdin, text=True, capture_output=True, env=self.env)
        if ok:
            self.assertEqual(p.returncode, 0, p.stderr)
            return json.loads(p.stdout) if p.stdout.strip() else {}
        self.assertNotEqual(p.returncode, 0, "expected the command to fail")
        return p.stderr

    def seed(self, rows):
        self.run_cli("seed", "--file", "-", stdin=json.dumps([{"source_id": s, "content_type": ct} for s, ct in rows]))

    def row(self, sid):
        return self.run_cli("get", sid)

    def make(self, sid, ct, status, notes="", retries=0):
        """Put a row in a given state through the public CLI (failed x N for the retry count)."""
        self.seed([(sid, ct)])
        if status == "failed":
            for _ in range(retries or 1):
                self.run_cli("set", sid, "--status", "failed", "--notes", notes)
        else:
            self.run_cli("set", sid, "--status", status, "--notes", notes)


class Migration(Base):
    def test_old_database_is_migrated_in_place_and_keeps_every_row(self):
        con = sqlite3.connect(self.env["MANIFEST_DB"])  # a temp database standing in for a pre-`skipped` manifest
        con.executescript(OLD_SCHEMA)
        con.executemany("INSERT INTO entries VALUES (?,?,?,?,?,?,?,?,?)",
                        [("a", "page", "B1", "verified", 0, None, None, "ok", "t"), ("b", "page", None, "needs_human", 4, None, None, "note b", "t"),
                         ("c", "hero", "B3", "migrated", 1, None, None, None, "t")])
        con.commit()
        con.close()
        s = self.run_cli("summary")  # any command triggers the migration
        self.assertEqual(s["total"], 3)
        self.assertEqual((s["by_status"]["verified"], s["by_status"]["needs_human"], s["by_status"]["skipped"]), (1, 1, 0))
        b = self.row("b")
        self.assertEqual((b["retry_count"], b["notes"], b["status"]), (4, "note b", "needs_human"))
        self.run_cli("set", "b", "--status", "skipped")  # the old CHECK would have rejected this
        self.assertEqual(self.row("b")["status"], "skipped")
        con = sqlite3.connect(self.env["MANIFEST_DB"])
        self.assertIn("idx_entries_status", [r[0] for r in con.execute("select name from sqlite_master where type='index'")])
        self.assertNotIn("entries_old", [r[0] for r in con.execute("select name from sqlite_master")])
        con.close()

    def test_migration_is_idempotent(self):
        self.seed([("a", "page")])
        for _ in range(3):
            self.assertEqual(self.run_cli("summary")["total"], 1)

    def test_fresh_database_accepts_skipped(self):
        self.seed([("a", "page")])
        self.run_cli("set", "a", "--status", "skipped", "--notes", "x")
        self.assertEqual(self.run_cli("summary")["by_status"]["skipped"], 1)


class Requeue(Base):
    def test_requeue_resets_retries_and_keeps_the_old_note(self):
        self.make("p1", "page", "failed", "verify: boom", retries=4)  # 4th failure escalates to needs_human
        r = self.row("p1")
        self.assertEqual((r["status"], r["retry_count"]), ("needs_human", 4))
        res = self.run_cli("requeue", "--content-type", "page", "--apply")
        self.assertEqual((res["matched"], res["changed"]), (1, 1))
        r = self.row("p1")
        self.assertEqual((r["status"], r["retry_count"]), ("pending", 0))
        self.assertIn("requeued", r["notes"])
        self.assertIn("verify: boom", r["notes"])
        self.assertIsNone(r["claimed_at"])

    def test_requeued_row_gets_a_fresh_retry_budget_but_plain_set_pending_does_not(self):
        self.make("a", "page", "failed", "x", retries=4)
        self.make("b", "page", "failed", "x", retries=4)
        self.run_cli("requeue", "--ids-file", "-", "--apply", stdin="a\n")  # a: requeue
        self.run_cli("set", "b", "--status", "pending")  # b: the old way
        self.run_cli("set", "a", "--status", "failed", "--notes", "again")
        self.run_cli("set", "b", "--status", "failed", "--notes", "again")
        self.assertEqual(self.row("a")["status"], "failed")        # retry 1 of 3
        self.assertEqual(self.row("b")["status"], "needs_human")   # retry 5: escalated again immediately

    def test_keep_retries(self):
        self.make("a", "page", "failed", "x", retries=4)
        self.run_cli("requeue", "--content-type", "page", "--keep-retries", "--apply")
        self.assertEqual(self.row("a")["retry_count"], 4)

    def test_dry_run_is_the_default_and_changes_nothing(self):
        self.make("a", "page", "needs_human", "x")
        res = self.run_cli("requeue", "--content-type", "page")
        self.assertEqual((res["dry_run"], res["matched"], res["changed"]), (True, 1, 0))
        self.assertEqual(self.row("a")["status"], "needs_human")
        self.assertFalse(self.changes.exists())

    def test_selectors_combine(self):
        self.make("u1", "page", "needs_human", "URL /a already exists in Builder")
        self.make("u2", "page", "needs_human", "URL /b ALREADY EXISTS in Builder")
        self.make("o1", "page", "needs_human", "target is archived")
        self.make("h1", "hero", "needs_human", "URL /c already exists in Builder")
        res = self.run_cli("requeue", "--notes-contains", "already exists", "--content-type", "page", "--apply")  # case-insensitive
        self.assertEqual(sorted(res["sample"]), ["u1", "u2"])
        self.assertEqual({s: self.row(s)["status"] for s in ("u1", "u2", "o1", "h1")}, {"u1": "pending", "u2": "pending", "o1": "needs_human", "h1": "needs_human"})
        res = self.run_cli("requeue", "--content-type", "page", "--limit", "1", "--apply")
        self.assertEqual(res["changed"], 1)

    def test_ids_file_accepts_worklist_csv_json_and_lines(self):
        for sid in ("a", "b", "c", "d"):
            self.make(sid, "page", "needs_human", "x")
        csv_text = "group,severity,source_id,reason\ng,destructive,a,\"has, comma\"\ng,destructive,'b,x\n"
        res = self.run_cli("requeue", "--ids-file", "-", "--apply", stdin=csv_text)
        self.assertEqual(sorted(self.run_cli("list", "--status", "pending")["rows"][i]["source_id"] for i in range(2)), ["a", "b"])  # apostrophe stripped
        self.assertEqual(res["changed"], 2)
        res = self.run_cli("requeue", "--ids-file", "-", "--apply", stdin=json.dumps([{"source_id": "c"}, "zzz"]))
        self.assertEqual((res["changed"], res["ids_not_matched"]), (1, ["zzz"]))
        res = self.run_cli("requeue", "--ids-file", "-", "--apply", stdin="# comment\nd\n")
        self.assertEqual(res["changed"], 1)

    def test_refuses_to_act_on_everything_without_all(self):
        self.make("a", "page", "needs_human", "x")
        err = self.run_cli("requeue", "--apply", ok=False)
        self.assertIn("--all", err)
        self.assertEqual(self.run_cli("requeue", "--all", "--apply")["changed"], 1)

    def test_only_stuck_rows_can_be_requeued(self):
        for sid, st in (("v", "verified"), ("m", "migrated"), ("p", "pending")):
            self.make(sid, "page", st, "x")
        err = self.run_cli("requeue", "--from", "verified,migrated", "--all", "--apply", ok=False)
        self.assertIn("can only act on rows that are", err)
        self.assertEqual(self.run_cli("requeue", "--all", "--apply")["matched"], 0)  # default from=needs_human: nothing to do
        self.assertEqual(self.row("v")["status"], "verified")

    def test_requeued_rows_are_claimable_again(self):
        self.make("a", "page", "needs_human", "x")
        self.run_cli("requeue", "--all", "--apply")
        self.assertEqual([r["source_id"] for r in self.run_cli("claim", "--status", "pending", "--limit", "5")["claimed"]], ["a"])

    def test_bulk_change_is_audited(self):
        self.make("a", "page", "needs_human", "x")
        self.run_cli("requeue", "--notes-contains", "x", "--apply")
        line = json.loads(self.changes.read_text().splitlines()[-1])
        self.assertEqual((line["action"], line["count"], line["ids"], line["selectors"]["notes_contains"]), ("requeue", 1, ["a"], "x"))


class Skip(Base):
    def test_skip_requires_a_reason_and_records_it(self):
        self.make("a", "page", "needs_human", "URL /x already exists in Builder")
        self.assertIn("--reason", self.run_cli("skip", "--all", "--apply", ok=False))
        self.assertIn("--reason", self.run_cli("skip", "--reason", "  ", "--all", "--apply", ok=False))
        res = self.run_cli("skip", "--reason", "legacy campaign pages", "--content-type", "page", "--apply")
        self.assertEqual(res["changed"], 1)
        r = self.row("a")
        self.assertEqual(r["status"], "skipped")
        self.assertTrue(r["notes"].startswith("[skipped: legacy campaign pages]"))
        self.assertIn("already exists", r["notes"])  # the original reason is kept

    def test_skipped_rows_are_not_claimed_and_can_be_restored(self):
        self.make("a", "page", "pending")
        self.run_cli("skip", "--reason", "r", "--all", "--apply")
        self.assertEqual(self.run_cli("claim", "--status", "pending", "--limit", "5")["claimed"], [])
        self.run_cli("requeue", "--from", "skipped", "--all", "--apply")
        self.assertEqual(self.row("a")["status"], "pending")

    def test_skip_only_acts_on_unmigrated_rows(self):
        self.make("v", "page", "verified")
        self.make("m", "page", "migrated")
        self.assertIn("can only act on rows that are", self.run_cli("skip", "--from", "verified", "--reason", "r", "--all", "--apply", ok=False))
        self.assertEqual(self.run_cli("skip", "--reason", "r", "--all", "--apply")["matched"], 0)
        self.assertEqual((self.row("v")["status"], self.row("m")["status"]), ("verified", "migrated"))

    def test_skip_dry_run_and_summary_counts(self):
        self.make("a", "page", "pending")
        self.make("b", "page", "failed", "x")
        res = self.run_cli("skip", "--reason", "r", "--all")
        self.assertEqual((res["dry_run"], res["matched"]), (True, 2))
        self.run_cli("skip", "--reason", "r", "--all", "--apply")
        self.assertEqual(self.run_cli("summary")["by_status"]["skipped"], 2)


class Guards(Base):
    """Compare-and-set and the transition guards that stop a stale or confused worker from corrupting state."""

    def test_expect_status_blocks_a_stale_worker_from_overwriting_the_new_owner(self):
        self.seed([("a", "page")])
        self.run_cli("claim", "--status", "pending", "--limit", "1")               # worker 1 claims
        self.run_cli("set", "a", "--status", "pending", "--expect-status", "in_progress")  # ...is released/failed over
        self.run_cli("claim", "--status", "pending", "--limit", "1")               # worker 2 claims
        self.run_cli("set", "a", "--status", "migrated", "--expect-status", "in_progress")  # worker 2 finishes
        err = self.run_cli("set", "a", "--status", "verified", "--expect-status", "in_progress", ok=False)  # worker 1 is late
        self.assertIn("nothing changed", err)
        self.assertEqual(self.row("a")["status"], "migrated")

    def test_expect_status_rejects_unknown_status_and_keeps_old_behaviour_without_it(self):
        self.seed([("a", "page")])
        self.run_cli("set", "a", "--status", "migrated", "--expect-status", "nonsense", ok=False)
        self.run_cli("set", "a", "--status", "migrated")  # no --expect-status: unchanged behaviour
        self.assertEqual(self.row("a")["status"], "migrated")

    def test_set_cannot_fake_a_claim(self):
        self.seed([("a", "page")])
        self.assertIn("claim", self.run_cli("set", "a", "--status", "in_progress", ok=False))
        self.assertEqual(self.row("a")["status"], "pending")

    def test_stuck_rows_cannot_jump_straight_to_verified(self):
        for sid, st in (("n", "needs_human"), ("f", "failed"), ("s", "skipped")):
            self.make(sid, "page", st, "x")
            self.assertIn("cannot go straight to verified", self.run_cli("set", sid, "--status", "verified", ok=False))
            self.assertNotEqual(self.row(sid)["status"], "verified")
        self.make("m", "page", "migrated")
        self.run_cli("set", "m", "--status", "verified")  # the normal path still works
        self.assertEqual(self.row("m")["status"], "verified")

    def test_release_stale_also_frees_rows_that_have_no_claim_timestamp(self):
        self.seed([("a", "page")])
        con = sqlite3.connect(self.env["MANIFEST_DB"])  # a temp database: simulate a row left in_progress without claimed_at
        con.execute("UPDATE entries SET status='in_progress', claimed_from='pending', claimed_at=NULL WHERE source_id='a'")
        con.commit()
        con.close()
        res = self.run_cli("release-stale", "--minutes", "30")
        self.assertEqual([r["source_id"] for r in res["released"]], ["a"])
        self.assertEqual(self.row("a")["status"], "pending")

    def test_limits_must_be_positive(self):
        self.seed([("a", "page")])
        self.run_cli("set", "a", "--status", "needs_human")
        for args in (("claim", "--status", "pending", "--limit", "0"), ("claim", "--status", "pending", "--limit", "-1"),
                     ("requeue", "--all", "--limit", "0", "--apply"), ("skip", "--reason", "r", "--all", "--limit", "-5", "--apply")):
            self.assertIn("1 or more", self.run_cli(*args, ok=False))
        self.assertEqual(self.row("a")["status"], "needs_human")  # nothing was acted on

    def test_seed_rejects_malformed_items_cleanly(self):
        err = self.run_cli("seed", "--file", "-", stdin=json.dumps(["just-a-string"]), ok=False)
        self.assertIn("each item needs source_id and content_type", err)
        self.assertNotIn("Traceback", err)
        self.assertEqual(self.run_cli("summary")["total"], 0)

    def test_ids_file_keeps_a_leading_apostrophe_in_plain_lists(self):
        self.seed([("'weird", "page"), ("plain", "page")])
        self.run_cli("set", "'weird", "--status", "needs_human")
        res = self.run_cli("requeue", "--ids-file", "-", "--apply", stdin="'weird\n")  # not a CSV: the id is taken literally
        self.assertEqual(res["changed"], 1)


class InProcess(Base):
    """manifest.run() is the same code path as the CLI without a process start per call."""

    def setUp(self):
        super().setUp()
        from unittest import mock
        patcher = mock.patch.dict(os.environ, {k: self.env[k] for k in ("MANIFEST_DB", "WRITES_LOG", "CHANGES_LOG")})
        patcher.start()
        self.addCleanup(patcher.stop)
        sys.path.insert(0, str(ROOT / "scripts"))
        import manifest
        self.mf = manifest

    def test_run_returns_the_same_json_as_the_cli_and_uses_the_environment_database(self):
        seed_file = Path(self.tmp.name) / "seed.json"
        seed_file.write_text(json.dumps([{"source_id": "a", "content_type": "page"}]))
        self.assertEqual(self.mf.run(["seed", "--file", str(seed_file)]), {"inserted": 1, "already_present": 0})
        self.assertEqual(self.mf.run(["get", "a"])["status"], "pending")
        self.assertEqual(self.row("a")["status"], "pending")  # the CLI sees the same database
        self.assertEqual(self.mf.run(["claim", "--status", "pending", "--limit", "5"])["claimed"][0]["source_id"], "a")

    def test_run_raises_runtimeerror_with_the_message_and_never_exits_the_caller(self):
        with self.assertRaises(RuntimeError) as ctx:
            self.mf.run(["get", "does-not-exist"])
        self.assertIn("unknown source_id", str(ctx.exception))
        with self.assertRaises(RuntimeError):
            self.mf.run(["set", "x", "--status", "bogus"])
        with self.assertRaises(RuntimeError):
            self.mf.run(["claim", "--limit", "0"])  # argparse errors are RuntimeErrors too
        self.assertEqual(self.mf.run(["summary"])["total"], 0)  # and the module still works afterwards

    def test_run_follows_a_changed_database_path_between_calls(self):
        from unittest import mock
        self.mf.run(["summary"])
        other = Path(self.tmp.name) / "other.db"
        with mock.patch.dict(os.environ, {"MANIFEST_DB": str(other)}):
            self.assertEqual(self.mf.run(["summary"])["total"], 0)
            self.assertTrue(other.exists())

    def test_run_is_much_faster_than_a_subprocess_per_call(self):
        import time
        self.seed([("a", "page")])
        t0 = time.time()
        for _ in range(50):
            self.mf.run(["get", "a"])
        in_process = (time.time() - t0) / 50
        t0 = time.time()
        for _ in range(5):
            self.row("a")
        subprocess_cost = (time.time() - t0) / 5
        self.assertLess(in_process * 3, subprocess_cost)


if __name__ == "__main__":
    unittest.main()
