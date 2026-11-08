#!/usr/bin/env python3
"""Deterministic inventory for the project-sync skill.

What changed in a project's repositories since its last sync, read from git
and GitHub (through `gh`) and cut into batches for the workflow's agents.
Slack, Linear and documents are read by agents through their connectors.
Read-only: it never writes to a repository.

  # Build the inventory for one Kioku area:
  sync_inventory.py --domain personal/notes --sources ~/.config/kioku/sync/personal.json \\
      --state ~/.cache/kioku-sync/personal-notes.json --out inventory.json [--fetch] [--max-verify 30]

  # After a run completed, record where the next one starts, what it left
  # unverified, and what it cost:
  sync_inventory.py --mark ~/.cache/kioku-sync/personal-notes.json --inventory inventory.json \\
      --result result.json [--stats '{"agents": 12, "tokens": 900000}']

The sources file maps each area to its repositories and connector sources:

  {"personal/notes": {
     "repos": [{"path": "~/src/notes-app", "github": "owner/name", "branch": "origin/main"}],
     "slack": [{"id": "C0123", "name": "#notes-app"}],
     "linear": {"team": "ENG", "project": "Notes"},
     "docs": [{"kind": "notion", "id": "...", "title": "..."}],
     "confirmed": "2026-11-07"}}

The state file holds a cursor per source, the proposals the last run left
unverified ("carried": the next inventory hands them back), and the agents and
tokens of past runs ("runs"), from which the inventory estimates a run's cost.

The result file is the workflow's return value. --mark refuses a result that
names failed sources (a harvest source or a topic's reconcile), so the next run
reads the same window again, and keeps the cursor of each source the result
calls unavailable.
"""

from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

FIRST_RUN_MONTHS = 3
COMMITS_PER_BATCH = 40
PRS_PER_BATCH = 8
BATCH_CHARS = 60_000
BODY_CHARS = 6_000
COMMENT_CHARS = 1_500
CODEMAP_FILES = 60
SOURCE_SUFFIXES = (".rs", ".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".go", ".tf", ".hcl", ".sql", ".sh")

# gh reads at most this many PRs per state; a list that reaches it is truncated.
PR_LIMITS = {"merged": 200, "closed": 100}
# PRs are read from a little before the cursor, so one GitHub's search hadn't
# indexed yet when the cursor was set is still read.
PR_OVERLAP = timedelta(hours=1)
# The workflow's fixed agents, for the estimate: merge, reconcile, apply, report.
MERGE_AGENTS, RECONCILE_AGENTS, CLOSING_AGENTS = 1, 8, 2
DEFAULT_MAX_VERIFY = 30
CONFIRM_TOKENS = 1_500_000
KEPT_RUNS = 20


def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(text: str) -> datetime:
    """An ISO 8601 time; Python 3.9's fromisoformat doesn't read a Z suffix."""
    moment = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, indent=1), encoding="utf-8")
    temp.replace(path)


def run(args: list[str], cwd: Optional[Path] = None) -> str:
    proc = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"{' '.join(args[:3])} failed: {proc.stderr.strip()[:300]}")
    return proc.stdout


def git(repo: Path, *args: str) -> str:
    return run(["git", "-C", str(repo), *args])


def default_branch(repo: Path) -> str:
    try:
        return git(repo, "symbolic-ref", "--short", "refs/remotes/origin/HEAD").strip()
    except RuntimeError:
        return "HEAD"


def has_commit(repo: Path, commit: str) -> bool:
    try:
        git(repo, "cat-file", "-e", f"{commit}^{{commit}}")
        return True
    except RuntimeError:
        return False


