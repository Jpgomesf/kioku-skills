# Saving notes Kioku can find and connect

Kioku finds a note in four ways:
- **Exact match.** A query equal to a key, title or alias ranks first.
- **Keywords.** BM25 over the key, title, aliases, tags and body.
- **Meaning.** Vector search over the same text.
- **The link graph.** It fills the last result slots with notes linked to the top hits.

Write every note so all four work. A note with no links can never be reached through the graph.

## The vault's rules

A vault can have its own rules in `~/.config/kioku/rules/<vault>.md`, outside every repository. Read them when the file exists. They add to this format, for example:
- which domains mirror a knowledge base, and its topic tags;
- an external tracker, such as Linear, that the knowledge base's decisions mirror;
- the branch each repository's decisions are checked against.

## Where it goes

- **An update** stays in the domain of the note it updates.
- **A new note** goes in the session's area, as Kioku's instructions name it for saves (the vault's root domain when the session has no area).
- **The exception is a domain that mirrors a knowledge base,** as the vault's rules name it: notes about that knowledge base go in its domain. Agent notes there link to the KB's decisions by ID (`related: [DEC-12]`), and re-imports of the bundle leave them alone. Never reuse a KB key: those belong to files.
- **Links resolve only within one domain,** so put a note where the notes it relates to live.

## Shape

| Field | Rule | Example |
|---|---|---|
| key | Stable kebab-case, subject first. Reuse it to update. | `refund-retries-use-stored-amount` |
| title | The claim, specific enough to answer a question on its own | Refund retries charge the amount stored at request time, not the current order total |
| aliases | Every ID and name people will search with, including the user's own words | `ADR-12`, `PAY-381`, `payment.refund_requested` |
| tags | One kind (`decision`, `convention`, `gotcha`, `fix`, `preference`, `setup`), plus the project's topic tags | `decision`, `refunds` |
| body | One to three sentences under 600 characters: the fact, why it holds, where it applies. A decision adds the rejected options and the consequence. | Chose X over Y because Z; this means W. |
| related | 1–3 keys or IDs a reader needs next, in the same domain: the decisions it implements, narrows or replaces, or the note it refines | `[ADR-9, ADR-12]` |

- **Topic tags.** When the vault's rules list a knowledge base's topic tags, use them, so `reflect` groups the note with the KB's decisions.
- **After saving,** read `unresolved_related` in the response and fix every entry it lists.

## Lifecycle

- **A fact changes:** save with the same key. The old text can't be read back: `memory_inspect` lists past revisions by title and date only, and no tool reads or restores one.
- **A decision's text** changes only to say the same decision more clearly, as dream's rewrites do: each is checked to keep every fact. Changing the decision itself means a new note that replaces it, as below.
- **A decision replaces an agent note:**
  1. In the new note's body, write "Replaces <key> (<date>): <why>".
  2. Link the old note.
  3. Retire the old note with `memory_retire`, once the user confirms.
- **A decision replaces or narrows a KB decision**, which is backed by a file:
  1. Don't retire the KB note. Kioku won't, and the KB is the record.
  2. Write "Narrows DEC-xxx" or "Replaces DEC-xxx" in the new note, and link it.
  3. The KB change follows memory-refresh's "A KB decision changes": the external tracker first when the vault's rules name one, then the KB repository, then a re-import.
- **Retiring a note always needs the user's confirmation,** in every skill. checkpoint and decide retire right after it.
- **Left for the user:** whatever a skill leaves for the user to decide (a retirement, a KB change, a decision, a claim it couldn't verify) goes on Kioku's pending list, under the session's vault and the item's domain.
  - List first, so nothing is added twice. Mark an item done when the user settles it.
  - Workflow agents only report such items; their skill adds them.
  - `P` is `kioku_pending.py` in the `scripts` folder beside this file's folder (`../scripts/kioku_pending.py` from here). The list lives in `~/.local/state/kioku/pending/<vault>.json`.
  ```sh
  python3 -I $P list --vault <vault> --domain <domain>
  python3 -I $P add --vault <vault> --domain <domain> --kind <retire|kb|decision|unverified|regression|other> \
    --source "<skill> <YYYY-MM-DD>" --text "<one line>" [--detail-file <JSON file>]   # prints the new id
  python3 -I $P done --vault <vault> <id> --resolution "<how it was settled>"
  ```
- **Never save:** descriptions of code, progress, transcripts, unconfirmed plans or secrets.
