---
name: reflect
description: Audit what Kioku already holds for a domain and report what to fix, changing nothing on its own. Find decisions that contradict each other or the current code, records whose status or links disagree, topic notes and architecture diagrams that no longer match the decisions in force, and unhealthy nodes (undecided proposals, orphans, stale code notes). Its health mode is a quick check with no agents, plus a retrieval check where the area has a dataset. The full audit runs a deterministic inventory, then a workflow of agents with adversarial verification. Use when the user asks to reflect on past decisions, or to audit, health-check, review or verify the memory or a knowledge base. To fix stale values, use memory-refresh.
argument-hint: "[domain] [health | since | all | <topic>]"
---

> **Claude Code only.** This skill runs its agents with Claude Code's Workflow tool. In any other client, such as Codex, say that it needs Claude Code and stop.

# Reflect

`checkpoint` saves what one session settled. `reflect` audits everything stored, to find what has gone wrong over time:
- decisions that contradict each other;
- a later rule that silently overrides an earlier one;
- topic notes and diagrams that drifted from the decisions;
- code that moved under the notes.

The user starts it, and it changes nothing without their go-ahead.

## 1. Scope

- **Domain.** Use the argument, else the domain of the session's area, else the vault's domain that holds notes. Only audit domains of the session's vault, which Kioku's instructions name, and run from a folder of that vault.
- **The vault's rules.** Read `~/.config/kioku/rules/<vault>.md` when it exists. For a domain that mirrors a knowledge base, it names the bundle, the clones of the code the bundle describes and the ref each is checked at, and any tags that are never topics.
- **Source.** Each domain has one:

| Domain, for example | Source | Code it describes |
|---|---|---|
| `work/payments`, mirroring a knowledge base | the OKF bundle the vault's rules name, e.g. `<kb repo>/payments-kb` | the clones the rules name, each at its ref, e.g. `payments-api` at `origin/develop` |
| `personal/notes`, Markdown notes | the folder of Markdown notes Kioku imports, e.g. the Kioku checkout's `notes/` | the repo itself, at `origin/main` |

  Notes that agents saved have no file. List them with `memory_list` (`{"domain": "<domain>", "origin": "agent"}`, paging with `next_offset`). Give the workflow one more cluster, `{"name": "agent-notes", "kind": "kioku", "decisions": [<their keys>]}`: its agent reads them with `memory_get` and checks each against the decisions it links to. Leave the cluster out when there are none.
- **Mode.**
  - `health`: a quick brain-health check with no agents (see below). Use it when the user asks how the memory is doing, and offer it before a deep audit.
  - `since` (the default for an audit): topics never checked, plus topics whose decisions or topic note changed since they were last checked. Pass `--state ~/.cache/kioku-reflect/<domain with / as ->.json` to the inventory.
  - `all`: every topic.
  - A topic name, such as `architecture`.

## 2. Inventory, deterministic

`S` is this plugin's skills folder, the parent of this skill's folder. The state file also feeds the estimate in step 3, so pass `--state` in every mode; `since` uses it to skip unchanged topics.

```sh
S=<this plugin's skills folder>   # the parent of this skill's folder
OUT=$(mktemp -d)                  # or the session's scratchpad
STATE=~/.cache/kioku-reflect/<domain with / as ->.json
# OKF bundle: fetch the code first, then fold the drift scan into the inventory.
git -C <each clone the bundle describes> -c credential.helper='!gh auth git-credential' fetch -q origin
python3 -I $S/memory-refresh/scripts/kb_drift.py --kb <kb> --repos <folder with the clones> --fetch > $OUT/drift.json
python3 -I $S/reflect/scripts/reflect_inventory.py --kb <kb> --drift $OUT/drift.json \
  --state "$STATE" [--ignore-tags <the rules' tags that are never topics>] --out $OUT/inventory.json
# Markdown notes:
python3 -I $S/reflect/scripts/reflect_inventory.py --notes <folder> --domain <domain> --state "$STATE" --out $OUT/inventory.json
```

