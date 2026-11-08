#!/usr/bin/env python3
"""Checks for the kioku-skills marketplace, run by `make lint`.

- Manifests: every JSON file parses. Claude Code's marketplace lists every plugin;
  Codex's lists exactly the plugins with a portable plugin.json at their root. A
  plugin's versions agree everywhere they appear, and its hooks name files it has.
- Skills: each SKILL.md has a description and a name that matches its folder, its
  relative links resolve, and a skill that runs a workflow.js starts with the line
  that stops clients without Claude Code's Workflow tool.
- Vendored copies match their source under shared/ (tools/vendor.py).
- Python files parse as Python 3.9, the oldest version the scripts support.
- Leaks: absolute home paths, ~/projects/, paths into the personal skills folder
  or Claude Code's installed plugins, and the owner's GitHub username anywhere but the
  manifests' owner and author fields and the address of the owner's repositories
  this marketplace links (kioku-skills itself and the Kioku server).
- `claude plugin validate --strict` on the marketplace and each plugin, when the
  Claude Code CLI is on PATH. CI does not depend on it.
"""

from __future__ import annotations

import ast
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import vendor  # noqa: E402  (tools/vendor.py: the shared files and their copies)

CLAUDE_MARKETPLACE = ROOT / ".claude-plugin" / "marketplace.json"
CODEX_MARKETPLACE = ROOT / ".agents" / "plugins" / "marketplace.json"
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".ruff_cache", ".mypy_cache"}
# The validator holds the leak patterns themselves.
SKIP_LEAK_SCAN = {Path(__file__).resolve()}
CLAUDE_ONLY_LINE = "> **Claude Code only.**"
OWN_REPOS = ("kioku-skills", "kioku")
MANIFEST_OWNER_FIELDS = ("owner", "author")
LEAKS = (
    (re.compile(r"(?<![\w.])/(?:Users|home)/[A-Za-z0-9._-]+"), "an absolute home path"),
    (re.compile(r"~/projects/"), "a path under ~/projects/"),
    (re.compile(r"~/\.claude/skills"), "a path into the personal skills folder, not this plugin"),
    (re.compile(r"\.claude/plugins/(?:cache|marketplaces)/"), "a path into Claude Code's installed plugins"),
)
LINK = re.compile(r"\]\(([^)\s#]+)(?:#[^)]*)?\)")


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def files() -> list:
    found = []
    for path in sorted(ROOT.rglob("*")):
        if path.is_file() and not SKIP_DIRS.intersection(path.relative_to(ROOT).parts):
            found.append(path)
    return found


def rel(path: Path) -> str:
    return str(path.relative_to(ROOT))


def frontmatter(skill: Path) -> tuple:
    """The leading `---` block of a SKILL.md as a flat mapping, and the body after it."""
    lines = skill.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, lines
    fields: dict = {}
    for number, line in enumerate(lines[1:], 1):
        if line.strip() == "---":
            return fields, lines[number + 1:]
        key, separator, value = line.partition(":")
        if separator:
            fields[key.strip()] = value.strip().strip('"')
    return {}, lines


# --- manifests --------------------------------------------------------------------

def check_json() -> list:
    errors = []
    for path in files():
        if path.suffix == ".json":
            try:
                load(path)
            except ValueError as error:
                errors.append(f"{rel(path)}: not valid JSON: {error}")
    return errors


def check_marketplaces(claude: dict, codex: dict) -> list:
    errors = []
    portable = set()
    for entry in claude.get("plugins", []):
        name, source = entry.get("name"), str(entry.get("source", ""))
        folder = ROOT / source.removeprefix("./")
        if not source.startswith("./plugins/") or not folder.is_dir():
            errors.append(f"{name}: source {source!r} is not a folder under ./plugins/")
            continue
        if not entry.get("description"):
            errors.append(f"{name}: the marketplace entry has no description")
        manifest = folder / ".claude-plugin" / "plugin.json"
        if not manifest.is_file():
            errors.append(f"{name}: missing {rel(manifest)}")
            continue
        versions = {"marketplace entry": entry.get("version"), rel(manifest): load(manifest).get("version")}
        if load(manifest).get("name") != name:
            errors.append(f"{rel(manifest)}: name {load(manifest).get('name')!r} is not {name!r}")
        codex_manifest = folder / "plugin.json"
        if codex_manifest.is_file():
            portable.add(name)
            data = load(codex_manifest)
            versions[rel(codex_manifest)] = data.get("version")
            interface = data.get("extensions", {}).get("com.openai", {}).get("interface", {})
            if data.get("name") != name or not data.get("description"):
                errors.append(f"{rel(codex_manifest)}: needs name {name!r} and a description")
            if not (interface.get("displayName") and interface.get("category")):
                errors.append(f"{rel(codex_manifest)}: extensions.com.openai.interface needs displayName and category")
        if len(set(versions.values())) != 1:
            errors.append(f"{name}: versions disagree: {versions}")
        errors += check_hooks(name, folder)
    by_source = {e.get("name"): e.get("source") for e in claude.get("plugins", [])}
    listed = set()
    for entry in codex.get("plugins", []):
        name = entry.get("name")
        listed.add(name)
        source, policy = entry.get("source", {}), entry.get("policy", {})
        if source.get("source") != "local" or source.get("path") != by_source.get(name):
            errors.append(f"codex marketplace: {name}'s source does not match Claude Code's ({by_source.get(name)!r})")
        if not (policy.get("installation") and policy.get("authentication") and entry.get("category")):
            errors.append(f"codex marketplace: {name} needs policy.installation, policy.authentication and category")
    if listed != portable:
        errors.append("the Codex marketplace must list exactly the plugins with a portable plugin.json: "
                      f"missing={sorted(portable - listed)} extra={sorted(listed - portable)}")
    return errors


