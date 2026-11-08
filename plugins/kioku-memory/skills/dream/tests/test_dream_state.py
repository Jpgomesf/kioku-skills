"""Tests for dream_state.py. Run: python3 -m unittest discover -s tests

maybe-start runs against a fake `claude` on PATH, in a temporary HOME: no real
Claude or Kioku is started.
"""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "dream_state.py"
spec = importlib.util.spec_from_file_location("dream_state", SCRIPT)
ds = importlib.util.module_from_spec(spec)
sys.modules["dream_state"] = ds
spec.loader.exec_module(ds)

NOW = datetime(2026, 11, 7, 20, 0, tzinfo=timezone.utc)
DOMAIN = "personal/demo"
CONFIG = {"enabled": True, "every_days": 7, "max_budget_usd": 20, "max_topics": 4, "max_verify": 20}

# A fake `claude`: records its arguments, the variables the run sets and its folder;
# with FAKE_CLAUDE_MARK=ok it records the run as the skill would; exits FAKE_CLAUDE_EXIT.
FAKE_CLAUDE = textwrap.dedent("""\
    import json, os, re, subprocess, sys
    call = {"argv": sys.argv[1:], "cwd": os.getcwd(),
            "env": {k: os.environ.get(k) for k in ("KIOKU_HOOKS", "CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS", "CLAUDECODE")}}
    with open(os.environ["FAKE_CLAUDE_LOG"], "w") as out:
        json.dump(call, out)
    if os.environ.get("FAKE_CLAUDE_MARK"):
        prompt = sys.argv[sys.argv.index("-p") + 1]
        report = re.search(r"^- report: (.+)$", prompt, re.M).group(1)
        domain = re.search(r"^- domain: (.+)$", prompt, re.M).group(1)
        open(report, "w").write("# Dream report\\n")
        subprocess.run([sys.executable, "-I", os.environ["DREAM_STATE"], "mark", "--domain", domain,
                        "--status", os.environ["FAKE_CLAUDE_MARK"], "--report", report,
                        "--counts", json.dumps({"saved": 3}), "--scheduled"], check=True)
    print(json.dumps({"type": "result", "subtype": "success", "num_turns": 7, "total_cost_usd": 1.25}))
    sys.exit(int(os.environ.get("FAKE_CLAUDE_EXIT", "0")))
""")


def stamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


class DueTest(unittest.TestCase):
    def test_a_domain_that_never_succeeded_is_not_due(self) -> None:
        self.assertEqual(ds.due({}, None, NOW, 7), (False, "no successful dream yet: the first dream is manual"))
        failed_only = {"last_failed": stamp(NOW - timedelta(days=30))}
        self.assertFalse(ds.due(failed_only, None, NOW, 7)[0])

    def test_due_once_the_last_success_is_older_than_every_days(self) -> None:
        self.assertTrue(ds.due({"last_ok": stamp(NOW - timedelta(days=8))}, None, NOW, 7)[0])
        self.assertFalse(ds.due({"last_ok": stamp(NOW - timedelta(days=6))}, None, NOW, 7)[0])
        self.assertTrue(ds.due({"last_ok": stamp(NOW - timedelta(hours=13))}, None, NOW, 0.5)[0])

    def test_a_failure_in_the_last_24_hours_holds_it_back(self) -> None:
        state = {"last_ok": stamp(NOW - timedelta(days=10)), "last_failed": stamp(NOW - timedelta(hours=23))}
        due, why = ds.due(state, None, NOW, 7)
        self.assertFalse(due)
        self.assertIn("less than 24 hours ago", why)
        state["last_failed"] = stamp(NOW - timedelta(hours=25))
        self.assertTrue(ds.due(state, None, NOW, 7)[0])

    def test_a_live_lock_younger_than_6_hours_means_a_run_is_in_progress(self) -> None:
        state = {"last_ok": stamp(NOW - timedelta(days=10))}
        live = {"pid": os.getpid(), "started": stamp(NOW - timedelta(hours=5))}
        self.assertEqual(ds.due(state, live, NOW, 7)[0], False)
        stale = {"pid": os.getpid(), "started": stamp(NOW - timedelta(hours=6, minutes=1))}
        self.assertFalse(ds.lock_held(stale, NOW))
        self.assertTrue(ds.due(state, stale, NOW, 7)[0])
        ended = subprocess.Popen(["true"])
        ended.wait()
        self.assertFalse(ds.lock_held({"pid": ended.pid, "started": stamp(NOW)}, NOW))
        self.assertFalse(ds.lock_held({"pid": "x", "started": "not a time"}, NOW))


