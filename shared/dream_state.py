#!/usr/bin/env python3
"""State, schedule and launcher of the dream skill's runs.

  dream_state.py mark --domain D --status ok|failed --report <path> [--counts '<json>'] [--scheduled]
  dream_state.py status --domain D [--json]
  dream_state.py maybe-start --domain D --cwd <folder> --config '<json: every_days, max_budget_usd, max_topics, max_verify>'

State:   ~/.local/state/kioku/dream/<domain with / as ->.json
Reports: ~/.local/state/kioku/dream/reports/<domain with / as ->-<YYYYMMDD-HHMM>.md
Log:     ~/.local/state/kioku/dream/dream.log

`status --json` prints {} for a domain that never dreamed, else its last run:
domain, last_run (UTC ISO 8601), status, report, counts and scheduled, plus
last_ok and last_failed, the times the schedule reads.

`maybe-start` returns at once. It starts a detached headless dream only when the
domain is due: its last successful run is older than every_days (never for a
domain with no successful run: the first dream is manual), no run is in
progress (a lock holding the run's pid and start time, stale after 6 hours),
and no run failed in the last 24 hours. The detached process holds the lock,
runs `claude -p` in the folder, and records a failed run when the run ended
without recording one itself. A lock whose process is gone is recorded as a
failed run too, so a crashing run waits a day before the next try.

The run needs $KIOKU_HOME, the Kioku checkout, and the kioku-memory plugin's
skills folder: this script's own when it is that plugin's copy, else
$KIOKU_SKILLS_DIR, else the copy of kioku-memory that Claude Code installed.
The kioku-hooks plugin carries a copy of this file for its session-end trigger;
both are vendored from the marketplace's shared/ folder.

Runs on /usr/bin/python3 (3.9), standard library only.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

SCRIPT = Path(__file__).resolve()
LOCK_STALE = timedelta(hours=6)
FAILURE_BACKOFF = timedelta(hours=24)
RUN_TIMEOUT_SECONDS = int(LOCK_STALE.total_seconds())
LOG_BYTES = 1_000_000
DEFAULTS = {"every_days": 7, "max_budget_usd": 5.0, "max_topics": 2, "max_verify": 12}
DOMAIN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*(/[A-Za-z0-9][A-Za-z0-9._-]*)*$")
# Variables of the Claude Code session that started the hook; the run is its own session.
SESSION_ENV = (
    "CLAUDECODE", "CLAUDE_CODE_CHILD_SESSION", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_EXECPATH",
    "CLAUDE_CODE_MESSAGING_SOCKET", "CLAUDE_CODE_MESSAGING_TOKEN", "CLAUDE_CODE_SESSION_ATTENDED",
    "CLAUDE_CODE_SESSION_ID", "CLAUDE_EFFORT", "CLAUDE_ENV_FILE", "CLAUDE_PID", "CLAUDE_PROJECT_DIR",
    "CLAUDE_PLUGIN_ROOT",
)
# Built-in tools of the run. Its workflow agents inherit them: Bash for git ls-files,
# Read for the skill files, Write for the report, StructuredOutput for agents' results.
RUN_TOOLS = ("Bash", "Read", "Write", "Workflow", "ToolSearch", "StructuredOutput")
KIOKU_TOOLS = ("memory_list", "memory_get", "memory_search", "memory_save", "memory_inspect", "code_map")
# The run's own Kioku server: the core tools, plus the listing, the exact update time and the code map.
KIOKU_SERVE_TOOLS = "core,memory_list,memory_inspect,code_map"


# --- paths and time -------------------------------------------------------------

def base_dir() -> Path:
    return Path.home() / ".local" / "state" / "kioku" / "dream"


def slug(domain: str) -> str:
    return domain.replace("/", "-")


def state_path(domain: str) -> Path:
    return base_dir() / f"{slug(domain)}.json"


def lock_path(domain: str) -> Path:
    return base_dir() / f"{slug(domain)}.lock"


def log_path() -> Path:
    return base_dir() / "dream.log"


def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(text: str) -> datetime:
    """An ISO 8601 time; Python 3.9's fromisoformat doesn't read a Z suffix."""
    moment = datetime.fromisoformat(str(text).strip().replace("Z", "+00:00"))
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temp.write_text(json.dumps(value, indent=1), encoding="utf-8")
    temp.replace(path)


def log(event: str, **fields) -> None:
    path = log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if path.stat().st_size > LOG_BYTES:
            path.replace(path.with_name(path.name + ".1"))
    except OSError:
        pass
    with path.open("a", encoding="utf-8") as out:
        out.write(json.dumps({"at": iso(now_utc()), "event": event, **fields}) + "\n")


def check_domain(domain: str) -> str:
    if not DOMAIN.match(domain or "") or ".." in domain:
        raise SystemExit(f"not a Kioku domain: {domain!r}")
    return domain


# --- state ----------------------------------------------------------------------

def read_state(domain: str) -> dict:
    state = read_json(state_path(domain), {})
    return state if isinstance(state, dict) else {}


def mark(domain: str, status: str, report: str, counts: Optional[dict], scheduled: bool,
         moment: Optional[datetime] = None) -> dict:
    moment = moment or now_utc()
    state = read_state(domain)
    state.update({"domain": domain, "last_run": iso(moment), "status": status, "report": str(report),
                  "counts": counts or {}, "scheduled": bool(scheduled)})
    state["last_ok" if status == "ok" else "last_failed"] = iso(moment)
    write_json(state_path(domain), state)
    log("marked", domain=domain, status=status, report=str(report), scheduled=bool(scheduled))
    return state


def status_view(state: dict) -> dict:
    if not state.get("last_run"):
        return {}
    keys = ("domain", "last_run", "status", "report", "counts", "scheduled", "last_ok", "last_failed")
    return {key: state.get(key) for key in keys}


# --- the schedule ---------------------------------------------------------------

def pid_alive(pid) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def lock_held(lock: Optional[dict], moment: datetime) -> bool:
    """A lock holds while it is younger than 6 hours and its process lives."""
    if not isinstance(lock, dict):
        return False
    try:
        started = parse_iso(lock["started"])
    except (KeyError, TypeError, ValueError):
        return False
    return moment - started < LOCK_STALE and pid_alive(lock.get("pid"))


def due(state: dict, lock: Optional[dict], moment: datetime, every_days: float) -> tuple:
    """Whether a scheduled dream should start now, and why."""
    if lock_held(lock, moment):
        return False, f"a run started at {lock.get('started')} is in progress"
    last_ok = state.get("last_ok")
    if not last_ok:
        return False, "no successful dream yet: the first dream is manual"
    last_failed = state.get("last_failed")
    if last_failed and moment - parse_iso(last_failed) < FAILURE_BACKOFF:
        return False, f"a run failed at {last_failed}, less than 24 hours ago"
    if moment - parse_iso(last_ok) < timedelta(days=every_days):
        return False, f"the last dream was at {last_ok}, less than {every_days:g} days ago"
    return True, f"the last dream was at {last_ok}"


def parse_config(text: str) -> dict:
    try:
        given = json.loads(text or "{}")
    except ValueError as error:
        raise SystemExit(f"--config is not JSON: {error}") from None
    if not isinstance(given, dict):
        raise SystemExit("--config must be a JSON object")
    config = {**DEFAULTS, **{k: v for k, v in given.items() if k in DEFAULTS and v is not None}}
    for key in ("every_days", "max_budget_usd"):
        if not isinstance(config[key], (int, float)) or isinstance(config[key], bool) or config[key] <= 0:
            raise SystemExit(f"--config {key} must be a positive number")
    for key in ("max_topics", "max_verify"):
        if not isinstance(config[key], int) or isinstance(config[key], bool) or config[key] < 1:
            raise SystemExit(f"--config {key} must be a whole number of at least 1")
    return config


# --- the run ----------------------------------------------------------------------

def claude_binary() -> str:
    return shutil.which("claude") or str(Path.home() / ".local" / "bin" / "claude")


def kioku_home() -> Optional[Path]:
    """The Kioku checkout, from $KIOKU_HOME; None when it is unset."""
    home = os.environ.get("KIOKU_HOME") or ""
    return Path(home).expanduser() if home.strip() else None


def has_dream_skill(folder: Path) -> bool:
    return (folder / "dream" / "SKILL.md").is_file()


def installed_skills() -> Optional[Path]:
    """The skills folder of the kioku-memory plugin Claude Code installed, user scope first."""
    record = read_json(Path.home() / ".claude" / "plugins" / "installed_plugins.json", {})
    plugins = record.get("plugins") if isinstance(record, dict) else None
    found = []
    for name, installs in (plugins.items() if isinstance(plugins, dict) else []):
        if str(name).split("@")[0] != "kioku-memory" or not isinstance(installs, list):
            continue
        for install in installs:
            if isinstance(install, dict) and install.get("installPath"):
                found.append((install.get("scope") != "user", Path(str(install["installPath"])) / "skills"))
    for _, folder in sorted(found, key=lambda pair: pair[0]):
        if has_dream_skill(folder):
            return folder
    return None


def skills_dir() -> Optional[Path]:
    """The kioku-memory plugin's skills folder, which holds the dream skill and the scripts a run uses."""
    own = SCRIPT.parent.parent.parent
    if has_dream_skill(own):
        return own
    given = os.environ.get("KIOKU_SKILLS_DIR") or ""
    if given.strip():
        folder = Path(given).expanduser()
        return folder if has_dream_skill(folder) else None
    return installed_skills()