def clip(text: str, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


# --- git ------------------------------------------------------------------------

FIELD, RECORD = "\x1f", "\x1e"


def parse_log(out: str) -> list[dict]:
    """`git log --numstat --format=%x1e%H%x1f%an%x1f%aI%x1f%s%x1f%b` output: each record
    opens with the separator, so a commit's numstat lines stay in its own record."""
    commits = []
    for record in out.split(RECORD):
        record = record.strip("\n")
        if not record.strip():
            continue
        fields = record.split(FIELD, 4)
        if len(fields) < 5:
            continue
        sha, author, date, subject = fields[0].strip(), fields[1], fields[2], fields[3]
        body_and_stats = fields[4]
        # The body ends where the numstat lines (added<TAB>deleted<TAB>path) begin.
        lines = body_and_stats.split("\n")
        body_lines, files = [], []
        for line in lines:
            parts = line.split("\t")
            if len(parts) == 3 and (parts[0].isdigit() or parts[0] == "-") and (parts[1].isdigit() or parts[1] == "-"):
                changed = sum(int(x) for x in parts[:2] if x.isdigit())
                files.append({"path": parts[2], "changed": changed})
            else:
                body_lines.append(line)
        commits.append({"sha": sha, "author": author, "date": date, "subject": subject,
                        "body": "\n".join(body_lines).strip(), "files": files})
    return commits


def commits_since(repo: Path, branch: str, since_commit: Optional[str], since_date: str) -> list[dict]:
    fmt = f"--format={RECORD}%H{FIELD}%an{FIELD}%aI{FIELD}%s{FIELD}%b"
    if since_commit and has_commit(repo, since_commit):
        selection = [f"{since_commit}..{branch}"]
    else:
        selection = [f"--since={since_date}", branch]
    out = git(repo, "log", "--no-merges", "--numstat", fmt, *selection)
    return parse_log(out)


# --- GitHub ---------------------------------------------------------------------

def search_time(moment: str) -> str:
    """A cursor as GitHub's search writes a time, moved back by the overlap."""
    return (parse_iso(moment) - PR_OVERLAP).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def prs_since(github: str, since: str) -> tuple[list[dict], list[dict]]:
    """Merged PRs, and PRs closed without merging (often a rejected option), with the
    lists gh cut at their limit. Each list is read oldest-created first, so when one is
    cut, every PR merged or closed before the newest creation time read was read: a PR
    is never merged or closed before it is created."""
    fields = "number,title,body,url,author,createdAt,mergedAt,closedAt,state,labels,comments,reviews,files"
    stamp = search_time(since)
    queries = {"merged": f"merged:>={stamp}", "closed": f"closed:>={stamp} is:unmerged"}
    lists, truncated = {}, []
    for state, query in queries.items():
        limit = PR_LIMITS[state]
        found = json.loads(run(["gh", "pr", "list", "--repo", github, "--state", state, "--limit", str(limit),
                                "--search", f"{query} sort:created-asc", "--json", fields]) or "[]")
        lists[state] = found
        if len(found) >= limit:
            truncated.append({"repo": github, "state": state, "limit": limit,
                              "until": max(pr.get("createdAt") or "" for pr in found)})
    for pr in lists["closed"]:
        pr["rejected"] = True
    merged_numbers = {pr.get("number") for pr in lists["merged"]}
    prs = lists["merged"] + [pr for pr in lists["closed"] if pr.get("number") not in merged_numbers]
    return sorted(prs, key=lambda pr: pr.get("mergedAt") or pr.get("closedAt") or ""), truncated


def pr_text(pr: dict) -> str:
    when = pr.get("mergedAt") or pr.get("closedAt") or ""
    status = "closed without merging" if pr.get("rejected") else "merged"
    lines = [f"### PR #{pr.get('number')}: {pr.get('title')} ({status} {when[:10]}, {pr.get('url')})",
             f"Author: {(pr.get('author') or {}).get('login', '?')}"]
    labels = [label.get("name") for label in pr.get("labels") or []]
    if labels:
        lines.append(f"Labels: {', '.join(labels)}")
    lines.append(clip(pr.get("body") or "(no description)", BODY_CHARS))
    talk = []
    for comment in pr.get("comments") or []:
        talk.append(f"- {(comment.get('author') or {}).get('login', '?')}: {clip(comment.get('body'), COMMENT_CHARS)}")
    for review in pr.get("reviews") or []:
        if (review.get("body") or "").strip():
            talk.append(f"- review by {(review.get('author') or {}).get('login', '?')} ({review.get('state')}): "
                        f"{clip(review.get('body'), COMMENT_CHARS)}")
    if talk:
        lines.append("Discussion:\n" + "\n".join(talk))
    files = [f.get("path") for f in pr.get("files") or []][:30]
    if files:
        lines.append(f"Files: {', '.join(files)}" + (" …" if len(pr.get("files") or []) > 30 else ""))
    return "\n".join(lines)


def commit_text(commit: dict) -> str:
    files = ", ".join(f["path"] for f in commit["files"][:12]) + (" …" if len(commit["files"]) > 12 else "")
    body = f"\n{clip(commit['body'], 1500)}" if commit["body"] else ""
    return f"- {commit['sha'][:9]} {commit['date'][:10]} {commit['author']}: {commit['subject']}{body}\n  files: {files}"


def batches(kind: str, repo: str, texts: list[str], per_batch: int) -> list[dict]:
    out, current, size = [], [], 0
    for text in texts:
        if current and (len(current) >= per_batch or size + len(text) > BATCH_CHARS):
            out.append(current)
            current, size = [], 0
        current.append(text)
        size += len(text)
    if current:
        out.append(current)
    return [{"id": f"{kind}-{Path(repo).name}-{index + 1}", "kind": kind, "repo": repo, "items": len(chunk),
             "text": "\n\n".join(chunk)} for index, chunk in enumerate(out)]


# --- the code map -----------------------------------------------------------------

def codemap_purposes(repo: Path, commits: list[dict], kioku: Optional[Path], env_file: Optional[Path]) -> dict:
    """The code map's purpose of the source files these commits changed most."""
    if not kioku or not kioku.exists():
        return {}
    churn: Counter = Counter()
    for commit in commits:
        for file in commit["files"]:
            if file["path"].endswith(SOURCE_SUFFIXES):
                churn[file["path"]] += file["changed"] or 1
    purposes = {}
    for path, _ in churn.most_common(CODEMAP_FILES):
        if not (repo / path).is_file():
            continue
        args = [str(kioku)] + (["--env-file", str(env_file)] if env_file else []) + ["codemap", "show", str(repo / path)]
        try:
            entry = json.loads(run(args))
        except (RuntimeError, ValueError):
            continue
        if entry.get("purpose"):
            purposes[path] = entry["purpose"]
    return purposes


# --- the estimate -----------------------------------------------------------------

def recorded_runs(state: dict, state_dir: Optional[Path]) -> list[dict]:
    """The runs this area recorded, else every area's in the same state folder: what
    an agent costs is the workflow's, more than the area's."""
    runs = [r for r in state.get("runs") or [] if isinstance(r, dict)]
    if runs or not state_dir or not state_dir.is_dir():
        return runs
    for other in sorted(state_dir.glob("*.json")):
        runs += [r for r in (read_json(other, {}) or {}).get("runs") or [] if isinstance(r, dict)]
    return runs


def estimate(runs: list[dict], sources: int, carried: int, max_verify: int,
             confirm_above: int = CONFIRM_TOKENS) -> dict:
    """The run's planned agents times the recorded tokens per agent. Planned agents scale
    with the sources by the recorded agents per source, plus the carried proposals to
    verify, within the workflow's maximum. Without a recorded run there is no estimate,
    and the skill asks first."""
    verify_carried = min(carried, max_verify)
    if sources:
        most = sources + MERGE_AGENTS + RECONCILE_AGENTS + max_verify + CLOSING_AGENTS
    else:
        most = verify_carried + CLOSING_AGENTS if carried else 0
    agents = sum(max(0, int(r.get("agents") or 0)) for r in runs)
    tokens = sum(max(0, int(r.get("tokens") or 0)) for r in runs)
    if not most:
        return {"agents": 0, "tokens": 0, "tokens_per_agent": None, "from_runs": len(runs), "ask_first": False}
    if agents <= 0 or tokens <= 0:
        return {"agents": most, "tokens": None, "tokens_per_agent": None, "from_runs": 0, "ask_first": True}
    per_agent = tokens / agents
    with_sources = [r for r in runs if int(r.get("sources") or 0) > 0 and int(r.get("agents") or 0) > 0]
    if sources and with_sources:
        per_source = sum(int(r["agents"]) for r in with_sources) / sum(int(r["sources"]) for r in with_sources)
        planned = min(most, math.ceil(per_source * sources) + verify_carried)
    else:
        planned = most
    total = round(planned * per_agent)
    return {"agents": planned, "tokens": total, "tokens_per_agent": round(per_agent), "from_runs": len(runs),
            "ask_first": total > confirm_above}


def source_count(inventory: dict) -> int:
    """The workflow's harvest sources: one per batch and Slack channel, one for Linear, one for docs."""
    return (len(inventory.get("batches") or []) + len(inventory.get("slack") or [])
            + (1 if inventory.get("linear") else 0) + (1 if inventory.get("docs") else 0))


# --- inventory ---------------------------------------------------------------------

def build(domain: str, sources: dict, state: dict, fetch: bool, months: int,
          kioku: Optional[Path], env_file: Optional[Path], max_verify: int = DEFAULT_MAX_VERIFY,
          state_dir: Optional[Path] = None) -> dict:
    project = sources.get(domain)
    if not project:
        raise SystemExit(f"{domain} has no entry in the sources file; run the skill's source discovery first")
    started = now_utc()
    first_date = iso(started - timedelta(days=30 * months))
    last_run = state.get("last_run")
    carried = [p for p in state.get("carried") or [] if isinstance(p, dict)]
    inventory = {"domain": domain, "generated_at": iso(started), "first_run": last_run is None,
                 "since": last_run or first_date, "repos": [], "batches": [], "codemap": {},
                 "slack": [], "linear": None, "docs": [], "carried": carried, "truncated": [],
                 "next_state": {"domain": domain, "last_run": iso(started), "repos": {}, "slack": {}}}
    for entry in project.get("repos", []):
        path = Path(entry["path"]).expanduser()
        github = entry.get("github")
        repo_state = (state.get("repos") or {}).get(str(path), {})
        report = {"path": str(path), "github": github, "commits": 0, "prs": 0}
        # Each cursor moves only past what was read; one that wasn't read keeps its place.
        cursor = {k: repo_state[k] for k in ("commit", "at", "prs_since") if k in repo_state}
        if path.is_dir():
            if fetch:
                try:
                    git(path, "fetch", "-q", "origin")
                except RuntimeError as error:
                    report["fetch_error"] = str(error)
            branch = entry.get("branch") or default_branch(path)
            head = git(path, "rev-parse", branch).strip()
            commits = commits_since(path, branch, repo_state.get("commit"), repo_state.get("at") or first_date)
            report.update(branch=branch, head=head, commits=len(commits),
                          since_commit=repo_state.get("commit"))
            inventory["batches"] += batches("commits", str(path), [commit_text(c) for c in reversed(commits)],
                                            COMMITS_PER_BATCH)
            inventory["codemap"].update({f"{path.name}/{k}": v for k, v in
                                         codemap_purposes(path, commits, kioku, env_file).items()})
            cursor.update(commit=head, at=iso(started))
        else:
            report["missing"] = True
        if github:
            since = repo_state.get("prs_since") or repo_state.get("at") or first_date
            report["prs_since"] = since
            try:
                prs, cut = prs_since(github, since)
            except RuntimeError as error:
                # Pinned, so the commit cursor moving on doesn't move the PR window with it.
                prs, cut, report["github_error"] = [], [], str(error)
                cursor["prs_since"] = since
            else:
                cursor["prs_since"] = min(t["until"] for t in cut) if cut else iso(started)
            if cut:
                report["truncated"] = cut
                inventory["truncated"] += cut
            report["prs"] = len(prs)
            inventory["batches"] += batches("prs", github, [pr_text(pr) for pr in prs], PRS_PER_BATCH)
        if cursor:
            inventory["next_state"]["repos"][str(path)] = cursor
        inventory["repos"].append(report)
    slack_state = state.get("slack") or {}
    for channel in project.get("slack", []):
        oldest = slack_state.get(channel["id"]) or first_date
        inventory["slack"].append({**channel, "since": oldest})
        inventory["next_state"]["slack"][channel["id"]] = iso(started)
    if project.get("linear"):
        inventory["linear"] = {**project["linear"], "since": state.get("linear_since") or first_date}
        inventory["next_state"]["linear_since"] = iso(started)
    for doc in project.get("docs", []):
        inventory["docs"].append({**doc, "since": (state.get("docs") or {}).get(doc.get("id")) or first_date})
    inventory["next_state"]["docs"] = {doc.get("id"): iso(started) for doc in project.get("docs", [])}
    inventory["counts"] = {
        "commits": sum(r["commits"] for r in inventory["repos"]),
        "prs": sum(r["prs"] for r in inventory["repos"]),
        "batches": len(inventory["batches"]),
        "slack_channels": len(inventory["slack"]),
        "linear": bool(inventory["linear"]),
        "docs": len(inventory["docs"]),
        "codemap_files": len(inventory["codemap"]),
        "carried": len(carried),
        "truncated": len(inventory["truncated"]),
    }
    inventory["estimate"] = estimate(recorded_runs(state, state_dir), source_count(inventory), len(carried),
                                     max_verify)
    return inventory


# --- marking a run -----------------------------------------------------------------

def load_result(path: Path) -> dict:
    """The workflow's return value, also when a tool wrapped it as {"result": {...}}."""
    result = read_json(path, None)
    if isinstance(result, dict) and isinstance(result.get("result"), dict) and "counts" not in result:
        result = result["result"]
    if not isinstance(result, dict) or not any(k in result for k in ("counts", "unverified", "needs_user")):
        raise SystemExit(f"{path} is not a project-sync workflow result")
    return result


def failed_sources(result: dict) -> list[str]:
    failed = result.get("failed_sources")
    if isinstance(failed, list):
        return [str(item) for item in failed]
    counts = result.get("counts") or {}
    total, read = counts.get("sources"), counts.get("read")
    if isinstance(total, int) and isinstance(read, int) and read < total:
        return [f"{total - read} of {total} sources"]
    return []


def unavailable_sources(result: dict) -> list[str]:
    """Source ids, from [{"source": id, "reason": ...}] or older "id: reason" strings."""
    out = []
    for item in result.get("unavailable") or []:
        if isinstance(item, dict) and item.get("source"):
            out.append(str(item["source"]))
        elif isinstance(item, str) and ": " in item:
            out.append(item.split(": ", 1)[0].strip())
    return out


def keep_cursors(state: dict, previous: dict, inventory: dict, source_ids: list[str]) -> list[str]:
    """Put back the previous cursor of each source the run couldn't read."""
    kept = []
    batches_by_id = {b.get("id"): b for b in inventory.get("batches") or []}
    channels = {c.get("id") for c in inventory.get("slack") or []}

    def restore(table: dict, previous_table: dict, key: str, fields: tuple) -> None:
        current = table.setdefault(key, {})
        for name in fields:
            if name in previous_table.get(key, {}):
                current[name] = previous_table[key][name]
            else:
                current.pop(name, None)
        if not current:
            table.pop(key, None)

    for source in source_ids:
        channel = source[len("slack:"):] if source.startswith("slack:") else source
        if channel in channels:
            if channel in (previous.get("slack") or {}):
                state["slack"][channel] = previous["slack"][channel]
            else:
                state["slack"].pop(channel, None)
        elif source == "linear":
            if "linear_since" in previous:
                state["linear_since"] = previous["linear_since"]
            else:
                state.pop("linear_since", None)
        elif source == "docs":
            state["docs"] = dict(previous.get("docs") or {})
        elif source in batches_by_id:
            batch = batches_by_id[source]
            if batch.get("kind") == "commits":
                restore(state["repos"], previous.get("repos") or {}, batch.get("repo"), ("commit", "at"))
            else:
                for repo in inventory.get("repos") or []:
                    if repo.get("github") == batch.get("repo"):
                        restore(state["repos"], previous.get("repos") or {}, repo["path"], ("prs_since",))
        else:
            continue
        kept.append(source)
    return kept


def carry(result: dict, inventory: dict) -> list[dict]:
    """The proposals to verify next run: those over the verification cap and those whose
    save failed, each stamped with when it was drafted, so the next run's apply agent
    can tell whether the note changed since."""
    drafted = result.get("started_at") or inventory.get("generated_at")
    carried, seen = [], set()
    for proposal in list(result.get("unverified") or []) + list(result.get("retry") or []):
        if not isinstance(proposal, dict) or not proposal.get("key") or not proposal.get("action"):
            continue
        identity = (proposal.get("domain"), proposal["key"])
        if identity in seen:
            continue
        seen.add(identity)
        item = {k: v for k, v in proposal.items() if k not in ("verdict", "carried", "pending", "wait_reason")}
        item.setdefault("drafted_at", drafted)
        carried.append(item)
    return carried


def mark(state_path: Path, inventory_path: Path, result_path: Path, stats: Optional[dict] = None) -> dict:
    inventory = read_json(inventory_path, None)
    if not inventory or "next_state" not in inventory:
        raise SystemExit(f"{inventory_path} is not an inventory")
    result = load_result(result_path)
    failed = failed_sources(result)
    if failed:
        raise SystemExit(f"not marked: {', '.join(failed)} failed. "
                         "The state is unchanged, so the next run reads the same window again.")
    previous = read_json(state_path, {}) or {}
    state = json.loads(json.dumps(inventory["next_state"]))
    state.setdefault("repos", {})
    state.setdefault("slack", {})
    kept = keep_cursors(state, previous, inventory, unavailable_sources(result))
    state["carried"] = carry(result, inventory)
    runs = [r for r in previous.get("runs") or [] if isinstance(r, dict)]
    if stats:
        runs.append({"at": inventory.get("generated_at"), "agents": int(stats.get("agents") or 0),
                     "tokens": int(stats.get("tokens") or 0), "sources": source_count(inventory)})
    if runs:
        state["runs"] = runs[-KEPT_RUNS:]
    write_json(state_path, state)
    return {"marked": str(state_path), "last_run": state.get("last_run"), "carried": len(state["carried"]),
            "cursors_kept": kept, "runs": len(runs)}


def parse_stats(text: Optional[str]) -> Optional[dict]:
    if not text:
        return None
    try:
        stats = json.loads(text)
    except ValueError as error:
        raise SystemExit(f"--stats is not JSON: {error}") from None
    if not isinstance(stats, dict) or not all(isinstance(stats.get(k), int) and stats[k] >= 0
                                              for k in ("agents", "tokens")):
        raise SystemExit('--stats needs whole numbers, e.g. {"agents": 27, "tokens": 2600000}')
    return stats


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--domain")
    parser.add_argument("--sources", type=Path)
    parser.add_argument("--state", type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--fetch", action="store_true", help="git fetch each repository first")
    parser.add_argument("--first-months", type=int, default=FIRST_RUN_MONTHS)
    parser.add_argument("--max-verify", type=int, default=DEFAULT_MAX_VERIFY, help="the workflow's maxVerify, for the estimate")
    parser.add_argument("--kioku", type=Path, help="the kioku binary, for code map purposes")
    parser.add_argument("--env-file", type=Path, help="kioku's env file")
    parser.add_argument("--mark", type=Path, help="write the inventory's next state to this state file")
    parser.add_argument("--inventory", type=Path)
    parser.add_argument("--result", type=Path, help="the workflow's return value, saved as JSON")
    parser.add_argument("--stats", help='the run\'s cost, e.g. {"agents": 27, "tokens": 2600000}')
    args = parser.parse_args(argv)
    if args.mark:
        if not (args.inventory and args.result):
            parser.error("--mark needs --inventory and --result")
        summary = mark(args.mark.expanduser(), args.inventory.expanduser(), args.result.expanduser(),
                       parse_stats(args.stats))
        print(json.dumps(summary))
        return 0
    if not (args.domain and args.sources and args.state and args.out):
        parser.error("--domain, --sources, --state and --out are required")
    sources = read_json(args.sources.expanduser(), {})
    state_path = args.state.expanduser()
    state = read_json(state_path, {}) or {}
    inventory = build(args.domain, sources, state, args.fetch, args.first_months,
                      args.kioku.expanduser() if args.kioku else None,
                      args.env_file.expanduser() if args.env_file else None,
                      args.max_verify, state_path.parent)
    args.out.write_text(json.dumps(inventory, indent=1), encoding="utf-8")
    print(json.dumps({"out": str(args.out), **inventory["counts"], "since": inventory["since"],
                      "first_run": inventory["first_run"], "truncated": inventory["truncated"],
                      "estimate": inventory["estimate"]}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