class ConfigTest(unittest.TestCase):
    def test_the_plugins_config_is_read_and_checked(self) -> None:
        self.assertEqual(ds.parse_config(json.dumps(CONFIG)),
                         {"every_days": 7, "max_budget_usd": 20, "max_topics": 4, "max_verify": 20})
        self.assertEqual(ds.parse_config("{}"), ds.DEFAULTS)
        for bad in ('{"every_days": 0}', '{"max_topics": 1.5}', '{"max_budget_usd": "5"}', "[]", "nope"):
            with self.assertRaises(SystemExit):
                ds.parse_config(bad)

    def test_a_domain_cannot_leave_the_state_folder(self) -> None:
        for bad in ("../x", "/abs", "a//b", "", "a/../b"):
            with self.assertRaises(SystemExit):
                ds.check_domain(bad)
        self.assertEqual(ds.check_domain("personal/notes"), "personal/notes")


class CommandTest(unittest.TestCase):
    def test_the_headless_run(self) -> None:
        report, run_dir = Path("/s/reports/r.md"), Path("/s/runs/r")
        with mock.patch.dict(os.environ, {"CLAUDECODE": "1", "CLAUDE_CODE_SESSION_ID": "s1", "KIOKU_HOME": "/kioku"}):
            command, env = ds.run_command(DOMAIN, "/area", ds.parse_config(json.dumps(CONFIG)), report, run_dir,
                                          stamp(NOW), "2026-10-01T00:00:00Z")
        self.assertEqual(command[1], "-p")
        flags = command[3:]

        def option(name: str) -> str:
            return flags[flags.index(name) + 1]
        self.assertEqual(option("--model"), "opus")
        self.assertEqual(option("--permission-mode"), "dontAsk")
        self.assertEqual(option("--max-budget-usd"), "20")
        self.assertEqual(option("--tools"), "Bash,Read,Write,Workflow,ToolSearch,StructuredOutput")
        self.assertIn("--strict-mcp-config", flags)
        allowed = flags[flags.index("--allowedTools") + 1:]
        self.assertEqual(flags.index("--allowedTools") + 1 + len(allowed), len(flags), "the allowlist ends the command")
        for rule in ("mcp__kioku__memory_save", "mcp__kioku__memory_inspect", "mcp__kioku__code_map", "Workflow",
                     f"Bash(python3 -I {ds.SCRIPT} mark *)", "Bash(git -C * ls-files *)",
                     "Write(~/.local/state/kioku/dream/**)"):
            self.assertIn(rule, allowed)
        self.assertNotIn("mcp__kioku__memory_retire", allowed)
        skills = SCRIPT.parent.parent.parent
        self.assertIn(ds.read_rule(skills), allowed)
        self.assertIn(f"Bash(python3 -I {skills / 'project-sync' / 'scripts' / 'file_pending.py'} *)", allowed)
        server = json.loads(option("--mcp-config"))["mcpServers"]["kioku"]
        self.assertEqual(server["command"], "/kioku/bin/kioku")
        self.assertEqual(server["env"]["KIOKU_SESSION_DIR"], "/area")
        self.assertEqual(server["args"][-2:], ["--tools", "core,memory_list,memory_inspect,code_map"])
        self.assertEqual(env["KIOKU_HOOKS"], "off")
        self.assertEqual(env["CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS"], "0")
        self.assertNotIn("CLAUDECODE", env)
        self.assertNotIn("CLAUDE_CODE_SESSION_ID", env)
        prompt = command[2]
        for text in ("scheduled mode", "- domain: personal/demo", "- maxTopics: 4", "- maxVerify: 20",
                     "- since (the last successful dream): 2026-10-01T00:00:00Z", "- report: /s/reports/r.md",
                     "never ask a question", "--scheduled", str(skills / "dream" / "SKILL.md")):
            self.assertIn(text, prompt)

    def test_read_rules_use_the_home_folder_or_an_absolute_path(self) -> None:
        self.assertEqual(ds.read_rule(Path.home() / "x" / "skills"), "Read(~/x/skills/**)")
        self.assertEqual(ds.read_rule(Path("/opt/plugins/skills")), "Read(//opt/plugins/skills/**)")


