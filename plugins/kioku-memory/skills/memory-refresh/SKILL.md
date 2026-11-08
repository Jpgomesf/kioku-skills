---
name: memory-refresh
description: Fix stale values in Kioku memory, or in the OKF knowledge base it imports, against the project's current code and sources, such as moved or deleted files, merged or closed PRs, gone branches, renamed commands and tools, ports and defaults. Use when the user asks to refresh, update or reconcile memories or a knowledge base with the code, when a note turns out to be stale during work, before relying on notes written before a large refactor, or when a new decision replaces or narrows a KB decision (the external tracker first, then the KB). To audit or review without changing anything, use reflect.
argument-hint: "[domain or area, e.g. work/payments or personal/notes]"
---

# Memory refresh

A memory helps only while it is true. This skill compares what memory says with what the code and sources say now, changes the values the evidence contradicts, and keeps everything else, including every decision and its reasons.

## 1. Scope and source of truth

- **Domain.** Use the argument, else the area or vault that Kioku's instructions name for this session.
- **The vault's rules.** Read `~/.config/kioku/rules/<vault>.md` when it exists. It names the domains that mirror a knowledge base, the bundle and the clones of the code it describes, the branch to check against, and any external tracker the KB's decisions mirror.
- **Paths.** `S` is this plugin's skills folder, the parent of this skill's folder. Re-imports need `$KIOKU_HOME`, the Kioku checkout: if it's unset, say so, since the user sets it in their shell profile or under `env` in Claude Code's settings.
- **Where each note's truth lives.** Check with `memory_inspect`, using `origin` and `source`:

| Origin | How to recognize it | Where to write |
|---|---|---|
| OKF bundle | `source` is a file in a folder whose `index.md` declares `okf_version`, such as a knowledge-base repository | The bundle's files, through its repo |
| Markdown note | `origin: file`, with `source` under `MEMORY_NOTES_ROOT`, e.g. the Kioku checkout's `notes/` | The file |
| Agent note | `origin: agent`, no file | `memory_save` with the same key and domain |

Kioku refuses `memory_save` over a file-backed note, and a bundle re-import would undo a direct save, so always write at the source.

## 2. Find the drift, deterministically first

- **OKF bundle.** Run the scanner, which is read-only apart from the optional `--fetch`. The vault's rules name the knowledge base and the folder that holds the clones:

  ```sh
  python3 -I $S/memory-refresh/scripts/kb_drift.py \
    --kb <bundle>/<knowledge base> --repos <folder with the clones> --fetch > "$(mktemp -d)/kb-drift.json"
  ```

  - It checks every Code File note (`repo:<repo>@<ref>:<path>`) against its own ref since the commit it was read at, every Pull Request note against GitHub, and every Git Branch note against the remote. Each clone's GitHub owner comes from its `origin` remote; `--org` overrides it.
  - Work only on entries with `"actionable": true`. A `historical`, `superseded` or `obsolete` note whose file is gone is already consistent.
  - The scanner skips tracker notes, such as Linear issues. If the tracker's connector is available, compare each ticket note's `state` with it; otherwise list them as unchecked.
- **Markdown and agent notes.** These have no anchors, so list each checkable claim and check it against the repo and the live config:
  - Read each note with `memory_get` and `"count_use": false`, since checking isn't use. If Kioku answers that it doesn't accept `count_use` (an older server), repeat the call without it. A reply stops at 2000 bytes by default; when it says `truncated`, pass `max_bytes` up to 8000.
  - The claims to check are names, paths, commands, ports, URLs, versions, counts, tool names and defaults.
  - Check with `rg`, by reading the file, or with `--help`.
  - Read a config's non-secret fields only. Never print `.env` values, API keys or tokens.

## 3. Decide, note by note

