"""Tests for kioku_hook.py and codemap_summarize.py. Run: /usr/bin/python3 -m unittest discover -s tests

No test reaches a live service: Kioku is a fake class or a fake stdio server, and no
real claude runs."""

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
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

PLUGIN = Path(__file__).resolve().parent.parent
SCRIPT = PLUGIN / "scripts" / "kioku_hook.py"
# Never this machine's own settings or rules, and a stand-in Kioku checkout that nothing runs.
os.environ.update({"KIOKU_HOOKS_CONFIG": str(PLUGIN / "tests" / "no-local-settings.json"),
                   "KIOKU_RULES_DIR": str(PLUGIN / "tests" / "no-rules"), "KIOKU_HOME": "/kioku"})
spec = importlib.util.spec_from_file_location("kioku_hook", SCRIPT)
kh = importlib.util.module_from_spec(spec)
sys.modules["kioku_hook"] = kh  # dataclasses look their module up while the class is built
spec.loader.exec_module(kh)

summarize_spec = importlib.util.spec_from_file_location("codemap_summarize", PLUGIN / "scripts" / "codemap_summarize.py")
cs = importlib.util.module_from_spec(summarize_spec)
summarize_spec.loader.exec_module(cs)


def line(entry: dict) -> str:
    return json.dumps(entry, separators=(",", ":")) + "\n"


def user(text, **extra) -> dict:
    return {"type": "user", "timestamp": "2026-11-07T10:00:00Z", "message": {"role": "user", "content": text}, **extra}


def assistant(*parts, **extra) -> dict:
    return {"type": "assistant", "timestamp": "2026-11-07T10:01:00Z",
            "message": {"role": "assistant", "content": list(parts)}, **extra}


def tool_use(name: str, tool_id: str = "t1", **args) -> dict:
    return {"type": "tool_use", "id": tool_id, "name": name, "input": args}


def tool_result(tool_id: str, content, is_error: bool = False, **extra) -> dict:
    return user([{"type": "tool_result", "tool_use_id": tool_id, "content": content, "is_error": is_error}], **extra)


def hook_events(name: str) -> list[dict]:
    return [record for record in kh.read_lines("hooks.log") if record.get("event") == name]


class TempState(unittest.TestCase):
    # Most tests must not start real processes; the fake-server tests turn this off.
    mock_popen = True

    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name)
        self.addCleanup(self.dir.cleanup)
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(kh, "STATE", self.root / "state").start()
        pending_dir = mock.patch.dict(os.environ, {"KIOKU_PENDING_DIR": str(self.root / "pending")})
        pending_dir.start()
        self.addCleanup(pending_dir.stop)
        mock.patch.object(kh, "DREAM_SCRIPT", self.root / "dream" / "dream_state.py").start()
        mock.patch.object(kh.Kioku, "count_use_supported", True).start()
        if self.mock_popen:
            self.popen = mock.patch.object(kh.subprocess, "Popen").start()
        kh.DEADLINE.start(None)
        self.addCleanup(kh.DEADLINE.start, None)

    def transcript(self, entries: list[dict], tail: str = "", name: str = "session.jsonl") -> Path:
        path = self.root / name
        path.write_text("".join(line(e) for e in entries) + tail, encoding="utf-8")
        return path

    def config(self, **sections) -> None:
        """Patch CONFIG with sections merged over the current ones."""
        config = {**kh.CONFIG, "bundles": []}
        for name, values in sections.items():
            config[name] = {**kh.CONFIG[name], **values} if isinstance(values, dict) else values
        mock.patch.object(kh, "CONFIG", config).start()