def read_rule(folder: Path) -> str:
    """A permission rule that lets the run read a folder: ~/ for the home folder, // for any other absolute path."""
    try:
        return f"Read(~/{folder.relative_to(Path.home()).as_posix()}/**)"
    except ValueError:
        return f"Read(/{folder.as_posix()}/**)"


def run_prompt(domain: str, config: dict, report: Path, run_dir: Path, started: str, since: str,
               skills: Path) -> str:
    return f"""Run the dream skill on the Kioku domain {domain}, in its scheduled mode. Nobody attends this run: \
never ask a question, and where the skill would ask, take the choice that writes less.

Read {skills / "dream" / "SKILL.md"} and follow it, with its "Scheduled mode" section, and these settings:
- domain: {domain}
- startedAt: {started}
- since (the last successful dream): {since}
- maxTopics: {config['max_topics']}
- maxVerify: {config['max_verify']}
- run folder, for result.json and the retrieval reports: {run_dir}
- report: {report}

This run may only use the commands its allowlist names, so run each script as python3 -I with its absolute path:
- {skills / "project-sync" / "scripts" / "file_pending.py"}
- {skills / "reflect" / "scripts" / "retrieval_check.py"} (run and compare)
- {SCRIPT} (mark and status)

Record the run when it ends, also when it fails:
python3 -I {SCRIPT} mark --domain {domain} --status ok|failed --report {report} \
--counts '<the workflow counts as JSON>' --scheduled
"""


