#!/usr/bin/env python3
"""Inventory a Kioku domain's source for the reflect skill: deterministic health
checks, plus the topic clusters that the reflect workflow hands to agents.

Two kinds of source:
  --kb <dir>             a knowledge base inside an OKF bundle (for example
                         acme-kb/payments-kb). Decisions are clustered by their
                         first topic tag; --ignore-tags names tags that are
                         never topics, besides private-source.
  --notes <dir> --domain a folder of Markdown notes, such as the kioku checkout's
                         notes/. The notes of that domain form one cluster.

Deterministic checks (no model involved):
  supersession  a superseded decision whose `superseded_by` chain is missing,
                broken, loops, or ends at a decision not in force; or an active
                decision that another decision says it supersedes
  status-text   an active decision whose Status section says it was replaced
                (narrowing lines don't count: a narrowed decision stays active)
  undecided     decisions marked contested, and proposals older than 14 days
  answered      open questions whose text says they were answered
  past-stale    notes past their `stale_after` date
  orphan        active notes that no other note links to and that link nowhere
  drift         code, PR and branch notes that changed (from kb_drift.py, if given)

Incremental runs: --state <file> keeps a topic only if it was never checked or
its decisions or topic note changed since the bundle commit it was last checked
at; --all keeps every topic anyway. After a run, --mark <file> --kb <dir>
--topics a,b records those topics as checked at the bundle's current commit.

The estimate: with --state, the inventory also estimates the run's cost, its
agents at most (a check agent and up to --max-verify verifications per topic,
and a report agent) times the tokens per agent that past runs recorded with
--mark <file> --stats '{"agents": 9, "tokens": 700000}'. It asks the skill to
confirm first above 1.5M tokens, or when no run is recorded.

Read-only, except for --mark. Writes the inventory as JSON to --out and prints a
short summary.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

LINK = re.compile(r"\]\((/[^)#\s]+\.md)(?:#[^)\s]*)?\)")
REPLACED = re.compile(r"\b(superseded|replaced|reversed|overturned|withdrawn|revoked)\s+(by|on|in)\b", re.IGNORECASE)
ANSWERED = re.compile(r"\banswer(ed)?\s+(by|in|on)\b|\bAnswered\b")
# Tags that mark how a note was sourced, never what it is about.
NOT_TOPICS = frozenset({"private-source"})
DEFAULT_MAX_VERIFY = 4
CONFIRM_TOKENS = 1_500_000
KEPT_RUNS = 20


def frontmatter(text: str) -> tuple[dict, str]:
    """Top-level scalars and simple lists of a note's YAML frontmatter, and its body."""
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---", 4)
    raw, body = text[4:end], text[end + 4:]
    fields: dict = {}
    key = None
    for line in raw.splitlines():
        if m := re.match(r"^([A-Za-z_]+):\s*(.*)$", line):
            key, value = m.group(1), m.group(2).strip()
            if value.startswith("[") and value.endswith("]"):
                fields[key] = [v.strip().strip("'\"") for v in value[1:-1].split(",") if v.strip()]
            elif value:
                fields[key] = value.strip("'\"")
            else:
                fields[key] = []
        elif key and (m := re.match(r"^\s*-\s+(.*)$", line)) and isinstance(fields.get(key), list):
            fields[key].append(m.group(1).strip().strip("'\""))
    return fields, body


def section(body: str, name: str) -> str:
    m = re.search(rf"^#+\s*{name}\s*$(.*?)(?=^#+\s|\Z)", body, re.MULTILINE | re.DOTALL | re.IGNORECASE)
    return m.group(1) if m else ""


def parse_time(text) -> datetime | None:
    if not text or not isinstance(text, str):
        return None
    try:
        when = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def bundle_root(kb: Path) -> Path:
    for folder in [kb, *kb.parents]:
        index = folder / "index.md"
        if index.exists() and "okf_version" in index.read_text(encoding="utf-8", errors="ignore"):
            return folder
    sys.exit(f"{kb} is not inside an OKF bundle")


def changed_since(root: Path, since: str) -> set[str] | None:
    out = subprocess.run(["git", "diff", "--name-only", f"{since}..HEAD"], cwd=root, capture_output=True, text=True)
    return {"/" + line for line in out.stdout.split()} if out.returncode == 0 else None