class ConfigTest(unittest.TestCase):
    def test_nested_sections_merge_over_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.json"
            path.write_text(json.dumps({"prefetch": {"max_notes": 5}, "provider": "local", "dream": {"enabled": True}}))
            config = kh.load_config(path)
        self.assertEqual(config["prefetch"]["max_notes"], 5)
        self.assertEqual(config["prefetch"]["min_relevance"], kh.DEFAULTS["prefetch"]["min_relevance"])
        self.assertEqual(config["provider"], "local")
        self.assertEqual(config["review"]["model"], "sonnet")
        self.assertEqual(config["dream"], {"enabled": True, "every_days": 7, "max_budget_usd": 20,
                                           "max_topics": 4, "max_verify": 20})

    def test_missing_file_gives_defaults(self) -> None:
        config = kh.load_config(Path("/nonexistent/config.json"))
        self.assertEqual(config["nudge"]["every_prompts"], 8)
        self.assertFalse(config["dream"]["enabled"])
        for name in ("max_runs_per_day", "max_chars_per_day", "max_budget_usd"):
            self.assertGreater(config["review"][name], 0)

    def test_the_shipped_config_leaves_dreams_off(self) -> None:
        self.assertFalse(kh.load_config(PLUGIN / "config.json")["dream"]["enabled"])

    def test_the_shipped_config_is_generic(self) -> None:
        shipped = kh.load_config(PLUGIN / "config.json")
        self.assertEqual((shipped["bundles"], shipped["id_patterns"], shipped["kioku_home"]), ([], [], ""))
        self.assertEqual(shipped["drift_scanner"], "")
        self.assertEqual(shipped["review"]["note_format"], "")
        self.assertTrue((PLUGIN / "references" / "note-format.md").is_file())
        self.assertTrue((PLUGIN / "scripts" / "kb_drift.py").is_file())
        self.assertEqual(kh.DREAM_SCRIPT, PLUGIN / "scripts" / "dream_state.py")
        self.assertTrue(kh.DREAM_SCRIPT.is_file())

    def test_the_local_file_overrides_the_shipped_config(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            local = Path(folder) / "hooks.json"
            local.write_text(json.dumps({"bundles": [{"domain": "work/payments", "kb": "/kb", "repos": "/src"}],
                                         "id_patterns": ["PAY-\\d+"], "prefetch": {"max_notes": 2}}))
            config = kh.load_config(PLUGIN / "config.json", local)
        self.assertEqual(config["bundles"][0]["domain"], "work/payments")
        self.assertEqual(config["id_patterns"], ["PAY-\\d+"])
        self.assertEqual(config["prefetch"]["max_notes"], 2)
        self.assertEqual(config["prefetch"]["min_relevance"], kh.DEFAULTS["prefetch"]["min_relevance"])
        self.assertFalse(config["dream"]["enabled"])

    def test_kioku_home_comes_from_the_environment_then_the_config(self) -> None:
        with mock.patch.dict(os.environ, {"KIOKU_HOME": "/opt/kioku"}):
            self.assertEqual(kh.kioku_paths(), (Path("/opt/kioku/bin/kioku"), Path("/opt/kioku/.env")))
        env = {k: v for k, v in os.environ.items() if k not in ("KIOKU_HOME", "KIOKU_HOOK_BIN", "KIOKU_HOOK_ENV_FILE")}
        with mock.patch.dict(os.environ, env, clear=True):
            with mock.patch.object(kh, "CONFIG", {**kh.CONFIG, "kioku_home": "/srv/kioku"}):
                self.assertEqual(kh.kioku_paths()[0], Path("/srv/kioku/bin/kioku"))
            with mock.patch.object(kh, "CONFIG", {**kh.CONFIG, "kioku_home": ""}):
                self.assertFalse(kh.has_kioku())
                with self.assertRaises(LookupError):
                    kh.kioku_paths()


class DeadlineTest(unittest.TestCase):
    def test_each_hook_ends_well_before_claude_code_would_kill_it(self) -> None:
        hooks = json.loads((PLUGIN / "hooks" / "hooks.json").read_text())["hooks"]
        seen = set()
        for groups in hooks.values():
            for group in groups:
                for hook in group["hooks"]:
                    name = hook["command"].split()[-1]
                    seen.add(name)
                    self.assertLessEqual(kh.HOOK_SECONDS[name], hook["timeout"] - 1.5, name)
        self.assertEqual(seen, set(kh.HOOKS))

    def test_the_deadline_bounds_time_left(self) -> None:
        deadline = kh.Deadline()
        self.assertEqual(deadline.left(4.0), 4.0)
        deadline.start(1.0)
        self.assertLessEqual(deadline.left(4.0), 1.0)
        deadline.start(None)
        self.assertEqual(deadline.left(4.0), 4.0)


class TimeTest(unittest.TestCase):
    def test_timestamps_compare_as_utc(self) -> None:
        utc = timezone.utc
        self.assertEqual(kh.parse_time("2026-11-07T10:00:00Z"), datetime(2026, 11, 7, 10, tzinfo=utc))
        self.assertEqual(kh.parse_time("2026-11-07T05:00:00-05:00"), datetime(2026, 11, 7, 10, tzinfo=utc))
        self.assertEqual(kh.parse_time("2026-11-07T10:00:00.12Z"), datetime(2026, 11, 7, 10, 0, 0, 120000, tzinfo=utc))
        # Earlier versions wrote local time without an offset.
        self.assertEqual(kh.parse_time("2026-11-07T10:00:00"), datetime(2026, 11, 7, 10).astimezone().astimezone(utc))
        self.assertIsNone(kh.parse_time("yesterday"))
        self.assertIsNone(kh.parse_time(None))
        self.assertTrue(kh.now().endswith("+00:00"))


class RedactTest(unittest.TestCase):
    def test_masks_credentials_and_tokens(self) -> None:
        text = kh.redact("connect postgres://kioku:hunter2@db/x failed; api_key=abc123 Bearer abcdefghijk "
                         'token: "zzz" ghp_' + "a" * 30)
        for secret in ("hunter2", "abc123", "abcdefghijk", "zzz", "ghp_"):
            self.assertNotIn(secret, text)
        self.assertIn("postgres://***@db/x", text)
        self.assertEqual(kh.redact("test result: ok. 3 passed"), "test result: ok. 3 passed")


class PromptTextTest(unittest.TestCase):
    def test_keeps_what_the_person_wrote(self) -> None:
        entry = user("Let's go with B.<system-reminder>\nCLAUDE.md contents\n</system-reminder>")
        self.assertEqual(kh.prompt_text(entry), "Let's go with B.")

    def test_slash_commands_become_a_marker(self) -> None:
        entry = user("<command-name>/checkpoint</command-name>\n<command-message>checkpoint</command-message>\n"
                     "<command-args></command-args>")
        self.assertEqual(kh.prompt_text(entry), "[ran /checkpoint]")

    def test_skips_system_text(self) -> None:
        self.assertIsNone(kh.prompt_text(user("skill body", isMeta=True)))
        self.assertIsNone(kh.prompt_text(user("This session is being continued", isCompactSummary=True)))
        self.assertIsNone(kh.prompt_text(user("<task-notification>done</task-notification>")))
        self.assertIsNone(kh.prompt_text(user("[Request interrupted by user]")))
        self.assertIsNone(kh.prompt_text(tool_result("t1", "output")))

    def test_images_and_text_parts(self) -> None:
        entry = user([{"type": "text", "text": "this hover is broken"}, {"type": "image", "source": {}}])
        self.assertEqual(kh.prompt_text(entry), "this hover is broken\n[image]")


class CondenseTest(TempState):
    def test_condenses_the_conversation_and_stops_before_a_partial_line(self) -> None:
        entries = [
            user("Should the viewer save with Touch ID?"),
            {"type": "api-request-blob", "content": "x" * 1000},
            assistant({"type": "thinking", "thinking": ""}, {"type": "text", "text": "Two options."}),
            assistant(tool_use("AskUserQuestion", "ask1", questions=[{"question": "Which?"}])),
            tool_result("ask1", 'Your questions have been answered: "Which?"="Touch ID or password"'),
            assistant(tool_use("Bash", "b1", command="cargo test --all-targets")),
            tool_result("b1", "error: 2 tests failed", is_error=True),
            assistant(tool_use("Read", "r1", file_path="/repo/src/main.rs")),
            tool_result("r1", "fn main() {}"),
            user("subagent prompt", isSidechain=True),
            user("skill body", isMeta=True),
        ]
        path = self.transcript(entries, tail='{"type":"user","message":')
        cursor = kh.Cursor(0)
        blocks = list(kh.condense(path, cursor))
        text = "".join(block.text for block in blocks)
        self.assertIn("[User 2026-11-07T10:00]\nShould the viewer save with Touch ID?", text)
        self.assertIn("[Claude] Two options.", text)
        self.assertIn('answers: Your questions have been answered: "Which?"="Touch ID or password"', text)
        self.assertIn("- Bash: cargo test --all-targets", text)
        self.assertIn("    failed: error: 2 tests failed", text)
        self.assertIn("- Read: /repo/src/main.rs", text)
        self.assertNotIn("fn main", text)
        self.assertNotIn("subagent prompt", text)
        self.assertNotIn("skill body", text)
        self.assertEqual([b.prompt for b in blocks].count(True), 1)
        complete = len(path.read_bytes()) - len('{"type":"user","message":')
        self.assertEqual(cursor.offset, complete)

    def test_shows_command_results_saved_text_and_reports(self) -> None:
        passing = "running 3 tests\ntest a ... ok\n\ntest b ... ok\ntest result: ok. 3 passed; 0 failed\n"
        path = self.transcript([
            user("run the checks"),
            assistant(tool_use("Bash", "b1", command="cargo test")),
            tool_result("b1", passing, toolUseResult={"stdout": passing, "stderr": "", "interrupted": False}),
            assistant(tool_use("Bash", "b2", command="ruff check .")),
            tool_result("b2", "Exit code 1\nfoo.py:1:1: F401 unused import\nFound 1 error.", is_error=True),
            assistant(tool_use("Bash", "b3", command="grep -r nothing .")),
            tool_result("b3", "", toolUseResult={"stdout": "", "returnCodeInterpretation": "No matches found"}),
            assistant(tool_use("mcp__kioku__memory_save", "s1", domain="personal/app", key="tests-run-on-39",
                               title="Tests run on 3.9", body="The hooks run on /usr/bin/python3, so tests use it.")),
            tool_result("s1", '{"saved": true}'),
            assistant(tool_use("mcp__kioku__memory_save", "s2", domain="personal/app", key="bad", title="Bad",
                               body="Rejected.")),
            tool_result("s2", "invalid_argument: body is too long", is_error=True),
            assistant(tool_use("Agent", "a1", description="run the suite")),
            tool_result("a1", [{"type": "text", "text": "All 40 tests pass and ruff is clean."}]),
        ])
        blocks = list(kh.condense(path, kh.Cursor(0)))
        text = "".join(block.text for block in blocks)
        self.assertIn("- Bash: cargo test\n    exit 0: test a ... ok | test b ... ok | "
                      "test result: ok. 3 passed; 0 failed", text)
        self.assertIn("    exit 1: foo.py:1:1: F401 unused import | Found 1 error.", text)
        self.assertIn("    exit 0 (No matches found), no output", text)
        self.assertIn("    text: The hooks run on /usr/bin/python3, so tests use it.", text)
        self.assertIn("  error: invalid_argument: body is too long", text)
        self.assertIn("    report: All 40 tests pass and ruff is clean.", text)
        self.assertNotIn("running 3 tests", text)
        chunk = kh.chunked(blocks, 100_000)[0]
        self.assertEqual(chunk.saves, ["tests-run-on-39 (personal/app)"])

    def test_resumes_from_the_cursor(self) -> None:
        path = self.transcript([user("first prompt"), user("second prompt")])
        first = len(line(user("first prompt")).encode())
        blocks = list(kh.condense(path, kh.Cursor(first)))
        self.assertEqual(len(blocks), 1)
        self.assertIn("second prompt", blocks[0].text)

    def test_memory_saves_show_their_key(self) -> None:
        part = tool_use("mcp__kioku__memory_save", domain="personal/notes", key="hooks-plan", title="Hooks plan")
        self.assertEqual(kh.tool_line(part), "  - mcp__kioku__memory_save: personal/notes hooks-plan: Hooks plan")
        self.assertEqual(kh.tool_line(tool_use("TodoWrite")), "  - TodoWrite")

    def test_long_output_keeps_its_last_lines_and_masks_secrets(self) -> None:
        output = "\n".join(f"line {i}" for i in range(500)) + "\nDATABASE_PASSWORD=hunter2\n"
        outcome = kh.bash_outcome({"content": output}, {})
        self.assertEqual(outcome, "    exit 0: line 498 | line 499 | DATABASE_PASSWORD=***")


class ChunkTest(unittest.TestCase):
    def test_splits_between_blocks(self) -> None:
        blocks = [kh.Block("a" * 40, 10, True, "t1"), kh.Block("b" * 40, 20, False, "t2", ["k (d)"]),
                  kh.Block("c" * 40, 30, True, "t3")]
        chunks = kh.chunked(blocks, 100)
        self.assertEqual([c.size for c in chunks], [80, 40])
        self.assertEqual([c.end for c in chunks], [20, 30])
        self.assertEqual([c.prompts for c in chunks], [1, 1])
        self.assertEqual((chunks[0].first, chunks[0].last), ("t1", "t2"))
        self.assertEqual([c.saves for c in chunks], [["k (d)"], []])

    def test_a_block_larger_than_the_limit_gets_its_own_chunk(self) -> None:
        chunks = kh.chunked([kh.Block("a" * 300, 1, True, ""), kh.Block("b", 2, False, "")], 100)
        self.assertEqual([c.size for c in chunks], [300, 1])


class ScanTest(TempState):
    def test_counts_prompts_and_sees_commits_and_saves(self) -> None:
        path = self.transcript([
            user("one"), user("two"),
            assistant(tool_use("Bash", command="git -C /repo commit -m 'fix: x'")),
        ])
        stretch = kh.scan(path, kh.Cursor(0))
        self.assertEqual((stretch.prompts, stretch.committed, stretch.saved), (2, True, False))
        path = self.transcript([assistant(tool_use("mcp__kioku__memory_save", key="k"))])
        self.assertTrue(kh.scan(path, kh.Cursor(0)).saved)

    def test_names_the_folders_that_got_commits(self) -> None:
        path = self.transcript([
            assistant(tool_use("Bash", "b1", command="git -C /work/api commit -m 'feat: a'")),
            assistant(tool_use("Bash", "b2", command="git commit -m 'fix: b' && git push")),
            assistant(tool_use("Bash", "b3", command='git -c x=y -C "/work/my lib" commit -m c')),
            assistant(tool_use("Bash", "b4", command="git -C /work/api commit --amend --no-edit")),
        ])
        self.assertEqual(kh.scan(path, kh.Cursor(0)).commit_folders, ["/work/api", "", "/work/my lib"])

    def test_git_log_is_not_a_commit(self) -> None:
        path = self.transcript([assistant(tool_use("Bash", command="git log --oneline -5"))])
        self.assertFalse(kh.scan(path, kh.Cursor(0)).committed)


class NudgeReasonTest(unittest.TestCase):
    def test_reasons(self) -> None:
        self.assertIsNone(kh.nudge_reason(kh.Stretch(prompts=9, committed=True, saved=True), 9, 8))
        self.assertEqual(kh.nudge_reason(kh.Stretch(committed=True), 1, 8), "a commit just landed")
        self.assertEqual(kh.nudge_reason(kh.Stretch(prompts=3), 8, 8), "8 prompts since the last save")
        self.assertIsNone(kh.nudge_reason(kh.Stretch(prompts=3), 7, 8))


class StopTest(TempState):
    def run_stop(self, event: dict, attended: str = "1") -> str:
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ATTENDED": attended}), contextlib.redirect_stdout(out):
            kh.stop(event)
        return out.getvalue()

    def test_asks_once_after_a_commit(self) -> None:
        path = self.transcript([user("ship it"), assistant(tool_use("Bash", command="git commit -m 'feat: y'"))])
        event = {"session_id": "s1", "transcript_path": str(path), "stop_hook_active": False}
        output = json.loads(self.run_stop(event))
        context = output["hookSpecificOutput"]["additionalContext"]
        self.assertEqual(output["hookSpecificOutput"]["hookEventName"], "Stop")
        self.assertIn("a commit just landed", context)
        self.assertIn("checkpoint skill", context)
        self.assertEqual(self.run_stop(event), "")  # nothing new since

    def test_a_commit_starts_a_code_map_update_for_its_folder(self) -> None:
        path = self.transcript([
            user("ship it"),
            assistant(tool_use("Bash", "b1", command="git commit -m 'feat: y'")),
            assistant(tool_use("Bash", "b2", command="git -C ../lib commit -m 'feat: z'")),
        ])
        event = {"session_id": "s5", "transcript_path": str(path), "cwd": "/work/app"}
        with mock.patch.object(kh, "start_codemap") as started:
            self.assertIn("a commit just landed", self.run_stop(event))
        self.assertEqual([call.args[0] for call in started.call_args_list], ["/work/app", "/work/app/../lib"])

    def test_a_missing_folder_is_logged_not_raised(self) -> None:
        kh.start_codemap(str(self.root / "missing"))
        self.assertEqual(kh.read_lines("hooks.log")[-1]["event"], "codemap-skipped")

    def test_the_update_runs_detached_with_the_summarizer_and_no_hooks(self) -> None:
        folder = self.root / "repo"
        folder.mkdir()
        kh.start_codemap(str(folder))
        command = self.popen.call_args.args[0]
        options = self.popen.call_args.kwargs
        self.assertEqual(command[3:7], ["codemap", "update", "--repo", str(folder)])
        self.assertIn("--summarizer-provider", command)
        self.assertEqual(command[command.index("--summarizer-provider") + 1], "anthropic")
        self.assertTrue(options["start_new_session"])
        self.assertEqual(options["env"]["KIOKU_HOOKS"], "off")
        self.assertEqual(kh.read_lines("hooks.log")[-1]["event"], "codemap-started")

    def test_counts_prompts_across_turns(self) -> None:
        entries = [user(f"prompt {i}") for i in range(5)]
        path = self.transcript(entries)
        event = {"session_id": "s2", "transcript_path": str(path)}
        self.assertEqual(self.run_stop(event), "")
        with path.open("a") as handle:
            handle.write("".join(line(user(f"more {i}")) for i in range(3)))
        self.assertIn("8 prompts since the last save", self.run_stop(event))

    def test_quiet_while_continuing_and_in_unattended_runs(self) -> None:
        path = self.transcript([assistant(tool_use("Bash", command="git commit -m x"))])
        self.assertEqual(self.run_stop({"session_id": "s3", "transcript_path": str(path), "stop_hook_active": True}), "")
        self.assertEqual(self.run_stop({"session_id": "s3", "transcript_path": str(path)}, attended="0"), "")


class UnattendedTest(TempState):
    def test_logs_once_per_session_that_nudges_and_reviews_are_off(self) -> None:
        path = self.transcript([user("hi"), assistant(tool_use("Bash", command="git commit -m x"))])
        event = {"session_id": "u1", "transcript_path": str(path), "cwd": str(self.root)}
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ATTENDED": "0"}):
            kh.stop(event)
            kh.stop(event)
            kh.pre_compact(event)
            kh.session_end(event)
        records = hook_events("unattended")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["why"], "CLAUDE_CODE_SESSION_ATTENDED=0")
        self.popen.assert_not_called()
        env = {name: value for name, value in os.environ.items() if name != "CLAUDE_CODE_SESSION_ATTENDED"}
        env["CLAUDE_CODE_ENTRYPOINT"] = "claude-vscode"
        with mock.patch.dict(os.environ, env, clear=True):
            kh.stop({**event, "session_id": "u2"})
        self.assertEqual(hook_events("unattended")[-1]["why"], "entrypoint claude-vscode")