def check_hooks(name: str, folder: Path) -> list:
    hooks = folder / "hooks" / "hooks.json"
    if not hooks.is_file():
        return []
    errors = []
    for match in re.finditer(r"\$\{CLAUDE_PLUGIN_ROOT\}/([\w./-]+)", hooks.read_text(encoding="utf-8")):
        if not (folder / match.group(1)).is_file():
            errors.append(f"{name}: hooks.json runs {match.group(1)}, which the plugin doesn't have")
    return errors


# --- skills -----------------------------------------------------------------------

def check_skills() -> list:
    errors = []
    skills = sorted(ROOT.glob("plugins/*/skills/*/SKILL.md"))
    if not skills:
        errors.append("no skills under plugins/*/skills")
    for skill in skills:
        fields, body = frontmatter(skill)
        if not fields:
            errors.append(f"{rel(skill)}: no YAML frontmatter")
            continue
        if fields.get("name") != skill.parent.name:
            errors.append(f"{rel(skill)}: name {fields.get('name')!r} does not match its folder")
        description = fields.get("description", "")
        if not description:
            errors.append(f"{rel(skill)}: frontmatter has no description")
        elif len(description) > 1024:
            errors.append(f"{rel(skill)}: description is {len(description)} characters, over 1024")
        first = next((line for line in body if line.strip()), "")
        if (skill.parent / "workflow.js").is_file() and not first.startswith(CLAUDE_ONLY_LINE):
            errors.append(f"{rel(skill)}: runs a workflow, so its body must start with {CLAUDE_ONLY_LINE!r}")
    for doc in sorted(ROOT.glob("plugins/*/skills/**/*.md")):
        for target in LINK.findall(doc.read_text(encoding="utf-8")):
            if "://" not in target and not target.startswith("mailto:") and not (doc.parent / target).exists():
                errors.append(f"{rel(doc)}: link to {target} does not resolve")
    return errors


def check_python_syntax() -> list:
    errors = []
    for path in files():
        if path.suffix == ".py":
            try:
                ast.parse(path.read_text(encoding="utf-8"), filename=rel(path), feature_version=(3, 9))
            except SyntaxError as error:
                errors.append(f"{rel(path)}:{error.lineno}: not valid Python 3.9: {error.msg}")
    return errors


def check_vendored() -> list:
    return [f"{copy} differs from {source}: run `make vendor`" for source, copy in vendor.stale_copies()]


# --- leaks ------------------------------------------------------------------------

def without_owner_fields(path: Path, text: str) -> str:
    """A manifest's text without its owner and author fields, where the owner's name belongs."""
    if path.suffix != ".json" or path.name not in ("marketplace.json", "plugin.json"):
        return text
    data = load(path)
    for field in MANIFEST_OWNER_FIELDS:
        data.pop(field, None)
    return json.dumps(data)


def check_leaks(owner: str) -> list:
    username = re.compile(rf"(?i)(?<![\w-]){re.escape(owner)}(?![\w-])(?!/(?:{'|'.join(OWN_REPOS)})(?![\w-]))")
    errors = []
    for path in files():
        if path.resolve() in SKIP_LEAK_SCAN:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for number, line in enumerate(text.splitlines(), 1):
            for pattern, what in LEAKS:
                if pattern.search(line):
                    errors.append(f"{rel(path)}:{number}: {what}: {line.strip()[:120]}")
        if owner and username.search(without_owner_fields(path, text)):
            errors.append(f"{rel(path)}: names the GitHub user {owner!r} outside an owner or author field, "
                          f"and not as the address of {' or '.join(OWN_REPOS)}")
    return errors


# --- claude plugin validate -----------------------------------------------------------

def run_claude_validator(claude: dict) -> list:
    if shutil.which("claude") is None:
        print("  skipped: claude CLI not on PATH")
        return []
    targets = [ROOT] + [ROOT / str(entry["source"]).removeprefix("./") for entry in claude["plugins"]]
    errors = []
    for target in targets:
        result = subprocess.run(["claude", "plugin", "validate", "--strict", str(target)], capture_output=True, text=True)
        label = target.relative_to(ROOT.parent)
        if result.returncode == 0:
            print(f"  ok {label}")
        else:
            errors.append(f"claude plugin validate failed for {label}:\n{result.stdout}{result.stderr}")
    return errors


def main() -> int:
    errors = check_json()
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    claude, codex = load(CLAUDE_MARKETPLACE), load(CODEX_MARKETPLACE)
    sections = (
        ("manifests", lambda: check_marketplaces(claude, codex)),
        ("skills", check_skills),
        ("vendored copies", check_vendored),
        ("python 3.9 syntax", check_python_syntax),
        ("leaks", lambda: check_leaks(str(claude.get("owner", {}).get("name") or ""))),
        ("claude plugin validate --strict", lambda: run_claude_validator(claude)),
    )
    for title, check in sections:
        found = check()
        print(f"{title}: {'ok' if not found else f'{len(found)} problem' + ('s' if len(found) > 1 else '')}")
        errors += found
    if errors:
        print("\nFAILED", file=sys.stderr)
        for error in errors:
            print(f"  ✘ {error}", file=sys.stderr)
        return 1
    print("\nAll checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