def inventory_kb(kb: Path, drift: Path | None, since: str | None, runs: dict | None = None,
                 ignore_tags: frozenset = NOT_TOPICS) -> dict:
    root = bundle_root(kb)
    notes: dict[str, dict] = {}
    for path in sorted(kb.rglob("*.md")):
        if path.name in ("index.md", "log.md"):
            continue
        rel = "/" + str(path.relative_to(root))
        fields, body = frontmatter(path.read_text(encoding="utf-8", errors="ignore"))
        notes[rel] = {"fields": fields, "body": body, "links": set(LINK.findall(body))}
    by_id = {n["fields"].get("id"): rel for rel, n in notes.items() if n["fields"].get("id")}
    incoming: dict[str, set[str]] = defaultdict(set)
    for rel, note in notes.items():
        for target in note["links"]:
            incoming[target].add(rel)

    def lifecycle(rel: str) -> str:
        f = notes[rel]["fields"]
        return f.get("decision_status") or f.get("question_status") or f.get("update_status") or f.get("status", "")

    def ident(rel: str) -> str:
        return notes[rel]["fields"].get("id") or rel

    findings: list[dict] = []
    now = datetime.now(timezone.utc)
    decisions = [rel for rel, n in notes.items() if n["fields"].get("type") == "Decision"]
    for rel in decisions:
        f, body, state = notes[rel]["fields"], notes[rel]["body"], lifecycle(rel)
        successors = f.get("superseded_by") or []
        successors = [successors] if isinstance(successors, str) else successors
        if state == "superseded":
            if not successors:
                findings.append({"check": "supersession", "keys": [ident(rel)], "detail": "superseded, but names no successor"})
            for succ in successors:
                # Chains are history; only a chain that breaks, loops or ends
                # at something not in force leaves a reader without the rule.
                seen, current = [ident(rel)], succ
                while True:
                    target = by_id.get(current)
                    if not target:
                        findings.append({"check": "supersession", "keys": seen + [current],
                                         "detail": f"the chain from {ident(rel)} reaches {current}, which is not in the bundle"})
                        break
                    if current in seen:
                        findings.append({"check": "supersession", "keys": seen, "detail": f"the chain from {ident(rel)} loops"})
                        break
                    seen.append(current)
                    nxt = notes[target]["fields"].get("superseded_by") or []
                    nxt = [nxt] if isinstance(nxt, str) else nxt
                    if lifecycle(target) != "superseded":
                        if lifecycle(target) not in ("active", "proposed", "contested"):
                            findings.append({"check": "supersession", "keys": seen,
                                             "detail": f"the chain from {ident(rel)} ends at {current}, which is {lifecycle(target) or 'unknown'}"})
                        break
                    if not nxt:
                        findings.append({"check": "supersession", "keys": seen,
                                         "detail": f"the chain from {ident(rel)} ends at {current}, which is superseded with no successor"})
                        break
                    current = nxt[0]
        if state == "active":
            status_text = "\n".join(line for line in section(body, "Status").splitlines() if "arrowed" not in line)
            if m := REPLACED.search(status_text):
                findings.append({"check": "status-text", "keys": [ident(rel)],
                                 "detail": f"active, but its Status section says \"{m.group(0)}\""})
        for old in (f.get("supersedes") or []) if isinstance(f.get("supersedes"), list) else [f.get("supersedes")]:
            target = by_id.get(old)
            if target and lifecycle(target) == "active" and state in ("active", "superseded"):
                findings.append({"check": "supersession", "keys": [ident(rel), old],
                                 "detail": f"{ident(rel)} supersedes {old}, but {old} is still active"})
        if state == "contested":
            findings.append({"check": "undecided", "keys": [ident(rel)], "detail": "contested: needs a decision"})
        written = parse_time(f.get("created_at")) or parse_time((re.search(r"at:\s*'?([^'\n}]+)", "\n".join(
            f"{k}: {v}" for k, v in f.items() if isinstance(v, str))) or [None, None])[1])
        if state == "proposed" and written and now - written > timedelta(days=14):
            findings.append({"check": "undecided", "keys": [ident(rel)], "detail": f"proposed since {written:%d %b}, not yet decided"})
    for rel, note in notes.items():
        f = note["fields"]
        if f.get("type") == "Question" and lifecycle(rel) == "open" and ANSWERED.search(section(note["body"], "Status") or note["body"][:600]):
            findings.append({"check": "answered", "keys": [ident(rel)], "detail": "open, but its text says it was answered"})
        if (stale := parse_time(f.get("stale_after"))) and stale < now:
            findings.append({"check": "past-stale", "keys": [ident(rel)], "detail": f"past its stale_after date ({stale:%d %b})"})
        if f.get("status") != "deprecated" and not note["links"] and not incoming.get(rel):
            findings.append({"check": "orphan", "keys": [ident(rel)], "detail": "no links in or out"})
    if drift and drift.exists():
        for entry in json.loads(drift.read_text()).get("drifted", []):
            if entry.get("actionable"):
                findings.append({"check": "drift", "keys": ["/" + kb.name + "/" + entry["note"]], "detail": entry["drift"]})

    # Clusters: decisions still in force, by their first topic tag that has a topic note.
    topics = {p.stem for p in kb.glob("*.md") if p.stem not in ("index", "log", "overview", "glossary", "known-issues")}
    clusters: dict[str, list[str]] = defaultdict(list)
    for rel in decisions:
        if lifecycle(rel) not in ("active", "proposed", "contested"):
            continue
        tags = [t for t in notes[rel]["fields"].get("tags", []) if t not in ignore_tags]
        topic = next((t for t in tags if t in topics), "other")
        clusters[topic].append(rel)
    changed = changed_since(root, since) if since else None
    checked = (runs or {}).get("topics", {})
    cluster_list, skipped = [], []
    for topic, rels in sorted(clusters.items(), key=lambda kv: -len(kv[1])):
        topic_note = f"/{kb.name}/{topic}.md" if topic in topics else None
        files = {*rels, topic_note}
        if changed is not None and not (files & changed):
            skipped.append(topic)
            continue
        if topic in checked:
            since_topic = changed_since(root, checked[topic]["commit"])
            if since_topic is not None and not (files & since_topic):
                skipped.append(topic)
                continue
        cluster_list.append({"name": topic, "topic_note": topic_note, "decisions": rels})
    counts = defaultdict(int)
    for finding in findings:
        counts[finding["check"]] += 1
    head = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=root, capture_output=True, text=True).stdout.strip()
    return {"source": "okf", "root": str(root), "kb": str(kb), "commit": head, "notes": len(notes),
            "decisions": {s: sum(1 for r in decisions if lifecycle(r) == s) for s in ("active", "proposed", "contested", "superseded")},
            "clusters": cluster_list, "unchanged_topics": skipped, "findings": findings, "counts": dict(counts)}


