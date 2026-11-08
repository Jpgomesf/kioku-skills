---
name: dream
description: Improve a project's Kioku notes, the way sleep consolidates memory. It splits notes that hold two facts, merges duplicates, sharpens titles, adds missing links, fixes replacement chains, anchors decisions to the code paths they govern, and keeps one hub note per topic so each part of the project has its own context. An independent agent checks every change. Rewrites of agent notes are saved at once; merges, replacements and file changes wait for the user on the pending list. It can also run on a schedule, from a desktop scheduled task or at the end of a session in an area. Use when the user asks to dream, consolidate, improve or restructure a project's or area's notes, or about the scheduled dream. To find contradictions without changing anything, use reflect; to add knowledge from a project's work, use project-sync.
argument-hint: "[domain] [topic | scheduled]"
---

> **Claude Code only.** This skill runs its agents with Claude Code's Workflow tool. In any other client, such as Codex, say that it needs Claude Code and stop.

# Dream

`project-sync` adds what a project's work settled. `reflect` audits what's stored and changes nothing. `dream` improves what's stored: the same knowledge, easier to find, read and trust, organised by topic and tied to the code it governs. It adds no new facts.

## 1. Scope

- **Domain.** Use the argument; else the session's area, which Kioku's instructions name. A second argument keeps one topic; `scheduled` runs the scheduled mode below.
- **Stay inside the session's vault,** and run from a folder of that vault. Notes imported from a knowledge base or Markdown file are proposed changes only: their source is fixed through `memory-refresh`, the external tracker first when the vault's rules name one.
- **The vault's rules.** Read `~/.config/kioku/rules/<vault>.md` when it exists.
- **Anchors need a Kioku server with note paths.** A session whose server predates them must reconnect Kioku in `/mcp`.

## 2. Inventory

- **Paths.** `S` is this plugin's skills folder, the parent of this skill's folder. The commands also need `$KIOKU_HOME`, the Kioku checkout: if it's unset, say so and stop, since the user sets it in their shell profile or under `env` in Claude Code's settings.

```sh
S=<this plugin's skills folder>              # the parent of this skill's folder
D=$S/dream/scripts/dream_state.py; R=$S/reflect/scripts/retrieval_check.py
date -u +%Y-%m-%dT%H:%M:%SZ                 # startedAt: take it before reading anything
python3 -I $D status --domain <domain> --json   # last_ok is the last successful dream
OUT=<scratchpad>/dream-<domain with / as ->; mkdir -p "$OUT"
```

- **Notes:** `memory_list` (`{"domain": "<domain>"}`, paging with `next_offset`). Keep each row's key, title, tags, origin, links, paths and updated.
- **Repositories:** the domain's entry in `~/.config/kioku/sync/<vault>.json` lists them. Otherwise use the area's folders: `python3 $KIOKU_HOME/scripts/vaults.py where <folder>` confirms where a folder is placed.
- **The code map** is read by the agents themselves, file by file. They use the `code_map` tool when the session has it, else the Kioku CLI in `kioku`.
- **Retrieval check.** When the area has a dataset, `~/.config/kioku/evals/<vault>/<domain with / as ->/dataset.json`, record the baseline: `python3 -I $R run --dataset <dataset.json> --out "$OUT/retrieval-before.json"`.

## 3. Run the workflow of agents

- **Say the size first:** 1 planning agent, 1 improving agent per topic (at most 8), up to `maxVerify` (default 30) verification agents (Sonnet, high effort), 1 apply agent and 1 report agent. For an area of about 40 notes, expect around 25 agents. Confirm before more than that, and offer one topic at a time.
- **Call the Workflow tool** with the full text of `$S/dream/workflow.js` as `script`. Read the file first: the tool loads a `scriptPath` only from the working directory. Pass JSON values as `args`:
  ```json
  {"domain": "<domain>", "vault": "<vault>", "startedAt": "<the date -u time>",
   "repos": [{"path": "<repo>", "github": "<owner/name>"}],
   "notes": [<memory_list rows>], "topic": null, "since": null, "maxTopics": null,
   "kioku": {"bin": "$KIOKU_HOME/bin/kioku", "envFile": "$KIOKU_HOME/.env"},
   "noteFormat": "<text of $S/checkpoint/references/note-format.md>", "maxVerify": 30}
  ```
  Expand `$KIOKU_HOME` in `kioku` to the checkout's absolute path.

## 4. After the run

1. **Save the result:** write the Workflow tool's return value, as JSON, to `$OUT/result.json`.
2. **Retrieval check, after.** With a dataset, run it again into `$OUT/retrieval-after.json`, and put `python3 -I $R compare "$OUT/retrieval-before.json" "$OUT/retrieval-after.json"` in the report.
3. **File what waits for the user** on the pending list, one item per decision, with the full change in a detail file:
   ```sh
   python3 -I $S/project-sync/scripts/file_pending.py --vault <vault> --domain <domain> --skill dream \
     --result "$OUT/result.json" [--before "$OUT/retrieval-before.json" --after "$OUT/retrieval-after.json"]
   ```
   - It files merges and replacements (kind `retire`), file-backed changes (`kb`), key collisions and skipped saves (`other`), and each query that got worse (`regression`).
   - It skips items already on the list.
   - If it exits 1, say so and give the file it names.
4. **Write the report** to `~/.local/state/kioku/dream/reports/<domain with / as ->-<YYYYMMDD-HHMM>.md` (UTC):
   - what was saved, by topic;
   - what was filed for the user, with the pending ids;
   - what went unverified;
   - the retrieval comparison.

   Then show it.
