# kioku-skills

Skills and hooks for [Kioku](https://github.com/Jpgomesf/kioku), a local MCP memory server for coding agents. They keep a project's memory useful: they save what a session settled, check decisions before making new ones, fix notes that went stale, and audit and tidy what is stored.

This repository is a plugin marketplace for Claude Code and Codex.

## Plugins

| Plugin | Clients | What it does |
|---|---|---|
| `kioku-memory` | Claude Code, Codex | Six skills, listed below. |
| `kioku-hooks` | Claude Code only | Brings memory into every session without being asked. At session start it lists the area's notes and the items waiting for the user. For each prompt it adds notes on the IDs and subjects the prompt names, and for each file opened, the notes anchored to that file. After a commit or every few prompts it asks Claude to save what was settled. At compaction and session end, a background Sonnet reviews the transcript and saves what qualifies. |

The `kioku-memory` skills:

| Skill | Use it to | Clients |
|---|---|---|
| `checkpoint` | Save what this session settled: confirmed decisions, verified fixes, gotchas, conventions | Claude Code, Codex |
| `decide` | Check the stored decisions on a question before deciding it, then record the decision | Claude Code, Codex |
| `memory-refresh` | Fix stale paths, PRs, branches, commands and defaults in notes or an OKF knowledge base | Claude Code, Codex |
| `project-sync` | Add what a project's commits, PRs, threads and issues settled since the last sync | Claude Code only |
| `reflect` | Audit a domain for contradicting decisions, broken records and drift, changing nothing | Claude Code only |
| `dream` | Consolidate a domain's notes: split, merge, link, anchor to code, one hub per topic | Claude Code only |

`project-sync`, `reflect` and `dream` run their agents with Claude Code's Workflow tool, which Codex doesn't have. In Codex they say so and stop. `kioku-hooks` reads Claude Code's transcript format, so Codex's marketplace doesn't list it.

## What they need

- **A running Kioku server,** connected as an MCP server named `kioku` (see Kioku's [Connect a client](https://github.com/Jpgomesf/kioku#connect-a-client)).
- **`KIOKU_HOME`,** set to the folder of your Kioku checkout. The scripts run its `bin/kioku` and `scripts/vaults.py`, and the hooks start short-lived Kioku servers from it. Without it, the hooks say so at session start and stay off, and the skills say so and stop where they need it. Set it in your shell profile, and for Claude Code also under `env` in `~/.claude/settings.json`:

  ```json
  {"env": {"KIOKU_HOME": "/absolute/path/to/kioku"}}
  ```

- `python3` 3.9 or later, `git`, and `gh` for the skills that read GitHub. The hooks run on `/usr/bin/python3`.

## Install

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

## Local files

Nothing personal ships in the plugins. What belongs to one machine or one vault stays in local files, outside every repository:

| File | What it holds | Read by |
|---|---|---|
| `~/.config/kioku/rules/<vault>.md` | A vault's own rules, in plain Markdown (example below) | the skills, and the hooks' background review |
| `~/.config/kioku/hooks.json` | Your settings for `kioku-hooks`, over the shipped `config.json`, one level deep (example below) | the hooks |
| `~/.config/kioku/sync/<vault>.json` | The sources `project-sync` reads for each area, confirmed by you | `project-sync`, `dream` |
| `~/.config/kioku/evals/<vault>/<domain>/dataset.json` | An optional retrieval dataset for an area | `reflect`, `project-sync`, `dream` |
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

`~/.config/kioku/hooks.json` overrides the shipped [`config.json`](plugins/kioku-hooks/config.json). For example, to bring a knowledge base's notes up when a file it covers is opened, and to look up IDs that prompts name:

```json
{
  "bundles": [{"domain": "work/payments", "kb": "~/src/acme-kb/payments-kb", "repos": "~/src/acme"}],
  "id_patterns": ["ADR-\\d+", "PAY-\\d+"]
}
```

The other settings, with their defaults, are in [`kioku_hook.py`](plugins/kioku-hooks/scripts/kioku_hook.py): how many notes a prompt brings up, how often to nudge, the review's model and daily caps, and the scheduled dream. `KIOKU_HOOKS=off` turns every hook off.

## The scheduled dream

`dream` can run every week on its own. It needs Claude Code and the local Kioku server, so it runs on your computer, in one of two ways. Use one, not both.

1. **A desktop scheduled task (recommended).** In the Claude desktop app's Code tab, open Scheduled, then New task, and set it up manually. Under Advanced settings, turn on Only on this computer. Pick the area's folder as the local folder and a weekly trigger, with these instructions:

   ```
   /kioku-memory:dream <domain> scheduled
   ```

   The task runs while the app is open and the computer is awake. Try it once with Run now, and allow what it asks for, so later runs don't stop on a question. The first dream of a domain is always one you start yourself, and a scheduled run never retires a note: merges and replacements wait on the pending list.

2. **The hooks' session-end trigger,** a fallback that is off by default. With `"dream": {"enabled": true}` in `~/.config/kioku/hooks.json`, ending a session in an area starts a headless dream when one is due: the last successful dream is older than `every_days` (7), no run is in progress, and none failed in the last 24 hours. It runs `claude -p` with Opus, a budget cap, and only the tools and commands the dream needs. It finds the dream skill in the `kioku-memory` plugin Claude Code installed, or in `$KIOKU_SKILLS_DIR`.

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