def mark(path: Path, root: Path | None, topics: list[str], stats: dict | None = None) -> None:
    """Record topics as checked at the bundle's commit (with a bundle), and a run's cost (with stats)."""
    state = json.loads(path.read_text()) if path.exists() else {}
    if root is not None and topics:
        head = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=root, capture_output=True,
                              text=True).stdout.strip()
        for topic in topics:
            state.setdefault("topics", {})[topic] = {"commit": head, "at": datetime.now().strftime("%Y-%m-%d")}
        print(f"recorded {', '.join(topics)} as checked at {head}")
    if stats:
        runs = [r for r in state.get("runs") or [] if isinstance(r, dict)]
        runs.append({"at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "agents": stats["agents"],
                     "tokens": stats["tokens"], "topics": len(topics)})
        state["runs"] = runs[-KEPT_RUNS:]
        print(f"recorded a run of {stats['agents']} agents and {stats['tokens']} tokens")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=1))


def recorded_runs(state: dict, state_dir: Path | None) -> list[dict]:
    """The runs this domain recorded, else every domain's in the same state folder: what
    an agent costs is the workflow's, more than the domain's."""
    runs = [r for r in state.get("runs") or [] if isinstance(r, dict)]
    if runs or not state_dir or not state_dir.is_dir():
        return runs
    for other in sorted(state_dir.glob("*.json")):
        try:
            data = json.loads(other.read_text())
        except (OSError, ValueError):
            continue
        runs += [r for r in (data.get("runs") or [] if isinstance(data, dict) else []) if isinstance(r, dict)]
    return runs