- The inventory checks supersession chains, Status text against `decision_status`, undecided proposals, answered questions, `stale_after` dates, orphans and drift, without a model.
- It also groups the decisions in force into topic clusters for the agents.
- For `all`, add `--all`: it keeps every topic, and the state still feeds the estimate.
- To keep a single topic, filter `clusters` in the JSON before passing it on.

## Health mode: stop here

Report a short health card, with no agents:

| Line | Source |
|---|---|
| Notes active and legacy, links, pending vectors | `memory_stats` |
| Notes agents saved, and how many link to nothing | `memory_list` with `origin: agent` (`links: 0`) |
| Undecided proposals, orphans, broken supersession chains, status mismatches, past `stale_after`, drift | the inventory |
| Topics last audited, and topics never audited | `~/.cache/kioku-reflect/<domain with / as ->.json` |
| Recall against the last baseline, when the area has a dataset | the retrieval check below |

The area has a dataset when `~/.config/kioku/evals/<vault>/<domain with / as ->/dataset.json` exists. Its baseline is `baseline.json` beside it. The check needs `$KIOKU_HOME`, the Kioku checkout: if it's unset, say so, since the user sets it in their shell profile or under `env` in Claude Code's settings.

```sh
R=$S/reflect/scripts/retrieval_check.py; D=~/.config/kioku/evals/<vault>/<domain with / as ->
python3 -I $R run --dataset $D/dataset.json --out $OUT/retrieval.json
python3 -I $R compare $D/baseline.json $OUT/retrieval.json --format markdown   # no baseline: report recall alone
```

A drop in recall is a `regression` item on the pending list (`$S/checkpoint/references/note-format.md`).

Then make up to three suggestions, for example:
- `/memory-refresh` for drift;
- the user's call on the oldest undecided proposals;
- `/reflect <topic>` for a topic that changed or was never audited.

Pending vectors above zero after a minute, or a `memory_stats` error, means a Kioku problem: suggest `kioku doctor`.

## 3. Run the workflow of agents

- **Say the size first:** the inventory's `estimate`, its agents and tokens. Each topic gets one check agent, then up to `maxVerify` (default 4) verification agents, then one report agent. The estimate is those agents at most, scaled by the agents per topic that past runs recorded, times the tokens per agent they recorded.
  - **Ask the user first** when `estimate.ask_first` is true: the estimate is above 1.5M tokens, or no run is recorded yet. Offer to go topic by topic.
  - Otherwise, go ahead.
- **Call the Workflow tool** with the full text of `$S/reflect/workflow.js` as `script`. Read it first: the tool loads a `scriptPath` only from the working directory. Pass these `args`:
  ```json
  {"domain": "<domain>", "source": "okf|markdown", "root": "<inventory root>",
   "clusters": [<inventory clusters>],
   "code": [{"repo": "payments-api", "path": "/abs/path", "ref": "origin/develop"}],
   "deterministic": {"counts": <inventory counts>, "samples": <first 5 findings per check>},
   "maxVerify": 4}
  ```
- **Every finding is checked by an independent skeptic.** Only confirmed findings reach the report as facts.

## 4. Report, then fix on request

- Show the report, and keep it short.
- Put each confirmed finding that waits for the user on the pending list (note format, as above).
- On the user's go-ahead:
  - **Record fixes** follow memory-refresh's write rules.
  - **Contradictions between decisions in force** are the user's call. The resolution is a new decision, which for a KB decision follows memory-refresh's "A KB decision changes" (the external tracker first when the vault's rules name one), never only in Kioku.
- **Record the run,** so `since` skips the topics it checked until they change, and the next estimate knows what a run costs. Use the topics and agents the workflow returned, and the run total the Workflow tool reported:
  ```sh
  python3 -I $S/reflect/scripts/reflect_inventory.py --mark "$STATE" [--kb <kb>] --topics <checked topics> \
    --stats '{"agents": <result agents>, "tokens": <the run total>}'
  ```
  With `--kb`, it records the topics as checked at the bundle's commit; without it, only the cost.

## Rules

- Stay inside the session's vault.
- Notes tagged `private-source` stay inside the team.
- Keep the scripts, the cache and the reports out of repositories.
