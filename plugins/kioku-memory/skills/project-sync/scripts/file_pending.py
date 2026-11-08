#!/usr/bin/env python3
"""File what a project-sync or dream run leaves for the user on Kioku's pending list.

  file_pending.py --vault personal --domain personal/notes --skill project-sync \\
      --result result.json [--before retrieval-before.json --after retrieval-after.json] [--date 2026-11-07]

It files one item per decision the run left the user, through the checkpoint
skill's `kioku_pending.py add`, with the full entry as the item's detail:
- each `needs_user` and `skipped` entry of the workflow's result (each carries
  `pending`: kind, fingerprint id and text);
- each query the retrieval check found worse after the run, from
  `retrieval_check.py compare --format json` of the two reports (kind regression).

Each item's text ends with its fingerprint in brackets: skill, action, domain
and key, such as [project-sync:update:personal/notes/auth-ttl]. An item whose
fingerprint is already in the vault's pending store, open or done (the store
keeps done items for 90 days), is not filed again: a run that reads the same
window twice files it once, and what the user resolved stays resolved. The
store is read directly: ~/.local/state/kioku/pending/<vault>.json, or
$KIOKU_PENDING_DIR/<vault>.json.

Prints a JSON summary. Exits 1 when an item couldn't be filed, after writing
those items to a file the summary names, so none is lost.
Standard library only; runs on Python 3.9.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

SKILLS = Path(__file__).resolve().parent.parent.parent
KINDS = ("retire", "kb", "decision", "unverified", "regression", "other")
TEXT_CHARS = 300
VAULT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
FINGERPRINT = re.compile(r"\[([^\[\]\s]+)\]$")


def pending_cli() -> Path:
    default = SKILLS / "checkpoint" / "scripts" / "kioku_pending.py"
    return Path(os.environ.get("KIOKU_PENDING_CLI") or default).expanduser()


def pending_store(vault: str) -> Path:
    root = os.environ.get("KIOKU_PENDING_DIR") or str(Path.home() / ".local" / "state" / "kioku" / "pending")
    return Path(root).expanduser() / f"{vault}.json"


def retrieval_cli() -> Path:
    default = SKILLS / "reflect" / "scripts" / "retrieval_check.py"
    return Path(os.environ.get("KIOKU_RETRIEVAL_CHECK") or default).expanduser()


def unfiled_dir(skill: str) -> Path:
    return Path.home() / ".local" / "state" / "kioku" / "pending-unfiled" / skill


def one_line(text: str) -> str:
    return " ".join(str(text or "").split())


def load_result(path: Path) -> dict:
    result = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(result, dict) and isinstance(result.get("result"), dict) and "counts" not in result:
        result = result["result"]
    if not isinstance(result, dict):
        raise SystemExit(f"{path} is not a workflow result")
    return result


def run_item(entry: dict, skill: str, domain: str) -> dict:
    """The pending item for a needs_user or skipped entry; older results have no `pending`."""
    pending = entry.get("pending") if isinstance(entry.get("pending"), dict) else {}
    key = entry.get("key") or (entry.get("note") or {}).get("key") or "?"
    action = entry.get("action") or "change"
    kind = pending.get("kind") if pending.get("kind") in KINDS else "other"
    fingerprint = pending.get("id") or f"{skill}:{action}:{entry.get('domain') or domain}/{key}"
    return {"kind": kind, "id": "".join(str(fingerprint).split()),
            "text": one_line(pending.get("text") or f"{action} {key} needs your decision"), "detail": entry}


def regression_items(before: Path, after: Path, skill: str, domain: str, day: str) -> list[dict]:
    """One item per query the retrieval check found worse after the run."""
    proc = subprocess.run([sys.executable, "-I", str(retrieval_cli()), "compare", str(before), str(after),
                           "--format", "json"], capture_output=True, text=True)
    if proc.returncode != 0:
        raise SystemExit(f"retrieval_check.py compare failed: {proc.stderr.strip()[-300:]}")
    comparison = json.loads(proc.stdout)
    items = []
    for row in comparison.get("worse") or []:
        query = one_line(row.get("query") or row.get("id") or "?")
        b, a = row.get("ndcg_before"), row.get("ndcg_after")
        change = f" (nDCG {b:.2f} to {a:.2f})" if isinstance(b, (int, float)) and isinstance(a, (int, float)) else ""
        items.append({"kind": "regression", "id": "".join(f"{skill}:regression:{domain}:{row.get('id')}:{day}".split()),
                      "text": f'Retrieval got worse after {skill} on {day}: "{query}"{change}',
                      "detail": {"comparison": {k: v for k, v in comparison.items() if k not in ("worse", "better")},
                                 "query": row}})
    return items


def known_fingerprints(vault: str, domain: str) -> set:
    """The fingerprints of the domain's items in the vault's store, open or done."""
    data = None
    try:
        data = json.loads(pending_store(vault).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    items = data.get("items") if isinstance(data, dict) else None
    found = set()
    for item in items if isinstance(items, list) else []:
        if isinstance(item, dict) and item.get("domain") == domain:
            match = FINGERPRINT.search(str(item.get("text") or "").strip())
            if match:
                found.add(match.group(1))
    return found


def file_items(items: list[dict], vault: str, domain: str, skill: str, day: str, stamp: str) -> dict:
    known = known_fingerprints(vault, domain)
    filed, kept, failed = [], [], []
    with tempfile.TemporaryDirectory(prefix="kioku-pending-") as scratch:
        for number, item in enumerate(items, 1):
            if item["id"] in known:
                kept.append(item["id"])
                continue
            token = f"[{item['id']}]"
            room = TEXT_CHARS - len(token) - 1
            text = (item["text"] if len(item["text"]) <= room else item["text"][: room - 1] + "…") + " " + token
            # `pending add` copies the detail file into its store.
            detail = Path(scratch) / f"{number}.json"
            detail.write_text(json.dumps(item["detail"], indent=1), encoding="utf-8")
            proc = subprocess.run([sys.executable, "-I", str(pending_cli()), "add", "--vault", vault,
                                   "--domain", domain, "--kind", item["kind"], "--source", f"{skill} {day}",
                                   "--text", text, "--detail-file", str(detail)], capture_output=True, text=True)
            if proc.returncode == 0:
                filed.append({"id": proc.stdout.strip(), "kind": item["kind"], "text": text})
                known.add(item["id"])
            else:
                failed.append({**item, "error": (proc.stderr or proc.stdout).strip()[-300:]})
    summary = {"filed": filed, "already_pending": kept, "failed": len(failed)}
    if failed:
        folder = unfiled_dir(skill)
        folder.mkdir(parents=True, exist_ok=True)
        unfiled = folder / f"{domain.replace('/', '-')}-{stamp}.json"
        unfiled.write_text(json.dumps(failed, indent=1), encoding="utf-8")
        summary["unfiled"] = str(unfiled)
        summary["error"] = failed[0]["error"]
    return summary


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--vault", required=True)
    parser.add_argument("--domain", required=True)
    parser.add_argument("--skill", required=True, choices=["project-sync", "dream"])
    parser.add_argument("--result", type=Path, help="the workflow's return value, saved as JSON")
    parser.add_argument("--before", type=Path, help="retrieval_check.py run report from before the run")
    parser.add_argument("--after", type=Path, help="retrieval_check.py run report from after the apply")
    parser.add_argument("--date", help="the run's day, YYYY-MM-DD (default today, UTC)")
    args = parser.parse_args(argv)
    if not VAULT_NAME.fullmatch(args.vault):
        parser.error(f"not a vault name: {args.vault!r}")
    if bool(args.before) != bool(args.after):
        parser.error("--before and --after go together")
    if not (args.result or args.before):
        parser.error("give --result, --before and --after, or both")
    now = datetime.now(timezone.utc)
    day = args.date or now.strftime("%Y-%m-%d")
    items = []
    if args.result:
        result = load_result(args.result.expanduser())
        for entry in list(result.get("needs_user") or []) + list(result.get("skipped") or []):
            if isinstance(entry, dict):
                items.append(run_item(entry, args.skill, args.domain))
    if args.before:
        items += regression_items(args.before.expanduser(), args.after.expanduser(), args.skill, args.domain, day)
    summary = file_items(items, args.vault, args.domain, args.skill, day, now.strftime("%Y%m%d-%H%M%S"))
    print(json.dumps(summary))
    return 1 if summary["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