def estimate(runs: list[dict], topics: int, max_verify: int, confirm_above: int = CONFIRM_TOKENS) -> dict:
    """The run's planned agents times the recorded tokens per agent. Planned agents scale with
    the topics by the recorded agents per topic, within the workflow's maximum. Without a
    recorded run there is no estimate, and the skill asks first."""
    if not topics:
        return {"agents": 0, "tokens": 0, "tokens_per_agent": None, "from_runs": len(runs), "ask_first": False}
    most = topics * (1 + max_verify) + 1
    agents = sum(max(0, int(r.get("agents") or 0)) for r in runs)
    tokens = sum(max(0, int(r.get("tokens") or 0)) for r in runs)
    if agents <= 0 or tokens <= 0:
        return {"agents": most, "tokens": None, "tokens_per_agent": None, "from_runs": 0, "ask_first": True}
    per_agent = tokens / agents
    with_topics = [r for r in runs if int(r.get("topics") or 0) > 0 and int(r.get("agents") or 0) > 0]
    if with_topics:
        per_topic = sum(int(r["agents"]) for r in with_topics) / sum(int(r["topics"]) for r in with_topics)
        planned = min(most, math.ceil(per_topic * topics))
    else:
        planned = most
    total = round(planned * per_agent)
    return {"agents": planned, "tokens": total, "tokens_per_agent": round(per_agent), "from_runs": len(runs),
            "ask_first": total > confirm_above}


def parse_stats(text: str | None) -> dict | None:
    if not text:
        return None
    try:
        stats = json.loads(text)
    except ValueError as error:
        raise SystemExit(f"--stats is not JSON: {error}") from None
    if not isinstance(stats, dict) or not all(isinstance(stats.get(k), int) and not isinstance(stats.get(k), bool)
                                              and stats[k] >= 0 for k in ("agents", "tokens")):
        raise SystemExit('--stats needs whole numbers, e.g. {"agents": 9, "tokens": 700000}')
    return stats


def inventory_notes(folder: Path, domain: str) -> dict:
    rels = []
    for path in sorted(folder.rglob("*.md")):
        fields, _ = frontmatter(path.read_text(encoding="utf-8", errors="ignore"))
        if fields.get("domain") == domain:
            rels.append(str(path))
    return {"source": "markdown", "root": str(folder), "notes": len(rels),
            "clusters": [{"name": domain, "topic_note": None, "decisions": rels}], "findings": [], "counts": {}}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--kb", type=Path)
    source.add_argument("--notes", type=Path)
    parser.add_argument("--domain", help="with --notes: the Kioku domain to read")
    parser.add_argument("--drift", type=Path, help="kb_drift.py output to fold in")
    parser.add_argument("--since", help="only clusters changed since this git revision of the bundle")
    parser.add_argument("--state", type=Path, help="earlier runs: checked topics (with --kb) and their cost, for the estimate")
    parser.add_argument("--all", action="store_true", help="keep every topic, also those --state says are unchanged")
    parser.add_argument("--ignore-tags", default="", help="comma-separated tags that are never topics")
    parser.add_argument("--max-verify", type=int, default=DEFAULT_MAX_VERIFY, help="the workflow's maxVerify, for the estimate")
    parser.add_argument("--mark", type=Path, help="record --topics as checked (with --kb) and --stats, then exit")
    parser.add_argument("--topics", help="with --mark: comma-separated topic names")
    parser.add_argument("--stats", help='with --mark: the run\'s cost, e.g. {"agents": 9, "tokens": 700000}')
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    topics = [t.strip() for t in (args.topics or "").split(",") if t.strip()]
    if args.mark:
        stats = parse_stats(args.stats)
        if not (stats or (args.kb and topics)):
            parser.error("--mark needs --kb and --topics, or --stats")
        mark(args.mark.expanduser(), bundle_root(args.kb.expanduser()) if args.kb else None, topics, stats)
        return 0
    if not (args.kb or args.notes):
        parser.error("give --kb or --notes")
    if not args.out:
        parser.error("--out is required")
    state_path = args.state.expanduser() if args.state else None
    state = json.loads(state_path.read_text()) if state_path and state_path.exists() else None
    if args.kb:
        ignore = NOT_TOPICS | {t.strip() for t in args.ignore_tags.split(",") if t.strip()}
        result = inventory_kb(args.kb.expanduser(), args.drift, args.since, None if args.all else state,
                              frozenset(ignore))
    else:
        if not args.domain:
            parser.error("--notes needs --domain")
        result = inventory_notes(args.notes.expanduser(), args.domain)
    if state_path:
        runs = recorded_runs(state or {}, state_path.parent)
        result["estimate"] = estimate(runs, len(result["clusters"]), args.max_verify)
    args.out.write_text(json.dumps(result, indent=1, default=sorted))
    sizes = ", ".join(f"{c['name']} {len(c['decisions'])}" for c in result["clusters"])
    print(f"{result['notes']} notes; clusters: {sizes or 'none'}; findings: {result['counts'] or 'none'}")
    if "estimate" in result:
        print(f"estimate: {json.dumps(result['estimate'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
