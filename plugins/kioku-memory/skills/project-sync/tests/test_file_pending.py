"""Tests for file_pending.py. Run: python3 -m unittest discover -s tests"""

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "file_pending.py"
PENDING_CLI = Path(__file__).resolve().parents[2] / "checkpoint" / "scripts" / "kioku_pending.py"
DOMAIN = "personal/demo"

# A stand-in for `kioku_pending.py`: records each call with its detail file's
# content, prints a new id, and fails when FAKE_PENDING_FAIL is set.
FAKE_CLI = textwrap.dedent("""\
    import json, os, sys
    args = sys.argv[1:]
    call = {"args": args}
    if "--detail-file" in args:
        with open(args[args.index("--detail-file") + 1]) as f:
            call["detail"] = json.load(f)
    with open(os.environ["FAKE_PENDING_LOG"], "a") as log:
        log.write(json.dumps(call) + "\\n")
    if os.environ.get("FAKE_PENDING_FAIL"):
        print("pending: the store is busy", file=sys.stderr)
        sys.exit(1)
    with open(os.environ["FAKE_PENDING_LOG"]) as log:
        print("p%d" % sum(1 for _ in log))
""")

# A stand-in for `retrieval_check.py compare --format json`.
FAKE_CHECK = textwrap.dedent("""\
    import json
    print(json.dumps({"variant": "hybrid+graph", "queries": {"before": 2, "after": 2}, "headline": {},
                      "worse": [{"id": "q1", "query": "how do tokens expire", "ndcg_before": 0.9, "ndcg_after": 0.4,
                                 "delta": -0.5}], "better": [], "stale": [], "regressed": True}))
""")


def needs(action: str, key: str, kind: str, fingerprint: str) -> dict:
    return {"action": action, "key": key, "domain": DOMAIN, "title": f"Title {key}", "body": f"Body {key}.",
            "pending": {"kind": kind, "id": fingerprint, "text": f"{action} {key}: waits because it must"}}


class FilePendingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.folder = tempfile.TemporaryDirectory()
        self.dir = Path(self.folder.name)
        (self.dir / "home").mkdir()
        (self.dir / "cli.py").write_text(FAKE_CLI)
        (self.dir / "check.py").write_text(FAKE_CHECK)
        self.log = self.dir / "calls.jsonl"
        self.store = self.dir / "pending"
        self.store.mkdir()
        self.env = {**{k: v for k, v in os.environ.items() if k != "CLAUDE_PLUGIN_DATA"},
                    "HOME": str(self.dir / "home"), "KIOKU_PENDING_DIR": str(self.store),
                    "KIOKU_PENDING_CLI": str(self.dir / "cli.py"), "KIOKU_RETRIEVAL_CHECK": str(self.dir / "check.py"),
                    "FAKE_PENDING_LOG": str(self.log)}

    def tearDown(self) -> None:
        self.folder.cleanup()

    def run_filer(self, *extra: str, env: dict = None) -> tuple:
        proc = subprocess.run([sys.executable, "-I", str(SCRIPT), "--vault", "personal", "--domain", DOMAIN,
                               "--skill", "project-sync", "--date", "2026-11-07", *extra],
                              capture_output=True, text=True, env=env or self.env)
        self.assertIn(proc.returncode, (0, 1), proc.stderr)
        return proc.returncode, json.loads(proc.stdout)

    def calls(self) -> list:
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def write_result(self, value: dict) -> str:
        path = self.dir / "result.json"
        path.write_text(json.dumps(value))
        return str(path)

    def test_files_needs_user_and_skipped_entries_with_their_detail(self) -> None:
        update = needs("update", "auth-ttl", "decision", f"project-sync:update:{DOMAIN}/auth-ttl")
        skipped = {**needs("new", "fresh", "other", f"project-sync:skipped:{DOMAIN}/fresh:2026-11-07"),
                   "skip_reason": "exists"}
        older = {"action": "contradiction", "key": "old-format", "domain": DOMAIN}
        result = self.write_result({"needs_user": [update, older], "skipped": [skipped], "counts": {}})
        code, summary = self.run_filer("--result", result)
        self.assertEqual(code, 0)
        self.assertEqual([f["id"] for f in summary["filed"]], ["p1", "p2", "p3"])
        first, second, third = self.calls()
        args = first["args"]
        self.assertEqual(args[0], "add")
        self.assertEqual(args[args.index("--kind") + 1], "decision")
        self.assertEqual(args[args.index("--source") + 1], "project-sync 2026-11-07")
        self.assertEqual(args[args.index("--text") + 1],
                         f"update auth-ttl: waits because it must [project-sync:update:{DOMAIN}/auth-ttl]")
        self.assertEqual(first["detail"], update)
        self.assertEqual(second["args"][second["args"].index("--kind") + 1], "other")
        self.assertTrue(second["args"][second["args"].index("--text") + 1]
                        .endswith(f"[project-sync:contradiction:{DOMAIN}/old-format]"))
        self.assertEqual(third["detail"]["skip_reason"], "exists")

    def test_an_item_already_in_the_store_open_or_done_is_not_filed_again(self) -> None:
        def item(ident: str, fingerprint: str, status: str, domain: str = DOMAIN) -> dict:
            return {"id": ident, "kind": "decision", "domain": domain, "source": "project-sync 2026-11-01",
                    "text": f"something [{fingerprint}]", "created": "2026-11-01T00:00:00Z", "status": status}
        (self.store / "personal.json").write_text(json.dumps({"version": 1, "next": 4, "items": [
            item("p1", f"project-sync:update:{DOMAIN}/resolved", "done"),
            item("p2", f"project-sync:update:{DOMAIN}/waiting", "open"),
            item("p3", "project-sync:update:other/elsewhere", "done", domain="personal/other"),
        ]}))
        result = self.write_result({"needs_user": [
            needs("update", key, "decision", f"project-sync:update:{DOMAIN}/{key}")
            for key in ("resolved", "waiting", "elsewhere", "new-one")]})
        code, summary = self.run_filer("--result", result)
        self.assertEqual(code, 0)
        self.assertEqual(summary["already_pending"],
                         [f"project-sync:update:{DOMAIN}/resolved", f"project-sync:update:{DOMAIN}/waiting"])
        filed = [c["args"][c["args"].index("--text") + 1] for c in self.calls()]
        self.assertEqual(len(filed), 2)
        self.assertTrue(filed[0].endswith("/elsewhere]"))
        self.assertTrue(filed[1].endswith("/new-one]"))

    def test_regressions_are_filed_from_the_comparison(self) -> None:
        before, after = self.dir / "before.json", self.dir / "after.json"
        before.write_text("{}")
        after.write_text("{}")
        code, summary = self.run_filer("--before", str(before), "--after", str(after))
        self.assertEqual(code, 0)
        call = self.calls()[0]
        self.assertEqual(call["args"][call["args"].index("--kind") + 1], "regression")
        self.assertEqual(call["args"][call["args"].index("--text") + 1],
                         'Retrieval got worse after project-sync on 2026-11-07: "how do tokens expire" (nDCG 0.90 to 0.40) '
                         f"[project-sync:regression:{DOMAIN}:q1:2026-11-07]")
        self.assertEqual(call["detail"]["query"]["id"], "q1")

    def test_items_the_list_refuses_are_kept_in_a_file(self) -> None:
        result = self.write_result({"needs_user": [needs("update", "k", "decision", f"project-sync:update:{DOMAIN}/k")]})
        code, summary = self.run_filer("--result", result, env={**self.env, "FAKE_PENDING_FAIL": "1"})
        self.assertEqual(code, 1)
        self.assertEqual(summary["failed"], 1)
        self.assertIn("busy", summary["error"])
        kept = json.loads(Path(summary["unfiled"]).read_text())
        self.assertEqual(kept[0]["detail"]["key"], "k")
        self.assertTrue(summary["unfiled"].startswith(str(self.dir / "home")))

    def test_with_the_real_pending_cli_a_resolved_item_stays_resolved(self) -> None:
        env = {k: v for k, v in self.env.items() if k != "KIOKU_PENDING_CLI"}
        result = self.write_result({"needs_user": [needs("update", "auth-ttl", "decision",
                                                         f"project-sync:update:{DOMAIN}/auth-ttl")]})
        code, summary = self.run_filer("--result", result, env=env)
        self.assertEqual(code, 0, summary)
        ident = summary["filed"][0]["id"]
        store = json.loads((self.store / "personal.json").read_text())
        self.assertEqual(store["items"][0]["kind"], "decision")
        self.assertTrue(Path(store["items"][0]["detail"]).is_file(), "the CLI copied the detail into its store")
        done = subprocess.run([sys.executable, "-I", str(PENDING_CLI), "done", "--vault", "personal", ident,
                               "--resolution", "kept the old rule"], capture_output=True, text=True, env=env)
        self.assertEqual(done.returncode, 0, done.stderr)
        code, again = self.run_filer("--result", result, env=env)
        self.assertEqual((code, again["filed"], again["already_pending"]),
                         (0, [], [f"project-sync:update:{DOMAIN}/auth-ttl"]))


if __name__ == "__main__":
    unittest.main()
