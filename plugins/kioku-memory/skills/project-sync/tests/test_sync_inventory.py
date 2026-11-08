"""Tests for sync_inventory.py. Run: python3 -m unittest discover -s tests"""

import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "sync_inventory.py"
spec = importlib.util.spec_from_file_location("sync_inventory", SCRIPT)
inv = importlib.util.module_from_spec(spec)
sys.modules["sync_inventory"] = inv
spec.loader.exec_module(inv)


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=Test", "-c", "user.email=t@example.com",
                    "-c", "commit.gpgsign=false", "-c", "init.defaultBranch=main", *args],
                   check=True, capture_output=True)


def make_repo(folder: Path) -> Path:
    repo = folder / "app"
    repo.mkdir()
    git(repo, "init", "-q")
    (repo / "a.py").write_text("x = 1\n")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "feat: first", "-m", "Chose X over Y because Z.")
    return repo


def write(path: Path, value) -> Path:
    path.write_text(json.dumps(value))
    return path


def proposal(key: str, **extra) -> dict:
    return {"action": "new", "key": key, "domain": "test/app", "title": f"Title {key}", "body": f"Body {key}.",
            "evidence": "abc1234", **extra}


def result(**extra) -> dict:
    """A workflow result, as workflow.js returns it."""
    return {"report": "r", "saved": [], "failed_saves": [], "retry": [], "needs_user": [], "unverified": [],
            "failed_sources": [], "unavailable": [], "started_at": "2026-11-07T20:00:00Z", "agents": 9,
            "counts": {"sources": 1, "read": 1}, **extra}


class FakeGh:
    """Stands in for `run`: answers `gh pr list` with the PRs of each state and records the calls."""

    def __init__(self, merged: list, closed: list, fail: bool = False) -> None:
        self.lists = {"merged": merged, "closed": closed}
        self.fail = fail
        self.calls = []
        self.real = inv.run

    def __call__(self, args, cwd=None):
        if args[:3] != ["gh", "pr", "list"]:
            return self.real(args, cwd)
        self.calls.append(args)
        if self.fail:
            raise RuntimeError("gh pr list failed: not logged in")
        state = args[args.index("--state") + 1]
        limit = int(args[args.index("--limit") + 1])
        return json.dumps(self.lists[state][:limit])