5. **Record the run**, also when it failed:
   ```sh
   python3 -I $D mark --domain <domain> --status ok|failed --report <report path> --counts '<result counts>'
   ```
   It is `failed` when the Workflow tool failed or the result lists `failed_stages`. A scheduled dream starts only after a successful one.
6. **On the user's go-ahead** for a pending item:
   - **A merge or a replacement:** save the merged or newer note if the run didn't. Its body says "Replaces <key> (<date>): <why>". Then `memory_retire` the old notes.
   - **A file-backed note:** follow `memory-refresh`'s write rules (edit the file, then re-import).
   - **Anything that contradicts a decision in force:** follow `decide`.
   - Then close the item: `python3 -I $S/checkpoint/scripts/kioku_pending.py done --vault <vault> <id> --resolution "<what was done>"`.

## Scheduled mode

A scheduled dream runs these steps unattended, in Claude Code on the user's computer, since it needs the local Kioku server. Use one of two launchers, not both:

- **A desktop scheduled task.** In the Claude desktop app's Code tab, a task set to run only on this computer, in the area's folder, weekly, with the instructions `/kioku-memory:dream <domain> scheduled`. It runs while the app is open and the computer is awake.
- **The kioku-hooks fallback,** off by default. With `"dream": {"enabled": true}` in `~/.config/kioku/hooks.json`, the end of an attended session in an area starts `dream_state.py maybe-start` with the config's `every_days`, `max_budget_usd`, `max_topics` and `max_verify`.
  - **When it runs:** only when it is due.
    - The last successful dream is older than `every_days`. A domain with no successful dream never is: the first dream is manual.
    - No run is in progress: a lock holds the run's pid and start time, stale after 6 hours.
    - No run failed in the last 24 hours.
  - **How it runs:** headless `claude -p` in the area's folder, with Opus and `--max-budget-usd`.
    - It has its own Kioku server, which also has `code_map`, and the kioku-hooks plugin is off.
    - Its tools and allowlist are only what this section needs. The workflow's agents inherit them.
    - It reads this skill from the kioku-memory plugin Claude Code installed, or from `$KIOKU_SKILLS_DIR`.

**What a scheduled run does differently:**
- It never asks a question. Where this skill would ask, it takes the choice that writes less, and it never retires a note.
- **Its settings.** The hooks' prompt gives the domain, `startedAt`, `since`, `maxTopics`, `maxVerify`, a run folder that already exists, and the report path. A run the desktop task started takes them itself:
  - `startedAt` from `date -u`, then `since` from `status`'s `last_ok`. With no `last_ok`, it stops and reports that the first dream is manual.
  - `maxTopics` 4 and `maxVerify` 20.
  - The run folder is `~/.local/state/kioku/dream/runs/<domain with / as ->-<YYYYMMDD-HHMM>`, and the report goes to the path in step 4.
- It builds the inventory as above. The area's folder is the repository when the sources file lists none.
- A run the hooks started runs each script by the absolute path its prompt gives, since its allowlist names them that way, and passes `"kioku": null`.
- It passes `since`, `maxTopics` and `maxVerify`. The workflow keeps the topics with the most notes changed since the last dream; with none, it proposes nothing.
- It runs steps 1 to 3 of "After the run" in the run folder: needs-user items, skipped saves and retrieval regressions go to the pending list.
- It writes the report, and marks the run with `--scheduled`, also when it fails.
- **If a run the hooks started ends without marking itself,** for example out of budget, the launcher records it as failed, with a report that says so.
- **Where to look:**
  - `python3 -I $D status --domain <domain> --json` gives the last run.
  - Reports are in `~/.local/state/kioku/dream/reports/`.
  - Each run's output is in `~/.local/state/kioku/dream/runs/`.
  - The launcher logs to `~/.local/state/kioku/dream/dream.log`.

## What it writes without asking

- **Saved at once:** rewrites, splits, new links and anchors on agent notes, and new or refreshed topic hubs.
- **Waits for the user:**
  - a merge that retires a note;
  - a replaced decision;
  - any change to a knowledge-base or Markdown file.

How the workflow applies it, in plain code:
- **Decision notes** are rewritten live. Their verifier also rejects any rewrite that changes what the decision decides.
- **Key collisions wait for the user.** A split's new note may not take a key that is stored or that another change in the run writes. Neither may two changes write one key.
- **Fresh reads.** Each note is read again right before its save. One that changed after the run started is skipped and filed for the user.
- **Aliases only grow.** A save that names aliases replaces them, so a stored note is saved with the aliases memory_get returned plus the new ones, never dropping one. An older Kioku that doesn't return aliases gets them only on new notes, and the report lists the rest.

## Rules

- **No new knowledge.** The pass only reshapes what the notes hold.
- **No descriptions of code.** An anchor says which code a note governs, never what the code does.
- **Anchors must match tracked files** (`git ls-files -- ':(glob)<glob>'`). A note about the whole project gets none.
- **Hubs stay short:** two to four sentences and their links. A hub states only what its linked notes hold.
- **How agents read Kioku.** `memory_get` with `"count_use": false`, because improving notes isn't using them, and `"max_bytes": 8000`, so a note isn't cut at the 2000-byte default.
  - An older server rejects `count_use`; read again without it.
  - A response marked truncated is read again with fewer keys.
- **Keep the skill and its reports out of repositories.**
