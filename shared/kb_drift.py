#!/usr/bin/env python3
"""Read-only drift scan of an OKF knowledge base against the code it describes.

For each item note that points at code, compare what the note recorded with
what the source system says now:

- Code File notes (id `repo:<repo>@<ref>:<path>`): the commit the note was read
  at (a SHA in its frontmatter, else the last commit before its `updated_at`),
  then whether the file still exists on the ref and how many commits touched it
  since.
- Pull Request notes (id `pr:<repo>#<n>`): the recorded `state` against GitHub's.
- Git Branch notes (id `branch:<repo>/<name>`): whether the branch still exists
  on the remote, whether it is merged into the default branch, and whether its
  head moved past every commit the note names.

A drifted note is `actionable` unless it already says it isn't current: a
`historical`, `superseded` or `obsolete` note whose file is gone is consistent.

Nothing is written. Prints JSON: a summary and one entry per drifted note.

Usage: kb_drift.py --kb <bundle> --repos <dir with clones> [--org <github owner>] [--fetch]

Each clone's GitHub owner comes from its `origin` remote; --org overrides it.
Vendored into the kioku-memory and kioku-hooks plugins from the marketplace's
shared/ folder.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

SHA = re.compile(r"(?<![0-9a-f])[0-9a-f]{7,40}(?![0-9a-f])")
GITHUB_REMOTE = re.compile(r"github\.com[:/]+([^/\s]+)/[^/\s]+?(?:\.git)?/?$")


def run(cmd: list[str], cwd: Path | None = None) -> tuple[int, str]:
    out = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    return out.returncode, out.stdout.strip()


def frontmatter(path: Path) -> tuple[dict[str, str], str]:
    """Top-level scalar keys of the YAML frontmatter, and its raw text.
    Plain parsing keeps the scan dependency-free; nested keys stay in the text."""
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        return {}, ""
    end = text.find("\n---", 4)
    raw = text[4:end]
    fields: dict[str, str] = {}
    for line in raw.splitlines():
        m = re.match(r"^([a-z_]+):\s*(.*)$", line)
        if m and m.group(2) and not m.group(2).startswith(("-", "|", ">")):
            fields[m.group(1)] = m.group(2).strip().strip("'\"")
    return fields, raw


def parse_time(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def github_owner(remote: str) -> str | None:
    """The owner in a GitHub remote URL (https or ssh), or None for any other remote."""
    m = GITHUB_REMOTE.search(remote.strip())
    return m.group(1) if m else None


class Repo:
    def __init__(self, path: Path, name: str, org: str | None, fetch: bool):
        if not org:
            _, remote = run(["git", "remote", "get-url", "origin"], path)
            org = github_owner(remote) or ""
        self.path, self.name, self.org = path, name, org
        if fetch:
            run(["git", "-c", "credential.helper=!gh auth git-credential", "fetch", "-q", "origin"], path)
        _, head = run(["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"], path)
        self.main = head or "origin/main"
        _, heads = run(["git", "-c", "credential.helper=!gh auth git-credential", "ls-remote", "--heads", "origin"], path)
        self.heads = {line.split("\trefs/heads/", 1)[1]: line.split("\t")[0] for line in heads.splitlines() if "\trefs/heads/" in line}
        self._prs: dict[int, dict] | None = None

    def commit_exists(self, sha: str) -> str | None:
        code, full = run(["git", "rev-parse", "--verify", "--quiet", f"{sha}^{{commit}}"], self.path)
        return full if code == 0 else None

    def newest(self, shas: list[str]) -> str | None:
        found = [full for sha in shas if (full := self.commit_exists(sha))]
        if not found:
            return None
        dated = []
        for full in set(found):
            _, when = run(["git", "show", "-s", "--format=%ct", full], self.path)
            dated.append((int(when or 0), full))
        return max(dated)[1]

    def before(self, when: datetime, ref: str) -> str | None:
        _, sha = run(["git", "rev-list", "-1", f"--before={when.isoformat()}", ref], self.path)
        return sha or None

    def prs(self) -> dict[int, dict]:
        if self._prs is None:
            _, out = run(["gh", "pr", "list", "--repo", f"{self.org}/{self.name}", "--state", "all", "--limit", "1000",
                          "--json", "number,state,isDraft,mergedAt,closedAt,updatedAt,headRefName"])
            self._prs = {pr["number"]: pr for pr in json.loads(out or "[]")}
        return self._prs


def scan_code(note: Path, fields: dict, raw: str, repo: Repo, ref: str, path: str) -> dict:
    branch = ref.removeprefix("origin/")
    target = f"origin/{branch}"
    entry = {"note": str(note), "kind": "code", "path": f"{repo.name}@{ref}:{path}",
             "recorded_state": fields.get("state"), "recorded_freshness": fields.get("freshness")}
    if branch not in repo.heads:
        code, _ = run(["git", "cat-file", "-e", f"{repo.main}:{path}"], repo.path)
        entry["drift"] = f"its branch {branch} is gone from the remote; the path is {'on' if code == 0 else 'not on'} {repo.main}"
        return entry
    pinned = repo.newest(SHA.findall(raw))
    how = "sha"
    if not pinned:
        how = "date"
        when = parse_time(fields.get("updated_at")) or parse_time(re.search(r"at: '?([^'\n]+)", raw).group(1) if "at:" in raw else None)
        pinned = repo.before(when, target) if when else None
    code, _ = run(["git", "cat-file", "-e", f"{target}:{path}"], repo.path)
    exists = code == 0
    entry.update({"pinned": (pinned or "")[:10], "pinned_by": how})
    if not pinned:
        entry["drift"] = "no commit to compare with"
        return entry
    if not exists:
        _, moves = run(["git", "log", "-M", "--diff-filter=DR", "--name-status", "--format=", f"{pinned}..{target}", "--", path], repo.path)
        moved = [line.split("\t")[-1] for line in moves.splitlines() if line.startswith("R")]
        entry["drift"] = f"moved to {moved[-1]}" if moved else f"deleted from {branch}"
        return entry
    _, count = run(["git", "rev-list", "--count", f"{pinned}..{target}", "--", path], repo.path)
    if int(count or 0):
        _, stat = run(["git", "diff", "--shortstat", pinned, target, "--", path], repo.path)
        entry["drift"] = f"{count} commits since, {stat.strip()}"
    return entry


def scan_pr(note: Path, fields: dict, repo: Repo, number: int) -> dict:
    entry = {"note": str(note), "kind": "pr", "pr": f"{repo.name}#{number}", "recorded_state": fields.get("state"),
             "recorded_freshness": fields.get("freshness")}
    pr = repo.prs().get(number)
    if not pr:
        code, out = run(["gh", "pr", "view", str(number), "--repo", f"{repo.org}/{repo.name}",
                         "--json", "number,state,isDraft,mergedAt,closedAt,updatedAt,headRefName"])
        pr = json.loads(out) if code == 0 and out else None
    if not pr:
        entry["drift"] = "not found on GitHub"
        return entry
    state = "DRAFT" if pr["isDraft"] and pr["state"] == "OPEN" else pr["state"]
    recorded = (fields.get("state") or "").upper()
    entry["github_state"] = state
    if state not in recorded:
        entry["drift"] = f"recorded {fields.get('state')!r}, GitHub says {state}"
    elif (updated := parse_time(pr["updatedAt"])) and (noted := parse_time(fields.get("updated_at"))) and updated > noted and state == "OPEN":
        entry["drift"] = f"open PR active since the note ({pr['updatedAt'][:16]})"
    return entry


def scan_branch(note: Path, fields: dict, raw: str, repo: Repo, name: str) -> dict:
    recorded = fields.get("state") or ""
    entry = {"note": str(note), "kind": "branch", "branch": f"{repo.name}/{name}", "recorded_state": recorded[:120],
             "recorded_freshness": fields.get("freshness")}
    head = repo.heads.get(name)
    says_local = "local" in recorded.lower() and "unpushed" in recorded.lower()
    if not head:
        if not says_local and "deleted" not in recorded.lower():
            entry["drift"] = "branch no longer on the remote"
        return entry
    code, _ = run(["git", "merge-base", "--is-ancestor", head, repo.main], repo.path)
    merged = code == 0
    if merged and "merged" not in recorded.lower().replace("unmerged", ""):
        entry["drift"] = "branch is now merged into the default branch"
        return entry
    named = [sha for sha in SHA.findall(raw) if repo.commit_exists(sha)]
    if named and not any(head.startswith(sha) or sha.startswith(head[: len(sha)]) for sha in named):
        newest = repo.newest(named)
        if newest:
            _, ahead = run(["git", "rev-list", "--count", f"{newest}..{head}"], repo.path)
            if int(ahead or 0):
                entry["drift"] = f"head moved {ahead} commits past the newest commit the note names"
    return entry


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--kb", type=Path, required=True)
    parser.add_argument("--repos", type=Path, required=True, help="folder holding local clones named like the repos")
    parser.add_argument("--org", help="GitHub owner of the repos (default: each clone's origin remote)")
    parser.add_argument("--fetch", action="store_true", help="git fetch each clone first (read-only for the working tree)")
    args = parser.parse_args()
    repos: dict[str, Repo] = {}

    def repo(name: str) -> Repo | None:
        if name not in repos:
            path = args.repos / name
            if not (path / ".git").exists():
                return None
            repos[name] = Repo(path, name, args.org, args.fetch)
        return repos[name]

    results, skipped = [], []
    for note in sorted(args.kb.rglob("*.md")):
        if note.name in ("index.md", "log.md"):
            continue
        fields, raw = frontmatter(note)
        ident = fields.get("id", "")
        rel = note.relative_to(args.kb)
        if m := re.match(r"^repo:([\w.-]+)@([^:]+):(.+)$", ident):
            r = repo(m.group(1))
            results.append(scan_code(rel, fields, raw, r, m.group(2), m.group(3))) if r else skipped.append((str(rel), f"no clone of {m.group(1)}"))
        elif m := re.match(r"^pr:([\w.-]+)#(\d+)$", ident):
            r = repo(m.group(1))
            results.append(scan_pr(rel, fields, r, int(m.group(2)))) if r else skipped.append((str(rel), f"no clone of {m.group(1)}"))
        elif m := re.match(r"^branch:([\w.-]+)/(.+)$", ident):
            r = repo(m.group(1))
            results.append(scan_branch(rel, fields, raw, r, m.group(2))) if r else skipped.append((str(rel), f"no clone of {m.group(1)}"))
        elif fields.get("type") in ("Linear Issue", "Linear Project"):
            skipped.append((str(rel), "Linear: not checked by this script"))
    drifted = [r for r in results if r.get("drift")]
    for r in drifted:
        r["actionable"] = (r.get("recorded_freshness") or "current") not in ("historical", "superseded", "obsolete")
    drifted.sort(key=lambda r: (not r["actionable"], r["kind"], r["note"]))
    summary: dict[str, dict[str, int]] = {}
    for r in results:
        s = summary.setdefault(r["kind"], {"checked": 0, "drifted": 0, "actionable": 0})
        s["checked"] += 1
        s["drifted"] += bool(r.get("drift"))
        s["actionable"] += bool(r.get("drift") and r.get("actionable"))
    reasons: dict[str, int] = {}
    for _, why in skipped:
        reasons[why] = reasons.get(why, 0) + 1
    print(json.dumps({"summary": summary, "skipped": reasons,
                      "default_branches": {name: r.main for name, r in repos.items()},
                      "drifted": drifted}, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