def prs(count: int, start: datetime, state: str = "merged", first: int = 1) -> list:
    out = []
    for n in range(count):
        created = start + timedelta(minutes=n)
        stamp = (created + timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        out.append({"number": first + n, "title": f"PR {first + n}", "body": "", "url": f"https://x/{first + n}",
                    "createdAt": created.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "mergedAt": stamp if state == "merged" else None, "closedAt": stamp})
    return out


class ParseLogTest(unittest.TestCase):
    def test_bodies_and_numstat_stay_with_their_commit(self) -> None:
        R, F = inv.RECORD, inv.FIELD
        out = (f"{R}aaa{F}Ann{F}2026-11-01T10:00:00+00:00{F}feat: one{F}Why we did it.\n\n3\t1\tsrc/a.rs\n-\t-\tlogo.png\n"
               f"{R}bbb{F}Bob{F}2026-11-02T10:00:00+00:00{F}fix: two{F}\n\n10\t0\tsrc/b.py\n")
        commits = inv.parse_log(out)
        self.assertEqual([c["sha"] for c in commits], ["aaa", "bbb"])
        self.assertEqual(commits[0]["body"], "Why we did it.")
        self.assertEqual(commits[0]["files"], [{"path": "src/a.rs", "changed": 4}, {"path": "logo.png", "changed": 0}])
        self.assertEqual(commits[1]["body"], "")
        self.assertEqual(commits[1]["files"], [{"path": "src/b.py", "changed": 10}])


class BatchTest(unittest.TestCase):
    def test_batches_split_by_count(self) -> None:
        out = inv.batches("prs", "owner/name", [f"pr {i}" for i in range(19)], 8)
        self.assertEqual([b["items"] for b in out], [8, 8, 3])
        self.assertEqual(out[0]["id"], "prs-name-1")


class BuildTest(unittest.TestCase):
    def test_first_run_then_only_new_commits(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            repo = make_repo(Path(folder))
            sources = {"test/app": {"repos": [{"path": str(repo), "branch": "main"}]}}
            first = inv.build("test/app", sources, {}, False, 3, None, None)
            self.assertTrue(first["first_run"])
            self.assertEqual(first["counts"]["commits"], 1)
            self.assertIn("Chose X over Y because Z.", first["batches"][0]["text"])
            state_file = Path(folder) / "state.json"
            inv.mark(state_file, write(Path(folder) / "inventory.json", first),
                     write(Path(folder) / "result.json", result()))
            state = json.loads(state_file.read_text())
            (repo / "b.py").write_text("y = 2\n")
            git(repo, "add", ".")
            git(repo, "commit", "-q", "-m", "fix: second")
            second = inv.build("test/app", sources, state, False, 3, None, None)
            self.assertFalse(second["first_run"])
            self.assertEqual(second["counts"]["commits"], 1)
            self.assertIn("fix: second", second["batches"][0]["text"])
            self.assertNotIn("feat: first", second["batches"][0]["text"])

    def test_an_area_without_sources_is_refused(self) -> None:
        with self.assertRaises(SystemExit):
            inv.build("test/none", {}, {}, False, 3, None, None)


class PullRequestTest(unittest.TestCase):
    def setUp(self) -> None:
        self.folder = tempfile.TemporaryDirectory()
        self.repo = make_repo(Path(self.folder.name))
        self.sources = {"test/app": {"repos": [{"path": str(self.repo), "branch": "main", "github": "o/app"}]}}

    def tearDown(self) -> None:
        self.folder.cleanup()

    def test_prs_are_read_oldest_created_first_from_just_before_the_cursor(self) -> None:
        fake = FakeGh(prs(2, datetime(2026, 11, 2, tzinfo=timezone.utc)), [])
        state = {"repos": {str(self.repo): {"at": "2026-11-01T10:00:00Z"}}}
        with mock.patch.object(inv, "run", fake):
            out = inv.build("test/app", self.sources, state, False, 3, None, None)
        merged = fake.calls[0][fake.calls[0].index("--search") + 1]
        self.assertEqual(merged, "merged:>=2026-11-01T09:00:00+00:00 sort:created-asc")
        self.assertIn("is:unmerged", fake.calls[1][fake.calls[1].index("--search") + 1])
        self.assertEqual(out["counts"]["prs"], 2)
        self.assertEqual(out["truncated"], [])
        self.assertEqual(out["next_state"]["repos"][str(self.repo)]["prs_since"], out["generated_at"])

    def test_a_list_cut_at_its_limit_is_reported_and_the_cursor_stops_at_what_was_read(self) -> None:
        start = datetime(2026, 10, 1, tzinfo=timezone.utc)
        fake = FakeGh(prs(205, start), prs(3, start, "closed", first=1000))
        with mock.patch.object(inv, "run", fake):
            out = inv.build("test/app", self.sources, {}, False, 3, None, None)
        until = (start + timedelta(minutes=199)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.assertEqual(out["truncated"], [{"repo": "o/app", "state": "merged", "limit": 200, "until": until}])
        self.assertEqual(out["counts"]["truncated"], 1)
        self.assertEqual(out["counts"]["prs"], 203)
        self.assertEqual(out["next_state"]["repos"][str(self.repo)]["prs_since"], until)
        self.assertEqual(out["next_state"]["repos"][str(self.repo)]["at"], out["generated_at"])

    def test_the_printed_counts_name_the_cut(self) -> None:
        folder = Path(self.folder.name)
        sources = write(folder / "sources.json", self.sources)
        fake = FakeGh(prs(200, datetime(2026, 10, 1, tzinfo=timezone.utc)), [])
        printed = io.StringIO()
        with mock.patch.object(inv, "run", fake), redirect_stdout(printed):
            inv.main(["--domain", "test/app", "--sources", str(sources), "--state", str(folder / "state.json"),
                      "--out", str(folder / "inventory.json")])
        line = json.loads(printed.getvalue())
        self.assertEqual(line["truncated"][0]["state"], "merged")
        self.assertEqual(line["truncated"][0]["limit"], 200)
        self.assertIn("estimate", line)

    def test_a_failed_gh_read_keeps_the_pr_cursor(self) -> None:
        state = {"repos": {str(self.repo): {"commit": "0" * 40, "at": "2026-11-01T10:00:00Z"}}}
        with mock.patch.object(inv, "run", FakeGh([], [], fail=True)):
            out = inv.build("test/app", self.sources, state, False, 3, None, None)
        cursor = out["next_state"]["repos"][str(self.repo)]
        self.assertIn("not logged in", out["repos"][0]["github_error"])
        self.assertEqual(cursor["prs_since"], "2026-11-01T10:00:00Z")
        self.assertEqual(cursor["at"], out["generated_at"], "the commit cursor still moves")


class MarkTest(unittest.TestCase):
    def setUp(self) -> None:
        self.folder = tempfile.TemporaryDirectory()
        self.dir = Path(self.folder.name)
        self.state = self.dir / "state.json"
        self.inventory = {
            "domain": "test/app", "generated_at": "2026-11-07T19:00:00Z",
            "batches": [{"id": "commits-app-1", "kind": "commits", "repo": "/r/app", "items": 2}],
            "repos": [{"path": "/r/app", "github": "o/app"}],
            "slack": [{"id": "C1", "name": "#one"}, {"id": "C2", "name": "#two"}],
            "linear": {"team": "ENG"}, "docs": [],
            "next_state": {"domain": "test/app", "last_run": "2026-11-07T19:00:00Z",
                           "repos": {"/r/app": {"commit": "new", "at": "2026-11-07T19:00:00Z",
                                                "prs_since": "2026-11-07T19:00:00Z"}},
                           "slack": {"C1": "2026-11-07T19:00:00Z", "C2": "2026-11-07T19:00:00Z"},
                           "linear_since": "2026-11-07T19:00:00Z", "docs": {}},
        }
        self.previous = {"last_run": "2026-11-01T00:00:00Z",
                         "repos": {"/r/app": {"commit": "old", "at": "2026-11-01T00:00:00Z"}},
                         "slack": {"C1": "2026-11-01T00:00:00Z"}, "linear_since": "2026-11-01T00:00:00Z",
                         "carried": [proposal("older")], "runs": [{"agents": 10, "tokens": 900000, "sources": 2}]}
        write(self.state, self.previous)

    def tearDown(self) -> None:
        self.folder.cleanup()

    def mark(self, outcome: dict, stats=None) -> dict:
        inventory = write(self.dir / "inventory.json", self.inventory)
        inv.mark(self.state, inventory, write(self.dir / "result.json", outcome), stats)
        return json.loads(self.state.read_text())

    def test_unverified_and_failed_saves_are_carried_with_when_they_were_drafted(self) -> None:
        state = self.mark(result(unverified=[proposal("over-cap"), proposal("again", drafted_at="2026-10-01T00:00:00Z",
                                                                          carried=True)],
                                 retry=[proposal("save-failed", verdict={"real": True})]))
        self.assertEqual([p["key"] for p in state["carried"]], ["over-cap", "again", "save-failed"])
        self.assertEqual([p["drafted_at"] for p in state["carried"]],
                         ["2026-11-07T20:00:00Z", "2026-10-01T00:00:00Z", "2026-11-07T20:00:00Z"])
        self.assertNotIn("carried", state["carried"][1])
        self.assertNotIn("verdict", state["carried"][2])
        state_dir = self.dir
        nxt = inv.build("test/app", {"test/app": {"repos": []}}, state, False, 3, None, None, 30, state_dir)
        self.assertEqual([p["key"] for p in nxt["carried"]], ["over-cap", "again", "save-failed"])
        self.assertEqual(nxt["counts"]["carried"], 3)

    def test_the_first_runs_result_format_is_accepted(self) -> None:
        first = {"report": "## Saved", "saved": ["a (test/app)"], "failed_saves": [],
                 "needs_user": [{**proposal("n"), "action": "contradiction", "verdict": {"real": True}}],
                 "unverified": [proposal(f"u{i}", tags=["decision"], existing_origin="none") for i in range(14)],
                 "counts": {"sources": 4, "read": 4, "candidates": 34, "proposals": 26}}
        state = self.mark(first)
        self.assertEqual(len(state["carried"]), 14)
        self.assertEqual(state["carried"][0]["drafted_at"], "2026-11-07T19:00:00Z", "no started_at: the inventory's time")
        self.assertEqual(state["last_run"], "2026-11-07T19:00:00Z")

    def test_a_failed_source_refuses_the_mark_and_leaves_the_state(self) -> None:
        before = self.state.read_text()
        with self.assertRaises(SystemExit) as refused:
            self.mark(result(failed_sources=["commits-app-1"]))
        self.assertIn("commits-app-1", str(refused.exception))
        self.assertEqual(self.state.read_text(), before)
        with self.assertRaises(SystemExit):
            self.mark({"unverified": [], "counts": {"sources": 4, "read": 3}})
        self.assertEqual(self.state.read_text(), before)

    def test_an_unavailable_source_keeps_its_cursor(self) -> None:
        state = self.mark(result(unavailable=[{"source": "slack:C1", "reason": "private"},
                                              {"source": "linear", "reason": "not connected"},
                                              {"source": "commits-app-1", "reason": "?"}]))
        self.assertEqual(state["slack"], {"C1": "2026-11-01T00:00:00Z", "C2": "2026-11-07T19:00:00Z"})
        self.assertEqual(state["linear_since"], "2026-11-01T00:00:00Z")
        self.assertEqual(state["repos"]["/r/app"],
                         {"commit": "old", "at": "2026-11-01T00:00:00Z", "prs_since": "2026-11-07T19:00:00Z"})
        write(self.state, self.previous)
        older = self.mark({"unverified": [], "counts": {"sources": 1, "read": 1},
                           "unavailable": ["C2: the bot was removed: ask an admin"]})
        self.assertNotIn("C2", older["slack"], "a channel never read before starts over")

    def test_stats_are_recorded_and_feed_the_estimate(self) -> None:
        state = self.mark(result(), {"agents": 27, "tokens": 2600000})
        self.assertEqual(state["runs"][-1], {"at": "2026-11-07T19:00:00Z", "agents": 27, "tokens": 2600000, "sources": 4})
        self.assertEqual(len(state["runs"]), 2)

    def test_mark_needs_a_result(self) -> None:
        with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
            inv.main(["--mark", str(self.state), "--inventory", str(self.dir / "inventory.json")])


class EstimateTest(unittest.TestCase):
    RUNS = [{"agents": 27, "tokens": 2600000, "sources": 4}]

    def test_planned_agents_scale_with_sources_times_tokens_per_agent(self) -> None:
        small = inv.estimate(self.RUNS, sources=1, carried=0, max_verify=30)
        self.assertEqual(small["agents"], 7)
        self.assertEqual(small["tokens_per_agent"], 96296)
        self.assertEqual(small["tokens"], round(7 * 2600000 / 27))
        self.assertFalse(small["ask_first"])
        big = inv.estimate(self.RUNS, sources=4, carried=14, max_verify=30)
        self.assertEqual(big["agents"], 27 + 14)
        self.assertTrue(big["ask_first"])

    def test_the_workflows_maximum_bounds_the_plan(self) -> None:
        huge = inv.estimate(self.RUNS, sources=20, carried=50, max_verify=30)
        self.assertEqual(huge["agents"], 20 + 1 + 8 + 30 + 2)

    def test_without_a_recorded_run_there_is_no_estimate_and_the_skill_asks(self) -> None:
        unknown = inv.estimate([], sources=2, carried=0, max_verify=30)
        self.assertIsNone(unknown["tokens"])
        self.assertTrue(unknown["ask_first"])
        nothing = inv.estimate([], sources=0, carried=0, max_verify=30)
        self.assertEqual((nothing["agents"], nothing["tokens"], nothing["ask_first"]), (0, 0, False))
        carried_only = inv.estimate(self.RUNS, sources=0, carried=5, max_verify=30)
        self.assertEqual(carried_only["agents"], 5 + 2)

    def test_an_area_without_runs_uses_the_other_areas(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            write(Path(folder) / "personal-other.json", {"runs": self.RUNS})
            self.assertEqual(inv.recorded_runs({}, Path(folder)), self.RUNS)
            self.assertEqual(inv.recorded_runs({"runs": [{"agents": 1, "tokens": 5}]}, Path(folder)),
                             [{"agents": 1, "tokens": 5}])


if __name__ == "__main__":
    unittest.main()