class FakeKioku:
    """Stands in for the short-lived Kioku server: answers memory_list from a fixed listing."""
    listing: dict = {}
    calls: list = []
    place: tuple = ("personal", "personal/app")
    rejected_arguments: set = set()

    def __init__(self, cwd, tools=None):
        FakeKioku.calls.append((cwd, tools))

    def tool(self, name, arguments):
        FakeKioku.calls.append((name, arguments))
        if any(argument in arguments for argument in FakeKioku.rejected_arguments):
            return {}
        return FakeKioku.listing if name == "memory_list" else {}

    def rejected(self, argument):
        return argument in FakeKioku.rejected_arguments

    def placement(self):
        return FakeKioku.place

    def close(self):
        pass


class FakeKiokuState(TempState):
    def setUp(self) -> None:
        super().setUp()
        FakeKioku.calls = []
        FakeKioku.listing = {"notes": []}
        FakeKioku.place = ("personal", "personal/app")
        FakeKioku.rejected_arguments = set()
        mock.patch.object(kh, "Kioku", FakeKioku).start()
        self.config()

    def capture(self, hook, event: dict) -> str:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            hook(event)
        return out.getvalue()


class PreReadTest(FakeKiokuState):
    def repo_file(self, name: str = "repo", rel: str = "src/vault/place.rs") -> Path:
        repo = self.root / name
        (repo / ".git").mkdir(parents=True)
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("// code\n")
        return target

    def test_a_file_brings_its_anchored_notes_once_per_session(self) -> None:
        target = self.repo_file()
        FakeKioku.listing = {"notes": [
            {"key": "vaults-file-has-one-writer", "domain": "personal/app", "title": "Only one module writes the vaults file"}]}
        event = {"session_id": "r1", "tool_input": {"file_path": str(target)}}
        context = json.loads(self.capture(kh.pre_read, event))["hookSpecificOutput"]["additionalContext"]
        self.assertIn("anchored to src/vault/place.rs", context)
        self.assertIn("vaults-file-has-one-writer (personal/app)", context)
        self.assertIn(("memory_list", {"path": "src/vault/place.rs", "limit": 20}), FakeKioku.calls)
        self.assertEqual(FakeKioku.calls[0], (str(self.root / "repo"), "core,memory_list"))
        self.assertEqual(self.capture(kh.pre_read, event), "")

    def test_a_file_read_again_starts_no_server(self) -> None:
        target = self.repo_file()
        event = {"session_id": "r3", "tool_input": {"file_path": str(target)}}
        self.assertEqual(self.capture(kh.pre_read, event), "")
        started = len(FakeKioku.calls)
        self.assertEqual(self.capture(kh.pre_read, event), "")
        self.assertEqual(len(FakeKioku.calls), started)

    def test_nothing_anchored_means_nothing_said(self) -> None:
        target = self.repo_file("repo2", "main.py")
        self.assertEqual(self.capture(kh.pre_read, {"session_id": "r2", "tool_input": {"file_path": str(target)}}), "")

    def test_a_server_without_note_paths_turns_anchors_off_for_the_session(self) -> None:
        FakeKioku.rejected_arguments = {"path"}
        first, second = self.repo_file(), self.repo_file("repo3", "lib.py")
        self.assertEqual(self.capture(kh.pre_read, {"session_id": "r4", "tool_input": {"file_path": str(first)}}), "")
        started = len(FakeKioku.calls)
        self.assertEqual(self.capture(kh.pre_read, {"session_id": "r4", "tool_input": {"file_path": str(second)}}), "")
        self.assertEqual(len(FakeKioku.calls), started)
        records = hook_events("kioku-old-server")
        self.assertEqual(len(records), 1)
        self.assertIn("/mcp reconnects it", records[0]["detail"])

    def test_a_timeout_is_logged_with_its_stage(self) -> None:
        target = self.repo_file()

        class Slow(FakeKioku):
            def tool(self, name, arguments):
                raise TimeoutError(name)

        with mock.patch.object(kh, "Kioku", Slow):
            self.assertEqual(self.capture(kh.pre_read, {"session_id": "r5", "tool_input": {"file_path": str(target)}}), "")
        self.assertEqual(hook_events("timeout")[-1]["stage"], "memory_list")
        self.assertEqual(hook_events("timeout")[-1]["hook"], "pre-read")