def allowed_tools(skills: Path) -> list:
    filer = skills / "project-sync" / "scripts" / "file_pending.py"
    check = skills / "reflect" / "scripts" / "retrieval_check.py"
    return [
        read_rule(skills), "Read(~/.config/kioku/**)", "Read(~/.local/state/kioku/**)",
        "Write(~/.local/state/kioku/dream/**)",
        "Workflow", "ToolSearch",
        *[f"mcp__kioku__{tool}" for tool in KIOKU_TOOLS],
        f"Bash(python3 -I {SCRIPT} mark *)", f"Bash(python3 -I {SCRIPT} status *)",
        f"Bash(python3 -I {filer} *)",
        f"Bash(python3 -I {check} run *)", f"Bash(python3 -I {check} compare *)",
        "Bash(git -C * ls-files *)",
    ]


def run_command(domain: str, cwd: str, config: dict, report: Path, run_dir: Path, started: str,
                since: str) -> tuple:
    """The headless Claude of a scheduled dream, and its environment."""
    home, skills = kioku_home(), skills_dir()
    if home is None:
        raise RuntimeError("KIOKU_HOME is not set")
    if skills is None:
        raise RuntimeError("the kioku-memory plugin's dream skill was not found")
    servers = {"mcpServers": {"kioku": {
        "type": "stdio", "command": str(home / "bin" / "kioku"),
        "args": ["--env-file", str(home / ".env"), "serve", "--tools", KIOKU_SERVE_TOOLS],
        "env": {"MEMORY_PROVIDER": "anthropic", "KIOKU_SESSION_DIR": cwd}}}}
    command = [claude_binary(), "-p", run_prompt(domain, config, report, run_dir, started, since, skills),
               "--model", "opus", "--permission-mode", "dontAsk",
               "--max-budget-usd", f"{config['max_budget_usd']:g}",
               "--strict-mcp-config", "--mcp-config", json.dumps(servers),
               "--output-format", "json",
               "--tools", ",".join(RUN_TOOLS),
               "--allowedTools", *allowed_tools(skills)]
    env = {name: value for name, value in os.environ.items() if name not in SESSION_ENV}
    env["KIOKU_HOOKS"] = "off"
    env["CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS"] = "0"
    return command, env


