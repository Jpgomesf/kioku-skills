---
name: project-sync
description: Add what a project's work settled to its Kioku notes, or build its notes from an existing project. It reads what changed since the last sync (the last 3 months the first time) in commits and PRs, Slack threads, Linear issues and docs when connected, and the code map; harvests decisions, conventions, gotchas and fixes; checks each against the stored notes; has an independent agent try to refute every proposal; saves the verified new notes; and files contradictions and file changes on the pending list for the user to decide. Use when the user asks to sync, backtrack, harvest or catch up a project's memory, or to build its notes from the project's history. For one session's decisions use checkpoint; to audit stored notes use reflect; to fix stale values use memory-refresh; to reshape stored notes use dream.
argument-hint: "[domain ... | vault] [dry-run]"
---

> **Claude Code only.** This skill runs its agents with Claude Code's Workflow tool. In any other client, such as Codex, say that it needs Claude Code and stop.

# Project sync

`checkpoint` saves what one session settled. `project-sync` catches a whole project's memory up with its work: what people decided in PRs, threads and issues since the last sync, checked against what Kioku already holds. On a project it never synced, it builds the notes from the last 3 months.

## 1. Scope

- **Projects.** The argument's domains; else the session's area, which Kioku's instructions name. `vault` means every area of the session's vault: list them, and ask which to include.
- **Stay inside the session's vault,** and run from a folder of that vault. Kioku refuses domains of other vaults anyway.
- **The vault's rules.** Read `~/.config/kioku/rules/<vault>.md` when it exists. For a domain that mirrors a knowledge base, as the rules name it, the sync proposes; knowledge-base changes go through `memory-refresh`, the external tracker first when the rules name one.

## 2. Sources: discover once, confirm, reuse

The sources file is `~/.config/kioku/sync/<vault>.json`, one per vault so vaults never mix. Its format is in `scripts/sync_inventory.py`. When the domain has no entry, discover its sources:

- **Repositories:** the area's folders (`python3 $KIOKU_HOME/scripts/vaults.py where <folder>` confirms where one is placed), and each one's GitHub name (`gh repo view --json nameWithOwner`).
- **Slack:** `slack_search_channels` for the project's and the repository's names, and `slack_search_public_and_private` for the repository name or its GitHub URL over the last 3 months. Note the channels where it comes up, and how often.
- **Linear:** if its tools are more than `authenticate`, list the teams and projects that match. Otherwise Linear isn't connected: say so, and that `/mcp` connects it.
- **Docs:** `notion-search` and Google Drive `search_files` for the project's name.

Hand the searching to one Agent when there are many hits, to keep this context small. Then show what was found and ask which to keep: AskUserQuestion, multiSelect, one question per kind, the strongest four candidates each. Write the confirmed entry with `"confirmed": "<date>"`. Run discovery again when the user wants to add sources.

## 3. Inventory, deterministic

- **Paths.** `S` is this plugin's skills folder, the parent of this skill's folder. `K` is `$KIOKU_HOME`, the Kioku checkout: if it's unset, say so and stop, since the user sets it in their shell profile or under `env` in Claude Code's settings.

```sh
S=<this plugin's skills folder>    # the parent of this skill's folder
K=$KIOKU_HOME; R=$S/reflect/scripts/retrieval_check.py
STATE=~/.cache/kioku-sync/<domain with / as ->.json
OUT=<scratchpad>/sync-<domain with / as ->; mkdir -p "$OUT"
date -u +%Y-%m-%dT%H:%M:%SZ        # startedAt: take it before reading anything
python3 -I $S/project-sync/scripts/sync_inventory.py --domain <domain> \
  --sources ~/.config/kioku/sync/<vault>.json --state "$STATE" \
  --kioku $K/bin/kioku --env-file $K/.env --fetch --max-verify 30 --out "$OUT/inventory.json"
```

- It reads commits since the last synced commit, and merged PRs and PRs closed without merging (which often record a rejected option) since the last run. The first time, it reads 3 months back.
- It hands back, as `carried`, the proposals the last run left unverified.
- It prints counts: commits, PRs, batches, Slack channels, Linear, docs, carried proposals.
- **`truncated`** lists any PR list that gh cut at its limit. Say so: this run reads up to the newest PR it got, and the next run carries on from there.
- **`estimate`**: the planned agents, times the tokens per agent that past runs recorded.
- Then list the domain's notes with `memory_list` (`{"domain": "<domain>"}`, paging with `next_offset`), keeping key, title, tags and origin.
- **Retrieval check.** When the area has a dataset, `~/.config/kioku/evals/<vault>/<domain with / as ->/dataset.json`, record the baseline: `python3 -I $R run --dataset <dataset.json> --out "$OUT/retrieval-before.json"`.

## 4. Run the workflow of agents

- **Say the size first:** the estimate's agents and tokens.
  - Harvest takes one agent per batch and per Slack channel, plus one for Linear and one for docs, at most 6 at a time.
  - Then one merge agent, up to 8 reconcile agents, and up to `maxVerify` (default 30) verification agents (Sonnet, high effort), carried proposals first. Then one apply agent and one report agent.
  - **Ask the user first** when `estimate.ask_first` is true: the estimate is above 1.5M tokens, or no run is recorded yet. Offer a shorter window (`--first-months`) or fewer sources.