class BundleIndexTest(TempState):
    def setUp(self) -> None:
        super().setUp()
        self.bundle_root = self.root / "kb-bundle"
        self.kb = self.bundle_root / "payments-kb"
        (self.kb / "code").mkdir(parents=True)
        (self.bundle_root / "index.md").write_text('---\nokf_version: "0.2"\n---\n')
        (self.kb / "index.md").write_text("# Start here\n")
        self.note("config", "src/refunds/config.py")
        self.bundle = {"domain": "work/payments", "kb": str(self.kb), "repos": str(self.root / "work")}

    def note(self, name: str, path: str) -> Path:
        target = self.kb / "code" / f"{name}.md"
        target.write_text(f"---\nid: repo:payments-api@main:{path}\n---\nBody.\n")
        return target

    def test_cached_while_the_folders_are_unchanged(self) -> None:
        first = kh.bundle_index(self.bundle)
        self.assertEqual(first, [{"repo": "payments-api", "path": "src/refunds/config.py",
                                  "key": "payments-kb/code/config"}])
        # An edit in place changes no folder: a Read finds the cache without walking the notes.
        self.note("config", "src/refunds/moved.py")
        with mock.patch.object(kh.Path, "rglob", side_effect=AssertionError("walked the notes")):
            self.assertEqual(kh.bundle_index(self.bundle), first)

    def test_a_new_note_or_an_hour_rebuilds_it(self) -> None:
        kh.bundle_index(self.bundle)
        self.note("router", "src/refunds/router.py")
        folder = self.kb / "code"
        os.utime(folder, ns=(folder.stat().st_atime_ns, folder.stat().st_mtime_ns + 1_000_000_000))
        self.assertEqual(len(kh.bundle_index(self.bundle)), 2)
        self.note("router", "src/refunds/router2.py")
        cache = kh.state_file("index-work_payments.json")
        data = json.loads(cache.read_text())
        data["built"] -= kh.INDEX_MAX_AGE + 1
        cache.write_text(json.dumps(data))
        self.assertIn("src/refunds/router2.py", [n["path"] for n in kh.bundle_index(self.bundle)])

    def test_a_repository_outside_the_bundles_repos_skips_it(self) -> None:
        outside = self.root / "personal" / "payments-api"
        (outside / ".git").mkdir(parents=True)
        self.assertFalse(kh.covers(self.bundle, outside))
        inside = self.root / "work" / "payments-api"
        inside.mkdir(parents=True)
        self.assertTrue(kh.covers(self.bundle, inside))
        with mock.patch.object(kh, "CONFIG", {**kh.CONFIG, "bundles": [self.bundle]}), \
                mock.patch.object(kh, "bundle_index", side_effect=AssertionError("indexed")):
            self.assertIsNone(kh.bundle_context({"session_id": "b1"}, outside, "payments-api",
                                                "src/refunds/config.py"))


class PrefetchTest(unittest.TestCase):
    def test_search_query_skips_trivial_prompts(self) -> None:
        self.assertIsNone(kh.search_query("ok thanks"))
        self.assertIsNone(kh.search_query("yes"))
        self.assertIsNone(kh.search_query("/checkpoint now please"))
        self.assertIsNone(kh.search_query("Looks good!"))
        self.assertEqual(kh.search_query("fix the sidebar bug"), "fix the sidebar bug")
        self.assertEqual(kh.search_query("/srv/repo/main.rs is broken"), "/srv/repo/main.rs is broken")

    def test_relevance_floor_and_exact_matches(self) -> None:
        result = {"domain": "personal/notes", "results": [
            {"key": "strong", "match": "search", "signals": {"dense": 0.75, "lexical": 9.4}},
            {"key": "weak", "match": "search", "signals": {"dense": 0.61, "lexical": 1.1}},
            {"key": "exact", "match": "exact", "signals": {"dense": 0.3, "lexical": None}},
            {"key": "keywords", "match": "search", "signals": {"dense": 0.6, "lexical": 40}},
        ]}
        hits = kh.relevant_hits(result)
        self.assertEqual([h["key"] for h in hits], ["strong", "exact", "keywords"])
        self.assertEqual(hits[0]["domain"], "personal/notes")
        self.assertAlmostEqual(kh.relevance(result["results"][3]), 0.6 + 0.012 * 15)


