"""Tests for kioku_pending.py, the pending list both plugins vendor. Run: python3 -m unittest discover -s tests"""

from __future__ import annotations

import contextlib
import fcntl
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parent.parent / "kioku_pending.py"
spec = importlib.util.spec_from_file_location("kioku_pending", SCRIPT)
kp = importlib.util.module_from_spec(spec)
sys.modules["kioku_pending"] = kp  # dataclasses look their module up while the class is built
spec.loader.exec_module(kp)


class PendingCliTest(unittest.TestCase):
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.store_dir = self.root / "pending"
        patcher = mock.patch.dict(os.environ, {"KIOKU_PENDING_DIR": str(self.store_dir)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_cli(self, *args: str) -> tuple:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = kp.main(list(args))
        return code, out.getvalue(), err.getvalue()

    def add(self, text: str, vault: str = "personal", kind: str = "retire", *extra: str) -> str:
        code, out, err = self.run_cli("add", "--vault", vault, "--domain", "personal/app", "--kind", kind,
                                      "--source", "dream 2026-11-07", "--text", text, *extra)
        self.assertEqual(code, 0, err)
        return out.strip()

    def test_add_list_and_done(self) -> None:
        self.assertEqual(self.add("old-key (personal/app), replaced by new-key"), "p1")
        self.assertEqual(self.add("old-key  (personal/app),\nreplaced by new-key"), "p1")  # the same item
        self.assertEqual(self.add("DEC-4 narrowed", kind="kb"), "p2")
        code, out, _ = self.run_cli("list", "--vault", "personal", "--json")
        items = json.loads(out)
        self.assertEqual([(i["id"], i["kind"], i["status"]) for i in items], [("p1", "retire", "open"), ("p2", "kb", "open")])
        code, out, _ = self.run_cli("list", "--vault", "personal")
        self.assertIn("p1 [retire, personal/app] old-key (personal/app), replaced by new-key (dream 2026-11-07", out)
        code, out, _ = self.run_cli("done", "--vault", "personal", "p1", "--resolution", "retired old-key")
        self.assertEqual((code, out.strip()), (0, "p1 done: retired old-key"))
        _, out, _ = self.run_cli("list", "--vault", "personal", "--json")
        self.assertEqual([i["id"] for i in json.loads(out)], ["p2"])
        self.assertEqual(self.add("old-key (personal/app), replaced by new-key"), "p3")  # done items don't absorb
        stored = json.loads((self.store_dir / "personal.json").read_text())
        self.assertEqual(stored["items"][0]["resolution"], "retired old-key")
        self.assertEqual(list(self.store_dir.glob(".*.tmp")), [])

    def test_vaults_never_mix(self) -> None:
        self.add("personal item")
        self.add("work item", vault="work")
        _, out, _ = self.run_cli("list", "--vault", "work", "--json")
        self.assertEqual([i["text"] for i in json.loads(out)], ["work item"])
        self.assertTrue((self.store_dir / "work.json").is_file())
        code, _, err = self.run_cli("list", "--vault", "../personal")
        self.assertEqual(code, 1)
        self.assertIn("not a vault name", err)

    def test_domain_filter_includes_subdomains(self) -> None:
        self.add("one")
        self.run_cli("add", "--vault", "personal", "--domain", "local", "--kind", "other", "--source", "x 2026-11-07",
                     "--text", "two")
        _, out, _ = self.run_cli("list", "--vault", "personal", "--domain", "personal", "--json")
        self.assertEqual([i["text"] for i in json.loads(out)], ["one"])

    def test_detail_files_are_copied_and_checked(self) -> None:
        detail = self.root / "detail.json"
        detail.write_text('{"proposal": {"key": "k", "body": "text"}}\n')
        ident = self.add("with detail", "personal", "decision", "--detail-file", str(detail))
        copied = self.store_dir / "personal" / f"{ident}.json"
        self.assertEqual(copied.read_bytes(), detail.read_bytes())
        _, out, _ = self.run_cli("list", "--vault", "personal", "--json")
        self.assertEqual(json.loads(out)[0]["detail"], str(copied))
        detail.write_text("not json")
        code, _, err = self.run_cli("add", "--vault", "personal", "--domain", "d", "--kind", "other", "--source", "s",
                                    "--text", "bad", "--detail-file", str(detail))
        self.assertEqual(code, 1)
        self.assertIn("not JSON", err)
        self.assertEqual(len(kp.PendingStore("personal").open_items()), 1)

    def test_errors_exit_non_zero(self) -> None:
        code, _, err = self.run_cli("done", "--vault", "personal", "p9")
        self.assertEqual(code, 1)
        self.assertIn("no item p9", err)
        self.assertEqual(self.run_cli("add", "--vault", "personal", "--kind", "nope")[0], 2)

    def test_done_items_older_than_90_days_are_pruned(self) -> None:
        store = kp.PendingStore("personal")
        old, recent = store.add("d", "other", "s", "old one", b"{}"), store.add("d", "other", "s", "recent one")
        store.done(old)
        store.done(recent)
        data = json.loads(store.path.read_text())
        data["items"][0]["done_at"] = (datetime.now(timezone.utc) - timedelta(days=91)).isoformat()
        store.path.write_text(json.dumps(data))
        self.assertTrue((store.details / f"{old}.json").is_file())
        store.add("d", "other", "s", "new one")
        ids = [item.id for item in store.load().items]
        self.assertEqual(ids, [recent, "p3"])
        self.assertFalse((store.details / f"{old}.json").exists())

    def test_a_busy_list_is_an_error_not_a_wait_forever(self) -> None:
        self.store_dir.mkdir(parents=True)
        with (self.store_dir / "personal.lock").open("w") as held, mock.patch.object(kp, "LOCK_SECONDS", 0.1):
            fcntl.flock(held, fcntl.LOCK_EX)
            code, _, err = self.run_cli("add", "--vault", "personal", "--domain", "d", "--kind", "other",
                                        "--source", "s", "--text", "t")
        self.assertEqual(code, 1)
        self.assertIn("busy", err)

    def test_items_resolve_by_their_ref(self) -> None:
        store = kp.PendingStore("personal")
        first = store.add("personal/app", "other", "review 2026-11-07", "left unreviewed", ref="review:abc")
        self.assertEqual(store.add("personal/app", "other", "review 2026-11-08", "still unreviewed", ref="review:abc"),
                         first)
        self.assertEqual(store.resolve("review:abc", "a later review read it"), 1)
        self.assertEqual(store.resolve("review:abc", "again"), 0)
        self.assertEqual(store.load().items[0].resolution, "a later review read it")

    def test_runs_as_a_script_in_isolated_mode(self) -> None:
        env = {**os.environ, "KIOKU_PENDING_DIR": str(self.store_dir)}
        run = subprocess.run([sys.executable, "-I", str(SCRIPT), "add", "--vault", "personal", "--domain", "d",
                              "--kind", "other", "--source", "s", "--text", "from a script"],
                             capture_output=True, text=True, env=env)
        self.assertEqual((run.returncode, run.stdout.strip()), (0, "p1"), run.stderr)
        # Without $KIOKU_PENDING_DIR, the store is under the home folder, where the data has always lived.
        with mock.patch.dict(os.environ, {"HOME": str(self.root / "home")}):
            os.environ.pop("KIOKU_PENDING_DIR")
            self.assertEqual(kp.pending_dir(), self.root / "home" / ".local" / "state" / "kioku" / "pending")


if __name__ == "__main__":
    unittest.main()
