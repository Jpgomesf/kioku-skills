#!/usr/bin/env python3
"""Before-and-after retrieval check for Kioku.

  retrieval_check.py run --dataset <dataset.json> --out <report.json>
  retrieval_check.py compare <before.json> <after.json> [--format markdown|json] [--fail-on-regression]
  retrieval_check.py mine --domain <domain> --out <candidates.jsonl>

Standard library only, Python 3.9 compatible. It never writes to the database:
`run` calls `kioku eval` on a dataset without a corpus (read-only) and
`kioku search` to see whether each expected key still exists.

It needs the Kioku checkout: $KIOKU_HOME, or $KIOKU_REPO.

Environment overrides (used by the tests):
  KIOKU_REPO          Kioku checkout (default $KIOKU_HOME)
  KIOKU_BIN           kioku binary (default $KIOKU_REPO/bin/kioku)
  KIOKU_ENV_FILE_PATH dotenv file (default $KIOKU_REPO/.env); its contents are never printed
  KIOKU_VAULTS_PY     vaults.py (default $KIOKU_REPO/scripts/vaults.py)
  KIOKU_VAULTS_TOML   vaults.toml (default ~/.config/kioku/vaults.toml)
  KIOKU_PROJECTS_DIR  Claude Code transcripts (default ~/.claude/projects)
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

EPS = 1e-9
DEFAULT_VARIANT = "hybrid+graph"


class CheckError(Exception):
    """A user-facing failure: printed without a traceback, exit code 2."""


# --------------------------------------------------------------------------
# Configuration and vaults
# --------------------------------------------------------------------------

def repo() -> Path:
    home = os.environ.get("KIOKU_REPO") or os.environ.get("KIOKU_HOME") or ""
    if not home.strip():
        raise CheckError("KIOKU_HOME is not set: set it to the folder of your Kioku checkout")
    return Path(home).expanduser()


def in_repo(variable: str, *parts: str) -> Path:
    """A path the environment names, else one inside the Kioku checkout."""
    given = os.environ.get(variable)
    return Path(given).expanduser() if given else repo().joinpath(*parts)


def kioku_bin() -> Path:
    return in_repo("KIOKU_BIN", "bin", "kioku")


def env_file() -> Path:
    return in_repo("KIOKU_ENV_FILE_PATH", ".env")


def vaults_py() -> Path:
    return in_repo("KIOKU_VAULTS_PY", "scripts", "vaults.py")


def vaults_toml() -> Path:
    return Path(os.environ.get("KIOKU_VAULTS_TOML", "~/.config/kioku/vaults.toml")).expanduser()


def projects_dir() -> Path:
    return Path(os.environ.get("KIOKU_PROJECTS_DIR", "~/.claude/projects")).expanduser()


def parse_vaults_toml(text: str) -> Dict[str, List[Dict[str, List[str]]]]:
    """Minimal parser for the [[vault]]/[[area]]/[[lock]] tables: string and string-array values."""
    tables: Dict[str, List[Dict[str, List[str]]]] = {}
    current: Optional[Dict[str, List[str]]] = None
    pending_key: Optional[str] = None
    buffer = ""
    for raw in text.splitlines():
        line = raw.strip()
        if pending_key is None:
            if not line or line.startswith("#"):
                continue
            header = re.match(r"^\[\[(\w+)\]\]", line)
            if header:
                current = {}
                tables.setdefault(header.group(1), []).append(current)
                continue
            pair = re.match(r"^(\w+)\s*=\s*(.*)$", line)
            if not pair or current is None:
                continue
            key, value = pair.group(1), pair.group(2)
        else:
            key, value = pending_key, buffer + " " + line
        if value.startswith("[") and "]" not in _strip_strings(value):
            pending_key, buffer = key, value
            continue
        pending_key, buffer = None, ""
        if current is not None:
            current[key] = re.findall(r'"((?:[^"\\]|\\.)*)"', value)
    return tables


def _strip_strings(value: str) -> str:
    return re.sub(r'"(?:[^"\\]|\\.)*"', '""', value)


def vault_of_folder(cwd: Path) -> str:
    """Name of the vault the current folder belongs to, per `vaults.py where`."""
    script = vaults_py()
    if not script.exists():
        raise CheckError("vaults.py not found at %s" % script)
    # vaults.py needs Python 3.11+ (tomllib); this script may run on an older one.
    candidates = [shutil.which("python3"), shutil.which("python3.13"), shutil.which("python3.12"),
                  shutil.which("python3.11"), sys.executable]
    proc = None
    for python in dict.fromkeys(c for c in candidates if c):
        proc = subprocess.run([python, str(script), "where", str(cwd)], capture_output=True, text=True, timeout=60)
        if re.search(r'vault "([^"]+)"', proc.stdout):
            break
    match = re.search(r'vault "([^"]+)"', proc.stdout)
    if not match:
        raise CheckError(
            "this folder is in no vault, so there is nothing to check: %s" % (proc.stdout.strip() or proc.stderr.strip())[-300:]
        )
    return match.group(1)


def domain_in_vault(domain: str, vault: str, tables: Dict[str, List[Dict[str, List[str]]]]) -> bool:
    for entry in tables.get("vault", []):
        if entry.get("name", [""])[0] == vault:
            return any(fnmatch.fnmatchcase(domain, pat) for pat in entry.get("domains", []))
    return False


def expand(path: str) -> Path:
    return Path(os.path.expanduser(path))


def vault_for_path(folder: Path, tables: Dict[str, List[Dict[str, List[str]]]]) -> Optional[str]:
    """Longest matching vault path wins. Git remotes are not consulted: a clone is not a configured path."""
    best: Tuple[int, Optional[str]] = (-1, None)
    for entry in tables.get("vault", []):
        for raw in entry.get("paths", []):
            root = expand(raw)
            if folder == root or root in folder.parents:
                if len(root.parts) > best[0]:
                    best = (len(root.parts), entry.get("name", [""])[0])
    return best[1]


# --------------------------------------------------------------------------
# Datasets and kioku calls
# --------------------------------------------------------------------------

def load_dataset(path: Path) -> Dict[str, Any]:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise CheckError("cannot read dataset %s: %s" % (path, exc))
    if not isinstance(data, dict) or not data.get("domain"):
        raise CheckError("dataset %s has no domain" % path)
    return data


def load_queries(dataset_path: Path, dataset: Dict[str, Any]) -> List[Dict[str, Any]]:
    queries_path = dataset_path.parent / dataset.get("queries", "queries.jsonl")
    queries: List[Dict[str, Any]] = []
    for line in queries_path.read_text().splitlines():
        if line.strip():
            queries.append(json.loads(line))
    return queries


def kioku_cmd(*args: str) -> List[str]:
    return [str(kioku_bin()), "--env-file", str(env_file())] + list(args)


def key_exists(key: str, domain: str) -> bool:
    proc = subprocess.run(
        kioku_cmd("search", key, "--domain", domain, "--limit", "3"),
        capture_output=True, text=True, timeout=120,
    )
    if proc.returncode != 0:
        raise CheckError("kioku search failed (exit %d)" % proc.returncode)
    try:
        results = json.loads(proc.stdout).get("results", [])
    except ValueError:
        raise CheckError("kioku search printed no JSON")
    return any(r.get("key") == key and r.get("match") == "exact" for r in results)


def cmd_run(args: argparse.Namespace) -> int:
    dataset_path = Path(args.dataset).expanduser().resolve()
    dataset = load_dataset(dataset_path)
    if dataset.get("corpus"):
        raise CheckError(
            "dataset has a corpus; this check only searches the live domain read-only. Remove `corpus` from %s" % dataset_path
        )
    tables = parse_vaults_toml(vaults_toml().read_text()) if vaults_toml().exists() else {}
    vault = vault_of_folder(Path.cwd())
    if not domain_in_vault(dataset["domain"], vault, tables):
        raise CheckError(
            'dataset domain "%s" is not in vault "%s" of the current folder; run from a folder of its own vault'
            % (dataset["domain"], vault)
        )
    queries = load_queries(dataset_path, dataset)

    out = Path(args.out).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        kioku_cmd("eval", "--dataset", str(dataset_path), "--format", "json", "--output", str(out), "--k", str(args.k)),
        capture_output=True, text=True, timeout=1800,
    )
    if proc.returncode != 0 or not out.exists():
        raise CheckError("kioku eval failed (exit %d): %s" % (proc.returncode, proc.stderr.strip()[-500:]))
    report = json.loads(out.read_text())

    exists: Dict[str, bool] = {}
    check_queries: Dict[str, Any] = {}
    for query in queries:
        relevant = query.get("relevant", {})
        for key in relevant:
            if key not in exists:
                exists[key] = key_exists(key, dataset["domain"])
        check_queries[query["id"]] = {
            "query": query["query"],
            "relevant": relevant,
            "missing": [k for k in relevant if not exists[k]],
        }
    report["retrieval_check"] = {"vault": vault, "domain": dataset["domain"], "queries": check_queries}
    out.write_text(json.dumps(report, indent=2) + "\n")

    default = default_variant(report)
    overall = default["overall"]
    stale = sum(1 for q in check_queries.values() if q["missing"])
    print("%s: %d queries, k=%s, variant %s" % (dataset["domain"], overall.get("queries", 0), report.get("k"), default["name"]))
    print("recall@k %.3f  nDCG@k %.3f  MRR %.3f  hit@1 %.3f  abstained %s/%s"
          % (overall.get("recall", 0), overall.get("ndcg", 0), overall.get("mrr", 0), overall.get("hit_at_1", 0),
             overall.get("abstained", 0), overall.get("abstention_queries", 0)))
    if stale:
        print("%d queries expect keys that no longer exist" % stale)
    print("report: %s" % out)
    return 0


# --------------------------------------------------------------------------
# compare
# --------------------------------------------------------------------------

def default_variant(report: Dict[str, Any]) -> Dict[str, Any]:
    variants = report.get("variants") or []
    if not variants:
        raise CheckError("report has no variants")
    for variant in variants:
        if variant.get("name") == DEFAULT_VARIANT:
            return variant
    return variants[-1]


def load_report(path: str) -> Dict[str, Any]:
    try:
        return json.loads(Path(path).expanduser().read_text())
    except (OSError, ValueError) as exc:
        raise CheckError("cannot read report %s: %s" % (path, exc))


HEADLINES = [("recall", "recall@k"), ("ndcg", "nDCG@k"), ("mrr", "MRR"), ("hit_at_1", "hit@1")]


def compare_reports(before: Dict[str, Any], after: Dict[str, Any]) -> Dict[str, Any]:
    b_var, a_var = default_variant(before), default_variant(after)
    headline: Dict[str, Any] = {}
    for key, label in HEADLINES:
        b, a = b_var["overall"].get(key), a_var["overall"].get(key)
        if b is not None and a is not None:
            headline[key] = {"label": label, "before": b, "after": a, "delta": a - b}

    b_queries = {q["id"]: q for q in b_var.get("queries", [])}
    a_queries = {q["id"]: q for q in a_var.get("queries", [])}
    a_check = (after.get("retrieval_check") or {}).get("queries", {})
    b_check = (before.get("retrieval_check") or {}).get("queries", {})

    worse: List[Dict[str, Any]] = []
    better: List[Dict[str, Any]] = []
    stale: List[Dict[str, Any]] = []
    for qid, a in a_queries.items():
        info = a_check.get(qid) or b_check.get(qid) or {}
        missing = (a_check.get(qid) or {}).get("missing", [])
        if missing:
            stale.append({"id": qid, "query": info.get("query"), "missing": missing})
            continue
        b = b_queries.get(qid)
        if b is None or "ndcg" not in a or "ndcg" not in b:
            continue  # new query, or an abstention query (no relevance score)
        delta = a["ndcg"] - b["ndcg"]
        if abs(delta) < EPS and abs(a.get("recall", 0) - b.get("recall", 0)) < EPS:
            continue
        row = {
            "id": qid, "query": info.get("query"),
            "ndcg_before": b["ndcg"], "ndcg_after": a["ndcg"], "delta": delta,
            "recall_before": b.get("recall"), "recall_after": a.get("recall"),
            "top_before": b.get("ranked", []), "top_after": a.get("ranked", []),
            "relevant": info.get("relevant"),
        }
        (better if delta > EPS or (abs(delta) < EPS and a.get("recall", 0) > b.get("recall", 0)) else worse).append(row)
    worse.sort(key=lambda r: r["delta"])
    better.sort(key=lambda r: -r["delta"])

    abstain = {}
    for q in a_queries:
        pass
    abstain = {
        "before": "%s/%s" % (b_var["overall"].get("abstained", 0), b_var["overall"].get("abstention_queries", 0)),
        "after": "%s/%s" % (a_var["overall"].get("abstained", 0), a_var["overall"].get("abstention_queries", 0)),
    }
    return {
        "variant": a_var.get("name"),
        "queries": {"before": len(b_queries), "after": len(a_queries)},
        "headline": headline,
        "abstained": abstain,
        "worse": worse,
        "better": better,
        "stale": stale,
        "regressed": bool(worse) or any(h["delta"] < -EPS for k, h in headline.items() if k in ("recall", "ndcg")),
    }


def render_markdown(result: Dict[str, Any]) -> str:
    lines = ["# Retrieval check (%s)" % result["variant"], ""]
    lines.append("Queries: %d before, %d after." % (result["queries"]["before"], result["queries"]["after"]))
    lines += ["", "| Metric | Before | After | Change |", "|---|---|---|---|"]
    for h in result["headline"].values():
        lines.append("| %s | %.3f | %.3f | %+.3f |" % (h["label"], h["before"], h["after"], h["delta"]))
    lines.append("| abstained | %s | %s | |" % (result["abstained"]["before"], result["abstained"]["after"]))

    def section(title: str, rows: List[Dict[str, Any]]) -> None:
        lines.extend(["", "## %s (%d)" % (title, len(rows))])
        if not rows:
            lines.append("None.")
        for r in rows:
            lines.append("- `%s` %s: nDCG %.3f -> %.3f (recall %s -> %s)" % (
                r["id"], json.dumps(r["query"]), r["ndcg_before"], r["ndcg_after"], r["recall_before"], r["recall_after"]))
            lines.append("  - top before: %s" % ", ".join(r["top_before"]))
            lines.append("  - top after: %s" % ", ".join(r["top_after"]))
            if r.get("relevant"):
                lines.append("  - expected: %s" % ", ".join("%s (%s)" % kv for kv in r["relevant"].items()))

    section("Worse", result["worse"])
    section("Better", result["better"])
    lines.extend(["", "## Stale (%d)" % len(result["stale"])])
    if not result["stale"]:
        lines.append("None.")
    for s in result["stale"]:
        lines.append("- `%s` %s: expected keys gone: %s" % (s["id"], json.dumps(s["query"]), ", ".join(s["missing"])))
    return "\n".join(lines) + "\n"


def cmd_compare(args: argparse.Namespace) -> int:
    result = compare_reports(load_report(args.before), load_report(args.after))
    if args.format == "json":
        print(json.dumps(result, indent=2))
    else:
        sys.stdout.write(render_markdown(result))
    if args.fail_on_regression and result["regressed"]:
        return 1
    return 0


# --------------------------------------------------------------------------
# mine
# --------------------------------------------------------------------------

SEARCH_TOOLS = ("memory_search",)
GET_TOOLS = ("memory_get",)


def encode_folder(path: Path) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", str(path))


def tool_base(name: str) -> str:
    return name.rsplit("__", 1)[-1]


def is_real_user_message(entry: Dict[str, Any]) -> bool:
    if entry.get("type") != "user" or entry.get("isMeta"):
        return False
    content = entry.get("message", {}).get("content")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        if any(b.get("type") == "tool_result" for b in content if isinstance(b, dict)):
            return False
        text = " ".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    else:
        return False
    text = text.strip()
    return bool(text) and not text.startswith("<local-command") and not text.startswith("<command-name>")


def result_keys(content: Any) -> List[str]:
    if isinstance(content, list):
        content = " ".join(b.get("text", "") for b in content if isinstance(b, dict))
    if not isinstance(content, str):
        return []
    return re.findall(r'"key"\s*:\s*"([^"]+)"', content)


def mine_file(path: Path, domain: str, folders: List[Path]) -> List[Dict[str, Any]]:
    """Searches in `path` with the keys the model read afterwards, graded 2 for the first read and 1 for others."""
    searches: List[Dict[str, Any]] = []
    window: List[Dict[str, Any]] = []  # searches since the last user message
    by_tool_id: Dict[str, Dict[str, Any]] = {}
    ignored_ids: Dict[str, bool] = {}  # searches in other domains or folders
    foreign: set = set()  # keys those searches returned

    def in_area(entry: Dict[str, Any]) -> bool:
        cwd = entry.get("cwd")
        if not cwd:
            return True
        cwd_path = Path(cwd)
        return any(cwd_path == f or f in cwd_path.parents for f in folders)

    with path.open(errors="replace") as handle:
        for raw in handle:
            try:
                entry = json.loads(raw)
            except ValueError:
                continue
            if is_real_user_message(entry):
                window = []
                continue
            content = entry.get("message", {}).get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict):
                    continue
                kind = block.get("type")
                if kind == "tool_use":
                    base = tool_base(block.get("name", ""))
                    inp = block.get("input") or {}
                    if base in SEARCH_TOOLS:
                        given = inp.get("domain")
                        query = inp.get("query")
                        if not query:
                            continue
                        if (given and given != domain) or (not given and not in_area(entry)):
                            ignored_ids[block.get("id", "")] = True
                            continue
                        search = {"query": query, "returned": [], "reads": [], "errored": False}
                        searches.append(search)
                        window.append(search)
                        by_tool_id[block.get("id", "")] = search
                    elif base in GET_TOOLS and window:
                        given = inp.get("domain")
                        if given and given != domain:
                            continue
                        keys = inp.get("keys") or []
                        target = None
                        for key in keys:
                            if key in foreign and not any(key in s["returned"] for s in window):
                                continue
                            target = next((s for s in reversed(window) if key in s["returned"]), window[-1])
                            if key not in target["reads"]:
                                target["reads"].append(key)
                elif kind == "tool_result":
                    if block.get("tool_use_id", "") in ignored_ids:
                        foreign.update(result_keys(block.get("content")))
                    search = by_tool_id.get(block.get("tool_use_id", ""))
                    if search is not None:
                        search["errored"] = bool(block.get("is_error"))
                        search["returned"] = result_keys(block.get("content"))
    return [s for s in searches if not s["errored"]]


def tokens(text: str) -> frozenset:
    return frozenset(re.findall(r"[a-z0-9]+", text.lower()))


def near_duplicate(a: frozenset, b: frozenset, threshold: float = 0.8) -> bool:
    if not a or not b:
        return a == b
    return len(a & b) / float(len(a | b)) >= threshold


def build_candidates(searches: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    candidates: List[Dict[str, Any]] = []
    seen: List[frozenset] = []
    for search in searches:
        if not search["reads"]:
            continue
        grades = {key: (2 if i == 0 else 1) for i, key in enumerate(search["reads"])}
        toks = tokens(search["query"])
        for i, other in enumerate(seen):
            if near_duplicate(toks, other):
                merged = candidates[i]["relevant"]
                for key, grade in grades.items():
                    merged[key] = max(merged.get(key, 0), grade)
                break
        else:
            seen.append(toks)
            candidates.append({"id": "mined-%03d" % (len(candidates) + 1), "category": "mined",
                               "query": search["query"], "relevant": grades})
    return candidates


def transcript_files(folder: Path) -> Iterator[Path]:
    root = projects_dir() / encode_folder(folder)
    if not root.is_dir():
        return
    for path in sorted(root.rglob("*.jsonl")):
        if path.parent == root or "subagents" in path.relative_to(root).parts:
            yield path


def area_folders(domain: str, tables: Dict[str, List[Dict[str, List[str]]]]) -> Tuple[str, List[Path]]:
    vault = next((v.get("name", [""])[0] for v in tables.get("vault", [])
                  if any(fnmatch.fnmatchcase(domain, p) for p in v.get("domains", []))), None)
    if vault is None:
        raise CheckError('no vault in vaults.toml owns domain "%s"' % domain)
    folders: List[Path] = []
    for area in tables.get("area", []):
        if area.get("domain", [""])[0] != domain:
            continue
        for raw in area.get("paths", []):
            folder = expand(raw)
            if vault_for_path(folder, tables) != vault:
                raise CheckError("area folder %s is not in vault %s; refusing to read its transcripts" % (folder, vault))
            folders.append(folder)
    if not folders:
        raise CheckError('domain "%s" has no [[area]] with folders in vaults.toml' % domain)
    return vault, folders


def cmd_mine(args: argparse.Namespace) -> int:
    tables = parse_vaults_toml(vaults_toml().read_text())
    vault, folders = area_folders(args.domain, tables)
    searches: List[Dict[str, Any]] = []
    files = 0
    for folder in folders:
        for path in transcript_files(folder):
            files += 1
            searches.extend(mine_file(path, args.domain, folders))
    candidates = build_candidates(searches)
    out = Path(args.out).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("".join(json.dumps(c, ensure_ascii=False) + "\n" for c in candidates))
    print("%s (vault %s): %d transcripts, %d searches, %d candidates with reads -> %s"
          % (args.domain, vault, files, len(searches), len(candidates), out))
    return 0


# --------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="score a dataset against the live domain")
    run.add_argument("--dataset", required=True)
    run.add_argument("--out", required=True)
    run.add_argument("--k", type=int, default=5)
    run.set_defaults(func=cmd_run)

    compare = sub.add_parser("compare", help="compare two reports")
    compare.add_argument("before")
    compare.add_argument("after")
    compare.add_argument("--format", choices=["markdown", "json"], default="markdown")
    compare.add_argument("--fail-on-regression", action="store_true")
    compare.set_defaults(func=cmd_compare)

    mine = sub.add_parser("mine", help="build candidate queries from Claude Code transcripts")
    mine.add_argument("--domain", required=True)
    mine.add_argument("--out", required=True)
    mine.set_defaults(func=cmd_mine)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except CheckError as exc:
        print("retrieval_check: %s" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