class ParseStreamTest(unittest.TestCase):
    def test_reads_saves_and_requests(self) -> None:
        stream = [
            {"type": "system", "subtype": "init"},
            {"type": "assistant", "message": {"content": [
                tool_use("mcp__kioku__memory_save", "s1", key="new-rule", domain="personal/notes"),
                tool_use("mcp__kioku__memory_save", "s2", key="bad", domain="personal/notes")]}},
            {"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "s1", "content": "{}"},
                {"type": "tool_result", "tool_use_id": "s2", "content": "error", "is_error": True}]}},
            {"type": "result", "subtype": "success", "num_turns": 6, "total_cost_usd": 0.12,
             "result": "Saved one.\nRETIRE: old-rule (personal/notes), replaced by new-rule\nKB: DEC-1: narrowed"},
        ]
        outcome = kh.parse_stream("\n".join(json.dumps(m) for m in stream) + "\nnot json\n", 0)
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.saved, ["new-rule (personal/notes)"])
        self.assertEqual(outcome.retire, ["old-rule (personal/notes), replaced by new-rule"])
        self.assertEqual(outcome.kb, ["DEC-1: narrowed"])
        self.assertEqual((outcome.turns, outcome.cost), (6, 0.12))

    def test_a_crash_is_not_ok(self) -> None:
        outcome = kh.parse_stream("", 1)
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error, "exit 1")
        self.assertTrue(kh.parse_stream(json.dumps({"type": "result", "subtype": "error_max_turns"}), 0).ok)

    def test_a_run_stopped_by_its_budget_keeps_its_saves_and_last_reply(self) -> None:
        # What claude -p printed for a run over --max-budget-usd on this machine: exit 1, no result.
        stream = [
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "RETIRE: a (d), replaced by b"}]}},
            {"type": "result", "subtype": "error_max_budget_usd", "is_error": True, "result": None,
             "num_turns": 1, "total_cost_usd": 2.01},
        ]
        outcome = kh.parse_stream("\n".join(json.dumps(m) for m in stream), 1)
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.error, "error_max_budget_usd")
        self.assertEqual(outcome.retire, ["a (d), replaced by b"])


class ReviewCommandTest(unittest.TestCase):
    def test_the_review_runs_sonnet_at_high_effort_with_only_kioku_and_a_budget(self) -> None:
        command = kh.review_command("/work/app")
        self.assertEqual(command[command.index("--model") + 1], "sonnet")
        self.assertEqual(command[command.index("--effort") + 1], "high")
        self.assertIn("--strict-mcp-config", command)
        self.assertEqual(command[command.index("--tools") + 1], "")
        self.assertEqual(command[command.index("--max-budget-usd") + 1], str(kh.CONFIG["review"]["max_budget_usd"]))
        servers = json.loads(command[command.index("--mcp-config") + 1])["mcpServers"]
        self.assertEqual(list(servers), ["kioku"])
        self.assertEqual(servers["kioku"]["env"]["KIOKU_SESSION_DIR"], "/work/app")

    def test_the_prompt_matches_the_digest_and_its_limits(self) -> None:
        chunk = kh.Chunk(pieces=["[User x]\nhi\n"], size=12, first="2026-11-07T10:00", last="2026-11-07T11:00")
        text = kh.review_prompt(chunk, "/work/app", "personal", "personal/app", "at session end (exit)",
                                ["k1 (personal/app)", "k2 (personal/app)"])
        self.assertIn("A Bash call is followed by its exit status and the last lines of its output", text)
        self.assertIn("A memory_save call is followed by the text it saved, and by an error if the save failed", text)
        self.assertIn("you can't ask the user", text)
        self.assertIn("If memory_get rejects count_use, the server is older: call it again without count_use", text)
        self.assertIn("Keys saved earlier in this session, by the session itself or an earlier review: "
                      "k1 (personal/app), k2 (personal/app).", text)
        self.assertIn("update that key and domain instead of saving a near-duplicate", text)
        self.assertNotIn("{", text.split("<transcript>")[0].replace("{cwd}", ""))
        self.assertNotIn("Keys saved earlier", kh.review_prompt(chunk, "/w", "personal", None, "r"))
        self.assertIn("# Saving notes Kioku can find and connect", text, "the plugin's own note format")
        self.assertNotIn("<vault-rules>", text)

    def test_the_prompt_adds_the_vaults_own_rules(self) -> None:
        chunk = kh.Chunk(pieces=["[User x]\nhi\n"], size=12, first="2026-11-07T10:00", last="2026-11-07T11:00")
        with tempfile.TemporaryDirectory() as folder:
            Path(folder, "work.md").write_text("# Rules\n\nwork/payments mirrors the payments knowledge base.\n")
            with mock.patch.dict(os.environ, {"KIOKU_RULES_DIR": folder}):
                text = kh.review_prompt(chunk, "/work/app", "work", "work/payments", "at session end (exit)")
                other = kh.review_prompt(chunk, "/home", "personal", None, "at session end (exit)")
                self.assertEqual(kh.vault_rules("../work"), "")
        rules = text.split("<vault-rules>")[1].split("</vault-rules>")[0]
        self.assertIn("work/payments mirrors the payments knowledge base.", rules)
        self.assertLess(text.index("</note-format>"), text.index("<vault-rules>"))
        self.assertLess(text.index("</vault-rules>"), text.index("<transcript>"))
        self.assertNotIn("<vault-rules>", other)