- **`dry-run`** stops after the inventory and reports the size.
- **Keep batch texts out of this context.** Build `batches` with `jq '[.batches | to_entries[] | .value + {index: .key} | del(.text)]' inventory.json`. Each harvest agent then reads its own batch from the file. Take `carried` from `jq '.carried' inventory.json`.
- **Call the Workflow tool** with the full text of `$S/project-sync/workflow.js` as `script`. Read the file first: the tool loads a `scriptPath` only from the working directory. Pass JSON values, not strings, as `args`:
  ```json
  {"domain": "<domain>", "vault": "<vault>", "startedAt": "<the date -u time>",
   "window": {"since": "<inventory since>", "until": "<inventory generated_at>", "first_run": true},
   "inventory": "<path of inventory.json>",
   "batches": [<inventory batches without "text", each with its "index">],
   "slack": [<inventory slack>], "linear": <inventory linear>,
   "docs": [<inventory docs>], "codemap": {<inventory codemap>}, "notes": [<memory_list rows>],
   "carried": [<inventory carried>],
   "noteFormat": "<text of $S/checkpoint/references/note-format.md>", "maxVerify": 30}
  ```

## 5. After the run

1. **Save the result:** write the Workflow tool's return value, as JSON, to `$OUT/result.json`.
2. **Retrieval check, after.** With a dataset, run it again into `$OUT/retrieval-after.json`, and put `python3 -I $R compare "$OUT/retrieval-before.json" "$OUT/retrieval-after.json"` in the report.
3. **File what waits for the user** on the pending list, one item per decision, with the full proposal in a detail file:
   ```sh
   python3 -I $S/project-sync/scripts/file_pending.py --vault <vault> --domain <domain> --skill project-sync \
     --result "$OUT/result.json" [--before "$OUT/retrieval-before.json" --after "$OUT/retrieval-after.json"]
   ```
   - It files each `needs_user` entry, each note skipped at save time, and each query that got worse (kind `regression`).
   - It skips items already on the list.
   - If it exits 1, the pending list couldn't take some items. Say so, and give the file it names.
4. **Show the report:**
   - the notes saved;
   - what was filed for the user's decision, with the pending ids;
   - what went unverified (carried to the next run);
   - which sources were read, which were unavailable, and which failed;
   - the retrieval comparison.
5. **Record where the next run starts**, unless `failed_sources` is not empty:
   ```sh
   python3 -I $S/project-sync/scripts/sync_inventory.py --mark "$STATE" --inventory "$OUT/inventory.json" \
     --result "$OUT/result.json" --stats '{"agents": <result agents>, "tokens": <the run total the Workflow tool reported>}'
   ```
   - It stores the unverified proposals, and those whose save failed, for the next run.
   - It keeps the cursor of each unavailable source.
   - **When a harvest source or a topic's reconcile failed** (`failed_sources`), don't mark: the script refuses anyway. Name what failed; the next run reads the same window again.
   - Don't mark after a run that didn't finish either.
6. **On the user's go-ahead** for a pending item:
   - **Contradictions and decision updates** follow `decide`. Record the resolution where the decision lives (the tracker, the thread, the knowledge base), never only in Kioku.
   - **Knowledge-base and Markdown changes** follow `memory-refresh`'s write rules.
   - **A replaced agent note** gets "Replaces <key> (<date>)" in the new note, and then `memory_retire`.
   - Then close the item: `python3 -I $S/checkpoint/scripts/kioku_pending.py done --vault <vault> <id> --resolution "<what was done>"`.

## What it writes without asking

- **Saved at once:** verified new notes and refreshed agent notes.
- **Waits for the user:** anything that contradicts or replaces a decision, retires a note, or changes a file-backed note.
- **Never:** posting to Slack, Linear or GitHub. The sync only reads them.

How the workflow applies it, in plain code:
- **Decision notes.** An update to a decision note (tag `decision`, or a key containing `decisions/` or `DEC-`) waits for the user.
- **Collisions.** So does a new note whose key is already stored, and a second change to a key in the same run.
- **Fresh reads.** Each note is read again right before its save. One that changed after the run started is skipped and filed for the user.
- **Aliases only grow.** A save that names aliases replaces them, so a stored note is saved with the aliases memory_get returned plus the new ones, never dropping one. An older Kioku that doesn't return aliases gets them only on new notes, and the report lists the rest.

## Rules

- Notes whose only evidence is a private channel or a DM are tagged `private-source`.
- Notes name people by role, never by name.
- **How agents read Kioku.** `memory_get` with `"count_use": false`, because comparing isn't use, and `"max_bytes": 8000`, so a note isn't cut at the 2000-byte default.
  - An older server rejects `count_use`; read again without it.
  - A response marked truncated is read again with fewer keys.
- Keep the scripts, the sources file, the state and the reports out of repositories.