class SkillsDirTest(unittest.TestCase):
    """The kioku-hooks plugin runs its own copy of this script, outside any skills folder."""

    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.dir = Path(folder.name)
        hooks_scripts = self.dir / "kioku-hooks" / "scripts"
        hooks_scripts.mkdir(parents=True)
        shutil.copy(SCRIPT, hooks_scripts / "dream_state.py")
        copy_spec = importlib.util.spec_from_file_location("dream_state_copy", hooks_scripts / "dream_state.py")
        self.copy = importlib.util.module_from_spec(copy_spec)
        copy_spec.loader.exec_module(self.copy)
        self.home = self.dir / "home"
        self.installed = self.dir / "cache" / "kioku-skills" / "kioku-memory" / "1.0.0"
        (self.installed / "skills" / "dream").mkdir(parents=True)
        (self.installed / "skills" / "dream" / "SKILL.md").write_text("---\nname: dream\n---\n")
        record = self.home / ".claude" / "plugins" / "installed_plugins.json"
        record.parent.mkdir(parents=True)
        record.write_text(json.dumps({"version": 2, "plugins": {
            "other@x": [{"scope": "user", "installPath": str(self.dir / "nowhere")}],
            "kioku-memory@kioku-skills": [{"scope": "project", "installPath": str(self.dir / "gone")},
                                          {"scope": "user", "installPath": str(self.installed)}]}}))
        env = {k: v for k, v in os.environ.items() if k != "KIOKU_SKILLS_DIR"}
        patcher = mock.patch.dict(os.environ, {**env, "HOME": str(self.home)}, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_its_own_folder_when_it_is_the_skills_copy(self) -> None:
        self.assertEqual(ds.skills_dir(), SCRIPT.parent.parent.parent)

    def test_the_hooks_copy_finds_the_installed_plugin(self) -> None:
        self.assertEqual(self.copy.skills_dir(), self.installed / "skills")

    def test_kioku_skills_dir_wins_over_the_install_record(self) -> None:
        other = self.dir / "checkout" / "skills"
        (other / "dream").mkdir(parents=True)
        (other / "dream" / "SKILL.md").write_text("x")
        with mock.patch.dict(os.environ, {"KIOKU_SKILLS_DIR": str(other)}):
            self.assertEqual(self.copy.skills_dir(), other)
        with mock.patch.dict(os.environ, {"KIOKU_SKILLS_DIR": str(self.dir / "empty")}):
            self.assertIsNone(self.copy.skills_dir())

    def test_no_skills_means_no_scheduled_run(self) -> None:
        (self.home / ".claude" / "plugins" / "installed_plugins.json").unlink()
        self.assertIsNone(self.copy.skills_dir())
        state = {"last_ok": stamp(datetime.now(timezone.utc) - timedelta(days=8))}
        with mock.patch.dict(os.environ, {"KIOKU_HOME": "/kioku"}), \
                mock.patch.object(self.copy, "read_state", return_value=state), \
                mock.patch.object(self.copy, "log"), \
                mock.patch.object(self.copy.subprocess, "Popen", side_effect=AssertionError("started a run")):
            started = self.copy.start(DOMAIN, str(self.dir), self.copy.parse_config(json.dumps(CONFIG)))
        self.assertFalse(started["started"])
        self.assertIn("dream skill was not found", started["why"])


class LauncherTest(unittest.TestCase):
    """mark, status and maybe-start, run as the hook runs them."""

    def setUp(self) -> None:
        self.folder = tempfile.TemporaryDirectory()
        self.dir = Path(self.folder.name)
        self.home = self.dir / "home"
        self.area = self.dir / "area"
        bin_dir = self.dir / "bin"
        for folder in (self.home, self.area, bin_dir):
            folder.mkdir()
        claude = bin_dir / "claude"
        claude.write_text(f"#!{sys.executable}\n" + FAKE_CLAUDE)
        claude.chmod(0o755)
        self.calls = self.dir / "claude-call.json"
        self.env = {**{k: v for k, v in os.environ.items() if k not in ("CLAUDE_PLUGIN_DATA", "KIOKU_SKILLS_DIR")},
                    "HOME": str(self.home), "PATH": f"{bin_dir}:{os.environ['PATH']}", "KIOKU_HOME": str(self.dir / "kioku"),
                    "FAKE_CLAUDE_LOG": str(self.calls), "DREAM_STATE": str(SCRIPT), "CLAUDECODE": "1"}
        self.state_dir = self.home / ".local" / "state" / "kioku" / "dream"

    def tearDown(self) -> None:
        self.folder.cleanup()

    def cli(self, *args: str, env: dict = None) -> dict:
        proc = subprocess.run([sys.executable, "-I", str(SCRIPT), *args], capture_output=True, text=True,
                              env=env or self.env, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    def state(self) -> dict:
        return self.cli("status", "--domain", DOMAIN, "--json")

    def maybe_start(self, env: dict = None) -> dict:
        return self.cli("maybe-start", "--domain", DOMAIN, "--cwd", str(self.area), "--config", json.dumps(CONFIG),
                        env=env)

    def wait_for_the_run(self) -> None:
        lock = self.state_dir / "personal-demo.lock"
        deadline = time.time() + 20
        while lock.exists() and time.time() < deadline:
            time.sleep(0.05)
        self.assertFalse(lock.exists(), "the run released its lock")

    def succeeded(self, days_ago: int) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        moment = stamp(datetime.now(timezone.utc) - timedelta(days=days_ago))
        (self.state_dir / "personal-demo.json").write_text(json.dumps(
            {"domain": DOMAIN, "last_run": moment, "status": "ok", "report": "/old.md", "counts": {},
             "scheduled": False, "last_ok": moment}))

    def test_mark_and_status(self) -> None:
        self.assertEqual(self.state(), {})
        marked = self.cli("mark", "--domain", DOMAIN, "--status", "ok", "--report", "/r.md", "--counts", '{"saved": 2}')
        status = self.state()
        self.assertEqual(marked, status)
        self.assertEqual({k: status[k] for k in ("domain", "status", "report", "counts", "scheduled")},
                         {"domain": DOMAIN, "status": "ok", "report": "/r.md", "counts": {"saved": 2}, "scheduled": False})
        self.assertTrue(status["last_run"].endswith("Z"))
        self.assertEqual(status["last_ok"], status["last_run"])
        self.cli("mark", "--domain", DOMAIN, "--status", "failed", "--report", "/f.md", "--scheduled")
        status = self.state()
        self.assertEqual((status["status"], status["scheduled"], status["last_ok"]), ("failed", True, marked["last_ok"]))
        self.assertTrue((self.state_dir / "personal-demo.json").is_file())

    def test_without_kioku_home_a_due_run_is_not_started(self) -> None:
        self.succeeded(days_ago=8)
        env = {k: v for k, v in self.env.items() if k != "KIOKU_HOME"}
        started = self.maybe_start(env=env)
        self.assertFalse(started["started"])
        self.assertIn("KIOKU_HOME is not set", started["why"])
        self.assertFalse((self.state_dir / "personal-demo.lock").exists())
        self.assertFalse(self.calls.exists())

    def test_a_domain_that_never_dreamed_is_not_started(self) -> None:
        started = self.maybe_start()
        self.assertEqual(started, {"started": False, "why": "no successful dream yet: the first dream is manual"})
        self.assertFalse(self.calls.exists())

    def test_a_due_run_starts_claude_and_records_itself(self) -> None:
        self.succeeded(days_ago=8)
        began = time.time()
        started = self.maybe_start(env={**self.env, "FAKE_CLAUDE_MARK": "ok"})
        self.assertLess(time.time() - began, 5, "maybe-start returns at once")
        self.assertTrue(started["started"], started)
        self.wait_for_the_run()
        call = json.loads(self.calls.read_text())
        self.assertEqual(Path(call["cwd"]).resolve(), self.area.resolve())
        self.assertEqual(call["env"], {"KIOKU_HOOKS": "off", "CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS": "0", "CLAUDECODE": None})
        argv = call["argv"]
        self.assertEqual((argv[0], argv[argv.index("--model") + 1], argv[argv.index("--max-budget-usd") + 1]),
                         ("-p", "opus", "20"))
        status = self.state()
        self.assertEqual((status["status"], status["scheduled"], status["counts"]), ("ok", True, {"saved": 3}))
        self.assertEqual(status["report"], started["report"])
        self.assertRegex(Path(started["report"]).name, r"^personal-demo-\d{8}-\d{4}\.md$")
        self.assertEqual(Path(started["report"]).parent, self.state_dir / "reports")
        events = [json.loads(line)["event"] for line in (self.state_dir / "dream.log").read_text().splitlines()]
        self.assertEqual(events[0], "started")
        self.assertEqual(events[-1], "run-finished")
        self.assertEqual(self.maybe_start()["why"].split(",")[0], f"the last dream was at {status['last_run']}")

    def test_a_run_that_ends_without_recording_itself_is_recorded_as_failed(self) -> None:
        self.succeeded(days_ago=8)
        started = self.maybe_start(env={**self.env, "FAKE_CLAUDE_EXIT": "1"})
        self.assertTrue(started["started"])
        self.wait_for_the_run()
        status = self.state()
        self.assertEqual((status["status"], status["scheduled"]), ("failed", True))
        self.assertEqual(status["counts"]["exit"], 1)
        self.assertEqual(status["counts"]["total_cost_usd"], 1.25)
        self.assertIn("ended without recording a result", Path(started["report"]).read_text())
        again = self.maybe_start()
        self.assertFalse(again["started"])
        self.assertIn("less than 24 hours ago", again["why"])

    def test_a_run_that_cannot_start_claude_is_recorded_as_failed(self) -> None:
        self.succeeded(days_ago=8)
        # No claude on this PATH, and none in the temporary home: the real one is never reached.
        started = self.maybe_start(env={**self.env, "PATH": "/usr/bin:/bin"})
        self.assertTrue(started["started"])
        self.wait_for_the_run()
        status = self.state()
        self.assertEqual(status["status"], "failed")
        self.assertIn("the run did not start", str(status["counts"]["exit"]))
        self.assertFalse(self.calls.exists())

    def test_a_held_lock_blocks_and_an_orphaned_one_counts_as_a_failure(self) -> None:
        self.succeeded(days_ago=8)
        lock = self.state_dir / "personal-demo.lock"
        lock.write_text(json.dumps({"pid": os.getpid(), "started": stamp(datetime.now(timezone.utc)), "report": "/x.md"}))
        held = self.maybe_start()
        self.assertFalse(held["started"])
        self.assertIn("in progress", held["why"])
        ended = subprocess.Popen(["true"])
        ended.wait()
        lock.write_text(json.dumps({"pid": ended.pid, "started": stamp(datetime.now(timezone.utc)), "report": "/x.md"}))
        orphaned = self.maybe_start()
        self.assertFalse(orphaned["started"])
        self.assertIn("less than 24 hours ago", orphaned["why"])
        self.assertFalse(lock.exists())
        status = self.state()
        self.assertEqual((status["status"], status["report"]), ("failed", "/x.md"))
        self.assertFalse(self.calls.exists())


if __name__ == "__main__":
    unittest.main()