class ReviewLoopTest(FakeKiokuState):
    def setUp(self) -> None:
        super().setUp()
        self.config(review={"chunk_chars": 300, "max_chunks": 2, "min_chars": 10, "min_prompts": 1,
                            "max_runs_per_day": 50, "max_chars_per_day": 1_000_000})
        self.reviewed: list = []
        self.outcomes: list = []

        def fake_run(chunk, cwd, vault, area, reason, earlier=()):
            self.reviewed.append((chunk.text, list(earlier)))
            return self.outcomes.pop(0) if self.outcomes else kh.Outcome(ok=True)

        mock.patch.object(kh, "run_review", side_effect=fake_run).start()
        entries = []
        for number in range(5):
            entries.append(user(f"prompt {number} " + "x" * 150))
            if number == 0:
                entries += [assistant(tool_use("mcp__kioku__memory_save", "s0", key="early", domain="personal/app",
                                               title="Early", body="Saved early.")),
                            tool_result("s0", "{}")]
        self.path = self.transcript(entries)
        self.chunks = kh.chunked(kh.condense(self.path, kh.Cursor(0)), 300)
        self.assertEqual(len(self.chunks), 5)

    def review(self, reason: str) -> None:
        kh.review(str(self.path), str(self.root), "session-1", reason)

    def offset(self) -> int:
        return kh.read_json(kh.state_file(f"review-{kh.digest(str(self.path))}.json"), {}).get("offset")

    def store(self) -> "kh.pending.PendingStore":
        return kh.pending.PendingStore("personal")

    def test_reviews_the_oldest_chunks_and_leaves_the_rest_for_the_next_review(self) -> None:
        self.review("before auto compaction")
        self.assertEqual(len(self.reviewed), 2)
        self.assertIn("prompt 0", self.reviewed[0][0])
        self.assertIn("prompt 1", self.reviewed[1][0])
        self.assertEqual(self.reviewed[1][1], ["early (personal/app)"])
        self.assertEqual(self.offset(), self.chunks[1].end)
        self.assertEqual(hook_events("review-deferred")[-1]["chunks"], 3)
        self.assertEqual(self.store().open_items(), [])
        self.review("at session end (exit)")
        self.assertIn("prompt 2", self.reviewed[2][0])
        self.assertEqual(self.offset(), self.chunks[3].end)
        [item] = self.store().open_items()
        self.assertEqual((item.kind, item.domain, item.ref), ("other", "personal/app", f"review:{kh.digest(str(self.path))}"))
        self.assertIn("unreviewed", item.text)
        detail = json.loads(Path(item.detail).read_text())
        self.assertTrue(detail["command"].endswith(" manual"))
        self.assertEqual(detail["characters"], self.chunks[4].size)
        self.review("manual")
        self.assertIn("prompt 4", self.reviewed[-1][0])
        self.assertEqual(self.store().open_items(), [])
        self.assertEqual(self.store().load().items[0].status, "done")

    def test_a_daily_cap_skips_the_review_and_session_end_records_it(self) -> None:
        self.config(review={"chunk_chars": 300, "max_chunks": 2, "min_chars": 10, "max_runs_per_day": 1,
                            "max_chars_per_day": 1_000_000})
        kh.append_line("reviews.log", {"at": kh.now(), "vault": "personal", "chars": 10})
        self.review("before auto compaction")
        self.assertEqual(self.reviewed, [])
        self.assertIn("daily cap of 1 review runs", hook_events("review-deferred")[-1]["why"])
        self.assertEqual(self.store().open_items(), [])
        self.review("at session end (exit)")
        self.assertEqual(self.reviewed, [])
        [item] = self.store().open_items()
        self.assertIn("daily cap", item.text)
        # A person can still run it: manual reviews skip the caps.
        self.review("manual")
        self.assertEqual(len(self.reviewed), 2)

    def test_the_character_cap_counts_old_local_timestamps_too(self) -> None:
        self.config(review={"chunk_chars": 300, "max_chunks": 2, "min_chars": 10, "max_runs_per_day": 50,
                            "max_chars_per_day": 1000})
        recent = (datetime.now() - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S")
        kh.append_line("reviews.log", {"at": recent, "vault": "personal", "chars": 900})
        kh.append_line("reviews.log", {"at": "2026-01-01T00:00:00", "vault": "personal", "chars": 100_000})
        self.assertEqual(kh.review_usage(), (1, 900))
        self.review("before auto compaction")
        self.assertEqual(self.reviewed, [])
        self.assertIn("characters", hook_events("review-deferred")[-1]["why"])

    def test_retire_and_kb_lines_go_to_the_pending_list(self) -> None:
        self.outcomes = [kh.Outcome(ok=True, retire=["old-rule (personal/other), replaced by new-rule"],
                                    kb=["DEC-1: narrowed to Python"]),
                         kh.Outcome(ok=True, retire=["old-rule (personal/other), replaced by new-rule"])]
        self.review("before auto compaction")
        items = self.store().open_items()
        self.assertEqual([(i.kind, i.domain) for i in items], [("retire", "personal/other"), ("kb", "personal/app")])
        self.assertTrue(all(i.source.startswith("review ") for i in items))
        record = kh.read_lines("reviews.log")[0]
        self.assertEqual(record["retire"], ["old-rule (personal/other), replaced by new-rule"])

    def test_a_failed_chunk_is_retried_once_then_skipped(self) -> None:
        self.outcomes = [kh.Outcome(ok=False, error="exit 1"), kh.Outcome(ok=False, error="exit 1")]
        self.review("before auto compaction")
        self.assertEqual(self.offset(), 0)
        self.review("before auto compaction")
        self.assertEqual(len(self.reviewed), 3)
        self.assertEqual(self.offset(), self.chunks[1].end)

    def test_a_review_waits_for_the_transcript_lock_then_skips(self) -> None:
        lock_path = kh.state_file(f"review-{kh.digest(str(self.path))}.lock")
        with lock_path.open("w") as held, mock.patch.object(kh, "lock_wait_seconds", return_value=0.2):
            fcntl.flock(held, fcntl.LOCK_EX)
            started = time.monotonic()
            self.review("at session end (exit)")
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(self.reviewed, [])
        self.assertIn("held it too long", hook_events("review-skipped")[-1]["why"])

    def test_acquire_takes_a_free_lock(self) -> None:
        with (self.root / "free.lock").open("w") as handle:
            self.assertTrue(kh.acquire(handle, 0))


class AnnouncementTest(TempState):
    def test_announces_each_review_once_per_vault(self) -> None:
        kh.append_line("reviews.log", {"at": "2026-11-07T10:00:00+00:00", "vault": "personal",
                                       "saved": ["a (x)", "b (x)"], "retire": ["old (x), replaced by a"], "kb": []})
        kh.append_line("reviews.log", {"at": "2026-11-07T10:05:00+00:00", "vault": "work", "saved": ["secret (w)"]})
        news = kh.announcements("personal")
        self.assertEqual(news.lines, ["Kioku's background review saved 2 notes from earlier sessions: a (x), b (x)."])
        self.assertNotIn("secret", " ".join(news.lines))
        # Only delivery marks them announced.
        self.assertEqual(kh.announcements("personal").lines, news.lines)
        kh.mark_announced("personal", news.newest)
        self.assertEqual(kh.announcements("personal").lines, [])
        self.assertEqual(list(kh.STATE.glob(".*.tmp")), [])

    def test_compares_times_in_utc_not_as_strings(self) -> None:
        # 23:00 at UTC-8 is 07:00 UTC the next day: as strings it sorts before both reviews.
        kh.write_json(kh.state_file("announced-personal.json"), {"at": "2026-11-07T23:00:00-08:00"})
        kh.append_line("reviews.log", {"at": "2026-11-08T06:00:00+00:00", "vault": "personal", "saved": ["before (x)"]})
        kh.append_line("reviews.log", {"at": "2026-11-08T08:00:00+00:00", "vault": "personal", "saved": ["after (x)"]})
        news = kh.announcements("personal")
        self.assertEqual(news.lines, ["Kioku's background review saved 1 notes from earlier sessions: after (x)."])
        self.assertEqual(news.newest, datetime(2026, 11, 8, 8, tzinfo=timezone.utc))


class IndexTest(unittest.TestCase):
    def test_newest_first_with_domains_at_a_vault_root(self) -> None:
        listing = {"total": 3, "notes": [
            {"key": "old", "domain": "d", "title": "Old", "updated": "2026-11-01"},
            {"key": "new", "domain": "d", "title": "New", "updated": "2026-11-07"},
            {"key": "mid", "domain": "e", "title": "Mid", "updated": "2026-11-04"},
        ]}
        lines = kh.index_lines(listing, None, 2)
        self.assertIn("(2 of 3)", lines[0])
        self.assertEqual(lines[1:], ["- new (d): New", "- mid (e): Mid"])
        self.assertEqual(kh.index_lines(listing, "d", 1)[1], "- new: New")
        self.assertEqual(kh.index_lines({"notes": []}, "d", 5), [])


class PendingAliasTest(TempState):
    def test_kioku_hook_pending_is_the_shared_cli(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = kh.main(["kioku_hook.py", "pending", "add", "--vault", "personal", "--domain", "personal/app",
                            "--kind", "retire", "--source", "dream 2026-11-07", "--text", "old-key, replaced"])
        self.assertEqual((code, out.getvalue().strip()), (0, "p1"), err.getvalue())
        self.assertEqual(kh.PENDING_CLI, PLUGIN / "scripts" / "kioku_pending.py")
        self.assertEqual([i.text for i in kh.pending.PendingStore("personal").open_items()], ["old-key, replaced"])


class SessionStartTest(FakeKiokuState):
    def start(self, session: str = "a1") -> dict:
        event = {"session_id": session, "cwd": str(self.root), "source": "startup"}
        out = self.capture(kh.session_start, event)
        return json.loads(out) if out else {}

    def test_lists_open_items_of_the_vault_every_session_until_done(self) -> None:
        store = kh.pending.PendingStore("personal")
        for number in range(4):
            store.add("personal/app", "retire", "review 2026-11-07", f"item {number}")
        kh.pending.PendingStore("work").add("work", "kb", "review 2026-11-07", "work item")
        for _ in range(2):
            output = self.start()
            context = output["hookSpecificOutput"]["additionalContext"]
            self.assertIn('4 Kioku items wait for the user\'s decision in vault "personal"', context)
            self.assertIn("- p1 [retire, personal/app] item 0 (review 2026-11-07)", context)
            self.assertIn("- p3 [retire, personal/app] item 2", context)
            self.assertNotIn("item 3", context)
            self.assertIn("and 1 more", context)
            self.assertIn("kioku_pending.py done --vault personal <id>", context)
            self.assertNotIn("work item", context)
            self.assertIn("4 items wait for your decision", output["systemMessage"])
        store.done("p1")
        self.assertIn("3 Kioku items wait", self.start()["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(hook_events("session-start")[-1]["pending"], 3)

    def test_records_the_placement_for_session_end(self) -> None:
        self.start("a2")
        self.assertEqual(kh.session_state("a2")["placement"], {"vault": "personal", "area": "personal/app"})

    def test_a_timeout_still_gives_what_it_has(self) -> None:
        class Slow(FakeKioku):
            def tool(self, name, arguments):
                raise TimeoutError(name)

        with mock.patch.object(kh, "Kioku", Slow):
            context = self.start("a3")["hookSpecificOutput"]["additionalContext"]
        self.assertIn('area "personal/app" of vault "personal"', context)
        self.assertEqual(hook_events("timeout")[-1], {**hook_events("timeout")[-1], "hook": "session-start",
                                                      "stage": "memory_list"})

    def test_prunes_old_state_but_keeps_cursors_of_live_transcripts(self) -> None:
        state = kh.STATE
        state.mkdir(parents=True, exist_ok=True)
        live = self.transcript([user("hi")], name="live.jsonl")
        files = {
            "seen-old.json": "{}", "stop-old.json": "{}", "review-0123456789abcdef.lock": "",
            "review-aaaaaaaaaaaaaaaa.json": json.dumps({"transcript": str(self.root / "gone.jsonl")}),
            "review-bbbbbbbbbbbbbbbb.json": json.dumps({"transcript": str(live)}),
            "review-output.log": "", ".x.json.123.tmp": "", "seen-new.json": "{}", "seen-a4.json": "{}",
        }
        old = time.time() - kh.STATE_MAX_AGE - 3600
        for name, text in files.items():
            (state / name).write_text(text)
            if name != "seen-new.json":
                os.utime(state / name, (old, old))
        self.start("a4")
        left = {path.name for path in state.iterdir()}
        for gone in ("seen-old.json", "stop-old.json", "review-0123456789abcdef.lock",
                     "review-aaaaaaaaaaaaaaaa.json", ".x.json.123.tmp"):
            self.assertNotIn(gone, left)
        for kept in ("seen-new.json", "review-bbbbbbbbbbbbbbbb.json", "review-output.log", "seen-a4.json"):
            self.assertIn(kept, left)
        self.assertEqual(hook_events("session-start")[-1]["pruned"], 5)


class DreamTest(FakeKiokuState):
    def setUp(self) -> None:
        super().setUp()
        kh.DREAM_SCRIPT.parent.mkdir(parents=True)
        kh.DREAM_SCRIPT.write_text("# stands in for dream_state.py\n")
        self.config(dream={"enabled": True})

    def end(self, session: str = "d1") -> None:
        transcript = self.transcript([user("hi")])
        event = {"session_id": session, "cwd": str(self.root), "transcript_path": str(transcript), "reason": "exit"}
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ATTENDED": "1"}):
            kh.session_end(event)

    def dream_calls(self) -> list:
        return [call for call in self.popen.call_args_list if "maybe-start" in call.args[0]]

    def test_session_end_starts_a_detached_maybe_start_without_hooks(self) -> None:
        kh.save_session("d1", placement={"vault": "personal", "area": "personal/app"})
        self.end()
        [call] = self.dream_calls()
        command = call.args[0]
        self.assertEqual(command[:5], ["/usr/bin/python3", "-I", str(kh.DREAM_SCRIPT), "maybe-start", "--domain"])
        self.assertEqual(command[5:9], ["personal/app", "--cwd", str(self.root), "--config"])
        self.assertEqual(json.loads(command[9]), {**kh.DEFAULTS["dream"], "enabled": True})
        self.assertTrue(call.kwargs["start_new_session"])
        self.assertEqual(call.kwargs["env"]["KIOKU_HOOKS"], "off")
        self.assertNotIn("CLAUDE_CODE_SESSION_ATTENDED", call.kwargs["env"])
        self.assertEqual(call.kwargs["env"]["KIOKU_HOME"], "/kioku")
        self.assertEqual(hook_events("dream-started")[-1]["domain"], "personal/app")

    def test_the_launcher_gets_the_checkout_the_config_names(self) -> None:
        kh.save_session("d10", placement={"vault": "personal", "area": "personal/app"})
        env = {k: v for k, v in os.environ.items() if k != "KIOKU_HOME"}
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(kh, "CONFIG", {**kh.CONFIG, "kioku_home": "/srv/kioku"}):
            self.end("d10")
        [call] = self.dream_calls()
        self.assertEqual(call.kwargs["env"]["KIOKU_HOME"], "/srv/kioku")

    def test_no_area_or_dreams_off_starts_nothing(self) -> None:
        FakeKioku.place = ("personal", None)
        self.end("d2")
        self.assertEqual(self.dream_calls(), [])
        FakeKioku.place = ("personal", "personal/app")
        self.config(dream={"enabled": False})
        self.end("d3")
        self.assertEqual(self.dream_calls(), [])

    def test_a_missing_script_is_logged_and_skipped(self) -> None:
        kh.DREAM_SCRIPT.unlink()
        self.end("d4")
        self.assertEqual(self.dream_calls(), [])
        self.assertEqual(hook_events("dream-skipped")[-1]["why"], "dream_state.py is missing")

    def announce(self, status: dict, session: str) -> dict:
        done = subprocess.CompletedProcess([], 0, stdout=json.dumps(status), stderr="")
        with mock.patch.object(kh.subprocess, "run", return_value=done) as run:
            out = self.capture(kh.session_start, {"session_id": session, "cwd": str(self.root)})
        self.assertEqual(run.call_args.args[0][3:], ["status", "--domain", "personal/app", "--json"])
        return json.loads(out)

    def test_session_start_announces_a_finished_run_once(self) -> None:
        kh.pending.PendingStore("personal").add("personal/app", "decision", "dream 2026-11-07", "merge a and b")
        kh.pending.PendingStore("personal").add("personal/app", "retire", "review 2026-11-07", "not the dream's")
        status = {"domain": "personal/app", "last_run": "2026-11-07T10:00:00Z", "status": "ok",
                  "report": "/reports/dream.md", "counts": {"saved": 5}, "scheduled": True}
        context = self.announce(status, "d5")["hookSpecificOutput"]["additionalContext"]
        self.assertIn("Kioku's dream of personal/app ran on", context)
        self.assertIn("it saved 5 notes, 1 of its items wait in the pending list, and its report is "
                      "/reports/dream.md.", context)
        self.assertNotIn("dream of", self.announce(status, "d6")["hookSpecificOutput"]["additionalContext"])

    def test_a_failed_run_points_at_its_log_and_a_running_one_waits(self) -> None:
        running = {"domain": "personal/app", "last_run": "2026-11-07T10:00:00Z", "status": "running"}
        self.assertNotIn("dream of", self.announce(running, "d7")["hookSpecificOutput"]["additionalContext"])
        failed = {**running, "last_run": "2026-11-07T11:00:00Z", "status": "failed"}
        output = self.announce(failed, "d8")
        self.assertIn("failed on", output["hookSpecificOutput"]["additionalContext"])
        self.assertIn("~/.local/state/kioku/dream/dream.log", output["systemMessage"])

    def test_a_dream_that_never_ran_says_nothing(self) -> None:
        self.assertNotIn("dream", self.announce({}, "d9")["hookSpecificOutput"]["additionalContext"])


# A Kioku server on stdio, for the client's own tests. FAKE_KIOKU_MODE picks its behaviour:
# "old" rejects count_use and path like servers before Kioku PRs #12 and #14, "broken"
# fails every tool call, "slow" answers tools after 5 seconds.
FAKE_SERVER = r'''#!/usr/bin/python3
import json, os, sys, time
mode = os.environ.get("FAKE_KIOKU_MODE", "")
record = os.environ.get("FAKE_KIOKU_LOG")

def reply(ident, result):
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": ident, "result": result}) + "\n")
    sys.stdout.flush()

def failure(ident, text):
    reply(ident, {"isError": True, "content": [{"type": "text", "text": text}]})

for line in sys.stdin:
    if record:
        with open(record, "a") as out:
            out.write(line)
    message = json.loads(line)
    if "id" not in message:
        continue
    if message["method"] == "initialize":
        # A notification first: the client must not lose the answer buffered behind it.
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/message", "params": {}}) + "\n")
        reply(message["id"], {"instructions": 'Kioku memory: this session is in area "personal/app" of vault "personal".'})
        continue
    name, args = message["params"]["name"], message["params"]["arguments"]
    if mode == "slow":
        time.sleep(5)
    if mode == "old" and ("count_use" in args or "path" in args):
        bad = "count_use" if "count_use" in args else "path"
        failure(message["id"], f'invalid_argument: {name} does not accept "{bad}". Allowed: keys, domain, '
                               f'max_bytes. Example: {name} {{"keys":["k"]}}')
    elif mode == "broken":
        failure(message["id"], "storage: connection to postgres://kioku:hunter2@localhost/kioku failed")
    else:
        body = {"memories": [{"key": "k", "title": "T"}], "notes": [], "arguments": args}
        reply(message["id"], {"content": [{"type": "text", "text": json.dumps(body)}]})
'''


class KiokuClientTest(TempState):
    mock_popen = False

    def setUp(self) -> None:
        super().setUp()
        server = self.root / "fake-kioku"
        server.write_text(FAKE_SERVER)
        server.chmod(0o755)
        self.requests = self.root / "requests.jsonl"
        self.environment = {"KIOKU_HOOK_BIN": str(server), "KIOKU_HOOK_ENV_FILE": str(self.root / "none.env"),
                            "FAKE_KIOKU_LOG": str(self.requests), "FAKE_KIOKU_MODE": ""}

    def server(self, mode: str = ""):
        return mock.patch.dict(os.environ, {**self.environment, "FAKE_KIOKU_MODE": mode})

    def sent(self, name: str) -> list[dict]:
        lines = self.requests.read_text().splitlines() if self.requests.exists() else []
        calls = [json.loads(text) for text in lines]
        return [c["params"]["arguments"] for c in calls if c.get("method") == "tools/call" and c["params"]["name"] == name]

    def test_talks_to_a_server_and_reads_its_placement(self) -> None:
        with self.server():
            kioku = kh.Kioku(str(self.root))
            try:
                self.assertEqual(kioku.placement(), ("personal", "personal/app"))
                self.assertEqual(kioku.tool("memory_get", {"keys": ["k"], "count_use": False})["arguments"],
                                 {"keys": ["k"], "count_use": False})
            finally:
                kioku.close()

    def test_an_old_server_gets_reads_without_count_use_for_the_rest_of_the_process(self) -> None:
        with self.server("old"):
            kioku = kh.Kioku(str(self.root))
            try:
                got = kioku.tool("memory_get", {"keys": ["k"], "count_use": False})
                self.assertEqual(got["memories"][0]["key"], "k")
                self.assertFalse(kh.Kioku.count_use_supported)
                kioku.tool("memory_get", {"keys": ["k"], "count_use": False})
            finally:
                kioku.close()
        self.assertEqual(self.sent("memory_get"), [{"keys": ["k"], "count_use": False}, {"keys": ["k"]}, {"keys": ["k"]}])
        self.assertEqual(len(hook_events("kioku-fallback")), 1)

    def test_an_old_server_turns_anchored_notes_off(self) -> None:
        repo = self.root / "repo"
        (repo / ".git").mkdir(parents=True)
        (repo / "a.py").write_text("")
        (repo / "b.py").write_text("")
        self.config()
        out = io.StringIO()
        with self.server("old"), contextlib.redirect_stdout(out):
            kh.pre_read({"session_id": "k1", "tool_input": {"file_path": str(repo / "a.py")}})
            kh.pre_read({"session_id": "k1", "tool_input": {"file_path": str(repo / "b.py")}})
        self.assertEqual(out.getvalue(), "")
        self.assertEqual(len(self.sent("memory_list")), 1)
        self.assertEqual(len(hook_events("kioku-old-server")), 1)
        self.assertIn('does not accept "path"', hook_events("kioku-error")[-1]["error"])

    def test_errors_are_logged_clipped_and_without_secrets(self) -> None:
        with self.server("broken"):
            kioku = kh.Kioku(str(self.root))
            try:
                self.assertEqual(kioku.tool("memory_search", {"query": "q"}), {})
                self.assertIn("hunter2", kioku.last_error)
            finally:
                kioku.close()
        record = hook_events("kioku-error")[-1]
        self.assertEqual(record["tool"], "memory_search")
        self.assertIn("postgres://***@localhost", record["error"])
        self.assertNotIn("hunter2", json.dumps(kh.read_lines("hooks.log")))

    def test_a_slow_server_times_out_at_the_deadline(self) -> None:
        with self.server("slow"):
            kioku = kh.Kioku(str(self.root))
            try:
                kh.DEADLINE.start(0.5)
                started = time.monotonic()
                with self.assertRaises(TimeoutError) as raised:
                    kioku.tool("memory_search", {"query": "q"})
                self.assertEqual(str(raised.exception), "memory_search")
                self.assertLess(time.monotonic() - started, 1.5)
            finally:
                kioku.close()

    def test_the_prompt_hook_logs_the_stage_it_timed_out_in(self) -> None:
        self.config()
        event = json.dumps({"session_id": "k2", "cwd": str(self.root), "prompt": "how do reviews cap their cost?"})
        out = io.StringIO()
        started = time.monotonic()
        with self.server("slow"), mock.patch.dict(kh.HOOK_SECONDS, {"prompt": 0.8}), \
                mock.patch("sys.stdin", io.StringIO(event)), contextlib.redirect_stdout(out):
            self.assertEqual(kh.main(["kioku_hook.py", "prompt"]), 0)
        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual(out.getvalue(), "")
        record = hook_events("timeout")[-1]
        self.assertEqual((record["hook"], record["stage"]), ("prompt", "memory_search"))
        self.assertEqual(kh.DEADLINE.until, None)


class MainTest(TempState):
    def test_kioku_hooks_off_silences_every_hook(self) -> None:
        path = self.transcript([assistant(tool_use("Bash", command="git commit -m x"))])
        event = json.dumps({"session_id": "s9", "transcript_path": str(path)})
        out = io.StringIO()
        env = {"KIOKU_HOOKS": "off", "CLAUDE_CODE_SESSION_ATTENDED": "1"}
        with mock.patch.dict(os.environ, env), mock.patch("sys.stdin", io.StringIO(event)), \
                contextlib.redirect_stdout(out):
            self.assertEqual(kh.main(["kioku_hook.py", "stop"]), 0)
        self.assertEqual(out.getvalue(), "")

    def test_errors_are_logged_not_raised(self) -> None:
        with mock.patch("sys.stdin", io.StringIO("{not json")):
            self.assertEqual(kh.main(["kioku_hook.py", "prompt"]), 0)
        self.assertEqual(kh.read_lines("hooks.log")[-1]["event"], "error")

    def test_without_kioku_home_the_hooks_say_so_and_stay_off(self) -> None:
        env = {k: v for k, v in os.environ.items() if k not in ("KIOKU_HOME", "KIOKU_HOOK_BIN", "KIOKU_HOOK_ENV_FILE")}

        def run(hook: str) -> str:
            out = io.StringIO()
            event = json.dumps({"session_id": "h1", "cwd": str(self.root), "prompt": "how do refund retries work?"})
            with mock.patch.dict(os.environ, env, clear=True), \
                    mock.patch.object(kh, "CONFIG", {**kh.CONFIG, "kioku_home": ""}), \
                    mock.patch.object(kh, "Kioku", side_effect=AssertionError("started a server")), \
                    mock.patch("sys.stdin", io.StringIO(event)), contextlib.redirect_stdout(out):
                self.assertEqual(kh.main(["kioku_hook.py", hook]), 0)
            return out.getvalue()

        message = json.loads(run("session-start"))["systemMessage"]
        self.assertIn("KIOKU_HOME is not set", message)
        self.assertEqual(run("prompt"), "")
        self.assertEqual(len(hook_events("no-kioku-home")), 1)
        self.assertEqual(hook_events("error"), [])


class CodemapSummarizeTest(unittest.TestCase):
    request = {"repo": "/work/app", "files": [{"path": "a.py", "language": "python", "text": "x = 1"}]}

    def run_main(self, stdout: str, code: int = 0) -> tuple[int, str, str]:
        done = subprocess.CompletedProcess([], code, stdout=stdout, stderr="boom" if code else "")
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(cs.subprocess, "run", return_value=done), \
                mock.patch("sys.stdin", io.StringIO(json.dumps(self.request))), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            result = cs.main()
        return result, out.getvalue(), err.getvalue()

    def test_a_good_reply_prints_the_purposes(self) -> None:
        reply = {"type": "result", "subtype": "success",
                 "result": '```json\n{"purposes": [{"path": "a.py", "purpose": "Holds x."}]}\n```'}
        code, out, err = self.run_main(json.dumps(reply))
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(json.loads(out), {"purposes": [{"path": "a.py", "purpose": "Holds x."}]})

    def test_a_bad_reply_is_one_error_line_and_a_failure(self) -> None:
        for stdout in ("not json at all", json.dumps([1, 2]),
                       json.dumps({"type": "result", "subtype": "success", "result": "I can't help with that."}),
                       json.dumps({"type": "result", "subtype": "error_max_turns", "result": None})):
            code, out, err = self.run_main(stdout)
            self.assertEqual(code, 1, stdout)
            self.assertEqual(out, "")
            self.assertTrue(err.startswith("codemap summarizer: "), err)
            self.assertEqual(err.count("\n"), 1, err)

    def test_a_failed_claude_is_a_failure(self) -> None:
        code, _, err = self.run_main("", code=1)
        self.assertEqual(code, 1)
        self.assertIn("claude -p failed (1): boom", err)


if __name__ == "__main__":
    unittest.main()