def owns_lock(domain: str, report: Path) -> bool:
    """A lock belongs to the run whose report path it names: each run has its own."""
    lock = read_json(lock_path(domain), None)
    return isinstance(lock, dict) and lock.get("report") == str(report)


def claim(domain: str, report: Path, pid: int, started: str) -> None:
    """Name the process that holds the run's lock, while the lock is still the run's."""
    if owns_lock(domain, report):
        write_json(lock_path(domain), {"pid": pid, "started": started, "report": str(report)})


def release(domain: str, report: Path) -> None:
    if owns_lock(domain, report):
        try:
            lock_path(domain).unlink()
        except OSError:
            pass


def outcome_of(output: Path) -> dict:
    """What `claude -p --output-format json` said about the run, if anything."""
    result = read_json(output, None)
    if not isinstance(result, dict):
        return {}
    return {k: result.get(k) for k in ("subtype", "is_error", "num_turns", "total_cost_usd") if k in result}


def same_path(given, path: Path) -> bool:
    try:
        return bool(given) and Path(str(given)).expanduser().resolve() == path.expanduser().resolve()
    except (OSError, RuntimeError):
        return False


def run_dream(domain: str, cwd: str, config: dict, report: Path, run_dir: Path, started: str, since: str) -> int:
    """The detached run: hold the lock, run Claude, and record a failure it didn't record."""
    code: object = None
    outcome: dict = {}
    claim(domain, report, os.getpid(), started)
    try:
        try:
            command, env = run_command(domain, cwd, config, report, run_dir, started, since)
            run_dir.mkdir(parents=True, exist_ok=True)
            report.parent.mkdir(parents=True, exist_ok=True)
            output, errors = run_dir / "claude-output.json", run_dir / "claude-stderr.log"
            with output.open("w", encoding="utf-8") as out, errors.open("w", encoding="utf-8") as err:
                try:
                    code = subprocess.run(command, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=out,
                                          stderr=err, timeout=RUN_TIMEOUT_SECONDS).returncode
                except subprocess.TimeoutExpired:
                    code = "timeout"
            outcome = outcome_of(output)
        except Exception as error:  # noqa: BLE001 - whatever stops the run is recorded below, not lost
            code = f"the run did not start: {error}"
        state = read_state(domain)
        recorded = same_path(state.get("report"), report) and str(state.get("last_run") or "") >= started
        if not recorded:
            try:
                if not report.exists():
                    report.parent.mkdir(parents=True, exist_ok=True)
                    report.write_text(f"# Dream of {domain}: failed\n\nThe scheduled run started at {started} ended "
                                      f"without recording a result (exit {code}). Claude's output is in {run_dir}.\n",
                                      encoding="utf-8")
            except OSError:
                pass
            mark(domain, "failed", str(report), {"exit": code, **outcome}, True)
        log("run-finished", domain=domain, exit=code, recorded=recorded, **outcome)
        return 0
    finally:
        release(domain, report)


