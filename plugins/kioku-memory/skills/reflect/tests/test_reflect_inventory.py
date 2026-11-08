"""Tests for reflect_inventory.py: the estimate, recording a run, and topic clusters. Run: python3 -m unittest discover -s tests"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "reflect_inventory.py"
spec = importlib.util.spec_from_file_location("reflect_inventory", SCRIPT)
ri = importlib.util.module_from_spec(spec)
sys.modules["reflect_inventory"] = ri
spec.loader.exec_module(ri)


def decision(ident: str, tags: list) -> str:
    return (f"---\ntype: Decision\nid: {ident}\ndecision_status: active\ntags: [{', '.join(tags)}]\n---\n"
            f"# Decision\nRefunds retry with the stored amount, see [the topic](/payments-kb/refunds.md).\n")


class EstimateTest(unittest.TestCase):
    def test_no_recorded_run_asks_first(self) -> None:
        self.assertEqual(ri.estimate([], 3, 4), {"agents": 16, "tokens": None, "tokens_per_agent": None,
                                                 "from_runs": 0, "ask_first": True})

    def test_recorded_runs_give_tokens_per_agent_and_agents_per_topic(self) -> None:
        runs = [{"agents": 5, "tokens": 700_000, "topics": 1}, {"agents": 11, "tokens": 1_500_000, "topics": 2}]
        got = ri.estimate(runs, 2, 4)
        self.assertEqual(got["tokens_per_agent"], round(2_200_000 / 16))
        self.assertEqual(got["agents"], 11)  # 16 agents over 3 topics, for 2 topics
        self.assertEqual(got["tokens"], round(11 * 2_200_000 / 16))
        self.assertTrue(got["ask_first"], "above 1.5M tokens")
        self.assertFalse(ri.estimate(runs, 1, 4)["ask_first"])

    def test_nothing_to_check_costs_nothing(self) -> None:
        self.assertEqual(ri.estimate([], 0, 4)["ask_first"], False)


class InventoryTest(unittest.TestCase):
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        bundle = self.root / "acme-kb"
        self.kb = bundle / "payments-kb"
        (self.kb / "decisions").mkdir(parents=True)
        (bundle / "index.md").write_text('---\nokf_version: "0.2"\n---\n')
        for topic in ("refunds", "team-only"):
            (self.kb / f"{topic}.md").write_text(f"# {topic}\n")
        (self.kb / "decisions" / "DEC-1-stored-amount.md").write_text(decision("DEC-1", ["private-source", "refunds"]))
        (self.kb / "decisions" / "DEC-2-retry-window.md").write_text(decision("DEC-2", ["team-only", "refunds"]))
        self.state = self.root / "state" / "work-payments.json"
        git = ["git", "-C", str(bundle), "-c", "user.name=t", "-c", "user.email=t@example.com"]
        for command in (["init", "-q"], ["add", "-A"], ["commit", "-q", "-m", "bundle"]):
            subprocess.run(git + command, check=True, capture_output=True)

    def run_cli(self, *args: str) -> str:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(ri.main(list(args)), 0)
        return out.getvalue()

    def inventory(self, *extra: str) -> dict:
        target = self.root / "inventory.json"
        self.run_cli("--kb", str(self.kb), "--out", str(target), *extra)
        return json.loads(target.read_text())

    def test_ignored_tags_are_never_topics(self) -> None:
        clusters = {c["name"]: len(c["decisions"]) for c in self.inventory()["clusters"]}
        self.assertEqual(clusters, {"refunds": 1, "team-only": 1})
        clusters = {c["name"]: len(c["decisions"]) for c in self.inventory("--ignore-tags", "team-only")["clusters"]}
        self.assertEqual(clusters, {"refunds": 2})

    def test_a_recorded_run_feeds_the_next_estimate(self) -> None:
        self.assertTrue(self.inventory("--state", str(self.state))["estimate"]["ask_first"])
        printed = self.run_cli("--mark", str(self.state), "--kb", str(self.kb), "--topics", "refunds,team-only",
                               "--stats", '{"agents": 6, "tokens": 600000}')
        self.assertIn("recorded a run of 6 agents and 600000 tokens", printed)
        recorded = json.loads(self.state.read_text())
        self.assertEqual(sorted(recorded["topics"]), ["refunds", "team-only"])
        self.assertEqual(recorded["runs"][0]["topics"], 2)
        since = self.inventory("--state", str(self.state))
        self.assertEqual((since["clusters"], sorted(since["unchanged_topics"])), ([], ["refunds", "team-only"]))
        self.assertEqual(since["estimate"]["agents"], 0)
        every = self.inventory("--state", str(self.state), "--all")
        self.assertEqual(len(every["clusters"]), 2)
        estimate = every["estimate"]
        self.assertEqual((estimate["agents"], estimate["tokens_per_agent"], estimate["ask_first"]), (6, 100_000, False))

    def test_markdown_notes_record_their_cost_without_a_bundle(self) -> None:
        notes = self.root / "notes"
        notes.mkdir()
        (notes / "a.md").write_text("---\ndomain: personal/notes\n---\nA fact.\n")
        self.run_cli("--mark", str(self.state), "--stats", '{"agents": 3, "tokens": 90000}')
        target = self.root / "notes-inventory.json"
        self.run_cli("--notes", str(notes), "--domain", "personal/notes", "--state", str(self.state), "--out", str(target))
        estimate = json.loads(target.read_text())["estimate"]
        self.assertEqual((estimate["tokens_per_agent"], estimate["agents"]), (30_000, 6))  # 1 check, 4 verifications, 1 report

    def test_bad_stats_are_refused(self) -> None:
        for bad in ('{"agents": 3}', '{"agents": "3", "tokens": 1}', "nope", '{"agents": true, "tokens": 1}'):
            with self.assertRaises(SystemExit):
                ri.parse_stats(bad)


if __name__ == "__main__":
    unittest.main()
