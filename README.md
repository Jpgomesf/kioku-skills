# kioku-skills

Skills and hooks for [Kioku](https://github.com/Jpgomesf/kioku), a local MCP memory server for coding agents. They keep a project's memory useful: they bring notes into each session, save what a session settled, check decisions before new ones are made, fix notes that went stale, and audit and tidy what is stored.

This repository is a plugin marketplace for Claude Code and Codex. For the server itself, its setup and the habits that get the most out of it, see Kioku's [README](https://github.com/Jpgomesf/kioku#quick-start) and its guide to [using Kioku well](https://github.com/Jpgomesf/kioku/blob/main/docs/usage.md).

## Plugins

| Plugin | Clients | What it does |
|---|---|---|
| `kioku-memory` | Claude Code, Codex | Six skills, [listed below](#which-skill-when). |
| `kioku-hooks` | Claude Code only | Brings memory into every session without being asked, and saves what a session settled. [What each hook does](#what-the-hooks-do). |

`project-sync`, `reflect` and `dream` run their agents with Claude Code's Workflow tool, which Codex doesn't have. In Codex they say so and stop. `kioku-hooks` reads Claude Code's transcript format, so Codex's marketplace doesn't list it.

## Install

You need:

- **A running Kioku server,** connected as an MCP server named `kioku` (Kioku's [quick start](https://github.com/Jpgomesf/kioku#quick-start)).
- **`KIOKU_HOME`,** set to the folder of your Kioku checkout. The scripts run its `bin/kioku` and `scripts/vaults.py`, and the hooks start short-lived Kioku servers from it. Without it, the hooks say so at session start and stay off, and the skills say so and stop where they need it. Set it in your shell profile, and for Claude Code also under `env` in `~/.claude/settings.json`:

  ```json
  {"env": {"KIOKU_HOME": "/absolute/path/to/kioku"}}
  ```

- `python3` 3.9 or later, `git`, and `gh` for the skills that read GitHub. The hooks run on `/usr/bin/python3`.

### Claude Code

```sh
claude plugin marketplace add Jpgomesf/kioku-skills
claude plugin install kioku-memory@kioku-skills
claude plugin install kioku-hooks@kioku-skills
```

The skills appear namespaced, as `/kioku-memory:checkpoint`. Update with `claude plugin marketplace update kioku-skills`, then `claude plugin update kioku-memory@kioku-skills` and `claude plugin update kioku-hooks@kioku-skills`, and restart sessions.

### Codex

```sh
codex plugin marketplace add Jpgomesf/kioku-skills
```

Then install `kioku-memory` from Codex's plugin browser (`/plugins`). Update with `codex plugin marketplace upgrade kioku-skills`.

## Which skill when

| Skill | Use it when | Example |
|---|---|---|
| `checkpoint` | Before you compact or end a session, or when you say "remember this" | `/kioku-memory:checkpoint the retry decision` |
| `decide` | A design, data-model, tooling or convention choice comes up | `/kioku-memory:decide which amount refund retries charge` |
| `memory-refresh` | A note turns out stale, or before you rely on notes older than a refactor | `/kioku-memory:memory-refresh work/payments` |
| `project-sync` | A project's notes are behind its commits, PRs and threads, or it has none | `/kioku-memory:project-sync personal/notes dry-run` |
| `reflect` | You want a health check, or an audit for contradictions that changes nothing | `/kioku-memory:reflect work/payments health` |
| `dream` | An area's notes need splitting, merging, linking and one hub per topic | `/kioku-memory:dream personal/notes` |

Every skill reads Kioku with `count_use: false`, so checking a note doesn't keep it from aging, and puts what needs your decision on [the pending list](#the-pending-list). Retiring a note always waits for your go-ahead.

### checkpoint

You and Claude settle that refund retries charge the amount stored at request time, and a test proves the fix. Before compacting, run `/kioku-memory:checkpoint`.

- **Saves at once:** the decision, with its reason and the option you rejected, and the verified fix with its root cause, each updating an existing note when one holds the fact. Usually zero to three notes.
- **Asks first:** when it isn't sure you confirmed something, and before retiring a note the new decision replaces.
- **Pending list:** whatever is left open, such as an unanswered question or a knowledge-base change.

### decide

A teammate asks whether retries should recompute the order total. Run `/kioku-memory:decide which amount refund retries charge`: it finds the stored decisions and the ones they link to, checks the code they name, and says whether the question is settled, constrained, contested or open.

- **Saves at once:** nothing before you choose. After you confirm, one decision note with the options rejected, linked to the decisions it builds on or replaces.
- **Asks first:** before retiring the note it replaces.
- **Pending list:** a change to a knowledge-base decision, which goes through `memory-refresh`, the external tracker first when the vault's rules name one.

### memory-refresh

A refactor moved `services/refunds/` and renamed a command. Run `/kioku-memory:memory-refresh work/payments`: it compares each checkable claim (paths, commands, ports, versions, PRs, branches) with the code and sources, and changes only what the evidence contradicts.

- **Saves at once:** stale values in agent notes; Markdown notes, after a backup, re-imported; a knowledge-base bundle's notes, committed on a branch of its repository.
- **Asks first:** before pushing that branch or opening a PR.
- **Never:** changes a decision in place. A decision the code now contradicts goes on the pending list, and so does each claim it couldn't verify.

### project-sync

A project ran for a month without checkpoints, or has no notes yet. Run `/kioku-memory:project-sync personal/notes dry-run` to see the size, then without `dry-run`. It reads commits, merged and closed PRs, and, when connected, Slack, Linear and docs since the last sync (three months the first time), and an independent agent tries to refute every proposal.

- **Asks first:** which sources to read, the first time; and before its first run or any run estimated above 1.5M tokens.
- **Saves at once:** verified new notes and refreshed agent notes.
- **Pending list:** anything that contradicts or replaces a decision, retires a note, or changes a file-backed note. It never posts to Slack, Linear or GitHub.

### reflect

Once a month, run `/kioku-memory:reflect work/payments health` for a quick card with no agents: notes, links, orphans, undecided proposals, drift, and recall against a baseline. For a full audit, `/kioku-memory:reflect work/payments` checks topics that changed since the last audit, and an independent skeptic checks every finding.

- **Asks first:** before its first audit, or one estimated above 1.5M tokens.
- **Saves at once:** nothing. It changes no note on its own.
- **Pending list:** each confirmed finding that waits for you, and each drop in recall. Fixes happen only on your go-ahead.

### dream

An area's notes grew by hand and by background reviews, and now repeat each other. Run `/kioku-memory:dream personal/notes`: it splits notes that hold two facts, merges duplicates, sharpens titles, adds links, anchors decisions to the code paths they govern, and keeps one short hub note per topic. An independent agent checks every change.

- **Asks first:** before a run of more than about 25 agents; it offers one topic at a time.
- **Saves at once:** rewrites, splits, new links and anchors on agent notes, and new or refreshed topic hubs. A rewrite never changes what a decision decides, and a note saved with new aliases keeps its old ones.
- **Pending list:** merges that retire a note, replaced decisions, any change to a knowledge-base or Markdown file, and notes that changed during the run.
- **Scheduled:** a scheduled run never asks and never retires a note ([below](#the-scheduled-dream)).

## What the hooks do

Each hook reaches Kioku through a short-lived server started in the session's folder, so vault placement and locks apply as in the session itself.

| When | Hook | What it does |
|---|---|---|
| Session start | `SessionStart` | Names the session's vault and area, and lists up to 15 of the area's notes, newest first. Adds what background reviews saved since the last session, the items waiting for your decision, knowledge-base notes the drift scan found stale, and a dream that finished. |
| Each prompt | `UserPromptSubmit` | Adds notes for IDs the prompt names (`id_patterns`) and up to three notes the prompt is about, each once per session. Skips short prompts, slash commands and replies such as "ok". |
| Opening a file | `PreToolUse` on Read and Edit | Adds the notes anchored to the file through `paths`, up to five, and knowledge-base notes about it (`bundles`), each once per session. |
| End of a turn | `Stop` | After a commit, or every 8 prompts without a save, asks Claude to save what that stretch settled with the `checkpoint` skill. After a commit, also updates the repository's code map in the background, with Haiku writing missing purposes. |
| Compaction | `PreCompact` | Starts a background review of the transcript so far. |
| Session end | `SessionEnd` | Starts a review of the rest of the session, and a scheduled dream when you turned it on and one is due. |

- **The background review** is a headless `claude -p` (Sonnet, high effort) that can only search, read and save Kioku notes in the session's vault. It saves what the user confirmed or a command verified, and its saves go live. A proposal to retire a note or change a knowledge-base decision goes on the pending list instead. Each session start says what reviews saved.
- **Only sessions a person attends** nudge and review; `claude -p` runs don't.
- **Spending caps:** 24 review runs and 2.5 million characters a day across vaults, and $2 per run. What a session-end review leaves unread goes on the pending list, with the command that reviews it.

## Local files

Nothing personal ships in the plugins. What belongs to one machine or one vault stays in local files, outside every repository:

| File | What it holds | Read by |
|---|---|---|
| `~/.config/kioku/rules/<vault>.md` | A vault's own rules, in plain Markdown (example below) | the skills, and the hooks' background review |
| `~/.config/kioku/hooks.json` | Your settings for `kioku-hooks`, over the shipped `config.json`, one level deep (example below) | the hooks |
| `~/.config/kioku/sync/<vault>.json` | The sources `project-sync` reads for each area, confirmed by you | `project-sync`, `dream` |
| `~/.config/kioku/evals/<vault>/<domain>/dataset.json` | An optional retrieval dataset for an area (`/` in the domain becomes `-`) | `reflect`, `project-sync`, `dream` |
| `~/.local/state/kioku/pending/<vault>.json` | The pending list: items that wait for your decision | every skill and the hooks |
| `~/.local/state/kioku/dream/` | Each domain's last dream, its reports, runs and log | `dream` and the hooks |

### A vault's rules

A vault's rules say what is particular to it: which domains mirror a knowledge base, where that knowledge base and its code live, the branch decisions are checked against, and an external tracker that the knowledge base's decisions mirror. For example, `~/.config/kioku/rules/work.md`:

```markdown
# Rules for the work vault

## Knowledge bases
- `work/payments` mirrors the OKF bundle `~/src/acme-kb`, knowledge base `payments-kb`.
  - It describes the clones in `~/src/acme`: `payments-api` at `origin/develop`, `infra` at `origin/main`.
  - Topic tags: refunds, ledger, payouts. Tags that are never topics: team-only.

## External tracker
- The knowledge base's decisions mirror Jira project PAY. A decision is recorded in its ticket's description.
- A KB decision changes in Jira first, then in the knowledge base's repository, then by a re-import.
```

### Hook settings

`~/.config/kioku/hooks.json` overrides the shipped [`config.json`](plugins/kioku-hooks/config.json), one level deep. For example, to bring a knowledge base's notes up when a file it covers is opened, and to look up IDs that prompts name:

```json
{
  "bundles": [{"domain": "work/payments", "kb": "~/src/acme-kb/payments-kb", "repos": "~/src/acme"}],
  "id_patterns": ["ADR-\\d+", "PAY-\\d+"]
}
```

The settings you are most likely to change:

| Setting | Default | What it does |
|---|---|---|
| `kioku_home` | empty | The Kioku checkout, when `KIOKU_HOME` isn't set |
| `provider` | `anthropic` | The `MEMORY_PROVIDER` the hooks' servers and the review declare |
| `prefetch.min_relevance` | `0.68` | How relevant a note must be for a prompt to bring it up; set for `nomic-embed-text`, so recalibrate after changing the embedding model |
| `prefetch.max_notes` | `3` | Notes a prompt brings up at most |
| `index.max_notes` | `15` | Notes listed at session start |
| `nudge.every_prompts` | `8` | Prompts without a save before Claude is asked to save |
| `codemap.enabled`, `codemap.summarize` | `true`, `true` | Update the code map after a commit, and have Haiku write purposes for files without a doc comment |
| `review.model`, `review.effort` | `sonnet`, `high` | The background review's model |
| `review.max_runs_per_day`, `review.max_chars_per_day`, `review.max_budget_usd` | `24`, `2500000`, `2.0` | The review's spending caps |
| `dream.enabled`, `dream.every_days` | `false`, `7` | The session-end dream trigger ([below](#the-scheduled-dream)) |

The rest, with comments, are in `DEFAULTS` in [`kioku_hook.py`](plugins/kioku-hooks/scripts/kioku_hook.py). `KIOKU_HOOKS=off` turns every hook off.

## The pending list

Skills and hooks save notes live, but anything that needs your decision waits on one list per vault: a note to retire, a knowledge-base change, a decision the code contradicts, a claim nobody could verify, a retrieval regression, or a transcript a review couldn't read. With the hooks, each session start says how many items wait and shows the oldest three, with the command that lists the rest. Tell Claude what you decided; it carries it out and marks the item done. Done items are pruned after 90 days.

## The scheduled dream

`dream` can run every week on its own. It needs Claude Code and the local Kioku server, so it runs on your computer, in one of two ways. Use one, not both.

1. **A desktop scheduled task (recommended).** In the Claude desktop app's Code tab, open Scheduled, then New task, and set it up manually. Under Advanced settings, turn on Only on this computer. Pick the area's folder as the local folder and a weekly trigger, with these instructions:

   ```
   /kioku-memory:dream <domain> scheduled
   ```

   The task runs while the app is open and the computer is awake. Try it once with Run now, and allow what it asks for, so later runs don't stop on a question. The first dream of a domain is always one you start yourself, and a scheduled run never retires a note: merges and replacements wait on the pending list.

2. **The hooks' session-end trigger,** a fallback that is off by default. With `"dream": {"enabled": true}` in `~/.config/kioku/hooks.json`, ending a session in an area starts a headless dream when one is due: the last successful dream is older than `every_days` (7), no run is in progress, and none failed in the last 24 hours. It runs `claude -p` with Opus, a budget cap (`max_budget_usd`, 20), and only the tools and commands the dream needs. It finds the dream skill in the `kioku-memory` plugin Claude Code installed, or in `$KIOKU_SKILLS_DIR`.

Each run writes its report to `~/.local/state/kioku/dream/reports/`, and the next session start in the area says how it went.

## How the repository fits together

```
kioku-skills/
├── .claude-plugin/marketplace.json   Claude Code marketplace: both plugins
├── .agents/plugins/marketplace.json  Codex marketplace: kioku-memory
├── shared/                           files both plugins use, edited only here
│   ├── kioku_pending.py              the pending list and its command line
│   ├── note-format.md                how notes are shaped and where they go
│   ├── dream_state.py                the dream's state, schedule and launcher
│   ├── kb_drift.py                   drift scan of an OKF knowledge base
│   └── tests/
├── plugins/
│   ├── kioku-memory/
│   │   ├── .claude-plugin/plugin.json  Claude Code manifest
│   │   ├── plugin.json                 Codex manifest
│   │   └── skills/<skill>/             SKILL.md, scripts, references, tests
│   └── kioku-hooks/
│       ├── .claude-plugin/plugin.json
│       ├── config.json                 generic defaults
│       ├── hooks/hooks.json
│       ├── references/                 note-format.md (vendored)
│       ├── scripts/                    kioku_hook.py, and the vendored shared scripts
│       └── tests/
└── tools/
    ├── validate.py                   make lint
    └── vendor.py                     make vendor
```

Installing a plugin copies only its own folder, so each plugin is self-contained. A file both need lives once under `shared/` and is vendored into each plugin that uses it ([`tools/vendor.py`](tools/vendor.py) lists where). Inside a plugin, scripts find each other by paths relative to themselves. The skills read the checkpoint skill's copy of the pending list's script; the hooks read their own.

## Development

```sh
make lint     # manifests, skills, vendored copies, Python 3.9 syntax, leaks; claude plugin validate when installed
make test     # every Python suite, then the Node tests of the workflows
make vendor   # copy shared/ into the plugins after editing it
```

- Edit a shared file under `shared/`, then run `make vendor`. `make lint` fails while a copy differs.
- Claude Code picks up a change only when the version moves. Bump it in the marketplace entry and the plugin's `.claude-plugin/plugin.json`, and for `kioku-memory` also in its `plugin.json`; `make lint` fails if they disagree.
- `make lint` also fails on personal paths (an absolute home folder, the home folder's `projects` folder, a plugin installed elsewhere) and on the owner's GitHub username outside the manifests' owner and author fields and this repository's and Kioku's addresses. Examples use made-up domains such as `work/payments` and `personal/notes`.
- The scripts use only the standard library and run on Python 3.9. CI runs both checks on 3.9 and 3.13.

## License

MIT