def start(domain: str, cwd: str, config: dict, moment: Optional[datetime] = None) -> dict:
    """Start a detached scheduled dream when one is due."""
    moment = moment or now_utc()
    state = read_state(domain)
    lock = read_json(lock_path(domain), None)
    if lock is not None and not lock_held(lock, moment):
        # A run that ended without releasing its lock: record it, so the failure backoff applies.
        started = (lock or {}).get("started") if isinstance(lock, dict) else None
        if not (started and str(state.get("last_run") or "") >= str(started)):
            report = (lock or {}).get("report") if isinstance(lock, dict) else None
            state = mark(domain, "failed", report or "(none)",
                         {"error": "the run's process ended without recording a result"}, True, moment)
        try:
            lock_path(domain).unlink()
        except OSError:
            pass
        lock = None
    is_due, why = due(state, lock, moment, config["every_days"])
    if is_due and kioku_home() is None:
        is_due, why = False, "KIOKU_HOME is not set: set it to the Kioku checkout"
    if is_due and skills_dir() is None:
        is_due, why = False, "the kioku-memory plugin's dream skill was not found: install it, or set KIOKU_SKILLS_DIR"
    if not is_due:
        log("not-started", domain=domain, why=why)
        return {"started": False, "why": why}
    stamp = moment.strftime("%Y%m%d-%H%M")
    report = base_dir() / "reports" / f"{slug(domain)}-{stamp}.md"
    run_dir = base_dir() / "runs" / f"{slug(domain)}-{stamp}"
    started = iso(moment)
    try:
        lock_path(domain).parent.mkdir(parents=True, exist_ok=True)
        handle = os.open(lock_path(domain), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        log("not-started", domain=domain, why="another run took the lock")
        return {"started": False, "why": "another run took the lock"}
    with os.fdopen(handle, "w", encoding="utf-8") as out:
        json.dump({"pid": os.getpid(), "started": started, "report": str(report)}, out)
    since = state.get("last_ok") or "none"
    process = subprocess.Popen([sys.executable, "-I", str(SCRIPT), "_run", "--domain", domain, "--cwd", cwd,
                                "--config", json.dumps(config), "--report", str(report), "--run-dir", str(run_dir),
                                "--started", started, "--since", since],
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               cwd=cwd, start_new_session=True)
    claim(domain, report, process.pid, started)
    log("started", domain=domain, pid=process.pid, report=str(report), why=why, config=config)
    return {"started": True, "why": why, "report": str(report), "pid": process.pid}


# --- command line -----------------------------------------------------------------

def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    marker = commands.add_parser("mark", help="record a run")
    marker.add_argument("--domain", required=True)
    marker.add_argument("--status", required=True, choices=["ok", "failed"])
    marker.add_argument("--report", required=True)
    marker.add_argument("--counts", help="the run's counts, as a JSON object")
    marker.add_argument("--scheduled", action="store_true")
    status = commands.add_parser("status", help="the domain's last run")
    status.add_argument("--domain", required=True)
    status.add_argument("--json", action="store_true")
    starter = commands.add_parser("maybe-start", help="start a scheduled dream when one is due")
    starter.add_argument("--domain", required=True)
    starter.add_argument("--cwd", required=True)
    starter.add_argument("--config", required=True)
    runner = commands.add_parser("_run", help=argparse.SUPPRESS)
    for name in ("--domain", "--cwd", "--config", "--report", "--run-dir", "--started", "--since"):
        runner.add_argument(name, required=True)
    args = parser.parse_args(argv)
    domain = check_domain(args.domain)
    if args.command == "mark":
        try:
            counts = json.loads(args.counts) if args.counts else {}
        except ValueError as error:
            raise SystemExit(f"--counts is not JSON: {error}") from None
        if not isinstance(counts, dict):
            raise SystemExit("--counts must be a JSON object")
        print(json.dumps(status_view(mark(domain, args.status, args.report, counts, args.scheduled))))
        return 0
    if args.command == "status":
        view = status_view(read_state(domain))
        if args.json:
            print(json.dumps(view))
        elif not view:
            print(f"{domain} never dreamed")
        else:
            print(f"{domain}: last run {view['last_run']} ({view['status']}{', scheduled' if view['scheduled'] else ''}), "
                  f"report {view['report']}")
        return 0
    if args.command == "maybe-start":
        if not Path(args.cwd).is_dir():
            log("not-started", domain=domain, why=f"no folder {args.cwd}")
            print(json.dumps({"started": False, "why": f"no folder {args.cwd}"}))
            return 0
        print(json.dumps(start(domain, str(Path(args.cwd).resolve()), parse_config(args.config))))
        return 0
    return run_dream(domain, args.cwd, parse_config(args.config), Path(args.report), Path(args.run_dir),
                     args.started, args.since)


if __name__ == "__main__":
    sys.exit(main())