- Change only what the evidence contradicts. Each change needs a source: a commit, a file and line, or an API answer.
- **Code that moved.** Find the commit (`git log --diff-filter=D --format='%h %s' <ref> -- <old path>`) and read it. Map old paths to new ones, then update the path, ref, cited commit, `state` and `freshness`. Add a dated line saying what moved and in which commit.
- **Decisions are never changed in place.** When the code now contradicts one, tell the user. In a bundle, also record it as a proposed update or an open question, as the bundle's rules say, never as a supersession. A new decision that replaces or narrows a KB decision follows "A KB decision changes" below.
- **Notes that describe what code does** are the ones that rot. When you refresh one, keep the pointer (path, ref, commit), why it matters, and the decisions it implements. Drop any retelling of the code that a reader could get from the file itself.
- **Leave alone:** notes already marked historical, and claims you couldn't verify. List both in the report.

## 4. Write, by origin

- **OKF bundle (a shared repo).**
  1. Work on a branch.
  2. Edit the notes and bump `generated.at`, keeping `generated.by` as the bundle's rules say.
  3. Add dated entries to the knowledge base's `log.md` and to the bundle's `log.md`.
  4. Run the bundle's own checks and index build, as its README says.
  5. Commit following the repo's own rules.
  6. Ask the user before pushing or opening a PR.
  7. After the PR merges, re-import with the CLI, since `memory_import` refuses bundles. The CLI ignores vaults, so first check that `python3 $KIOKU_HOME/scripts/vaults.py where <bundle>` names the session's vault, and that `<domain>` is one of its domains. Then, from `$KIOKU_HOME`, run `bin/kioku --env-file .env ingest --path <bundle> --domain <domain>`. Its `okf` report shows what was retired, restored and removed.
- **A KB decision changes:** the one path every skill uses. The decision is never edited in place: the change is a new decision that replaces or narrows it. Keep a `kb` pending item open until the re-import.
  1. **The external tracker first,** when the vault's rules name one that the KB mirrors. The rules say where a decision sits there, such as a section of its ticket. If it's already there, cite it. Posting is outward-facing: draft the text for the user to post, or post it only with their explicit approval. If the tracker's only tool is `authenticate`, it isn't connected: say so, and hand over the draft.
  2. **Then the KB repository,** with the bundle steps above and its own rules, as its README states:
     - The new decision, `decisions/DEC-<next>-<slug>.md`, has `decision_status: active` and the fields the bundle requires, such as who decided it, when, and where step 1 recorded it. A replacement adds `supersedes`. Its `# Related` lists `Supersedes:` or `Narrows:`.
     - A replaced decision keeps its text and gets `decision_status: superseded`, `status: deprecated` and `superseded_by`, with "Superseded by" in its `# Status` and `# Related`.
     - A narrowed decision keeps its text and status, and gets a dated "Narrowed on <date>: …" line in `# Status` and a `Narrowed by:` line in `# Related`.
  3. **Finally, re-import** the bundle (step 7). An agent note that held the decision meanwhile now repeats the new DEC: retire it once the user confirms.
- **Markdown notes.**
  1. Back up the files.
  2. Edit them, including a line such as `Checked against the code on <date> (<commit>)`.
  3. Re-import each file with `memory_import` (`{"path": "<file, relative to the notes folder>"}`).
  4. Drop `related` entries that point into another domain; links resolve only within one.
- **Agent notes.** `memory_save` with the same key. No tool reads old text back, so the report keeps the old value.

## 5. Verify and report

- Re-run the scanner, or re-check each claim. The notes you touched should no longer drift.
- Search one changed value with `memory_search` and confirm that the new text comes back.
- Report, briefly:
  - notes checked;
  - notes changed, as old value → new value, with evidence;
  - notes left alone, and why;
  - claims you couldn't verify;
  - what the user must decide, such as decisions the code now contradicts.
- Put each item left for the user on the pending list, as the note format says (`$S/checkpoint/references/note-format.md`): `decision` or `kb` for a decision the code contradicts, `unverified` for a claim you couldn't verify.

## Rules

- Stay inside the session's vault. Never copy one vault's notes into another vault's domain.
- Keep assistant files out of repositories: skills, hooks and scan output stay local.
