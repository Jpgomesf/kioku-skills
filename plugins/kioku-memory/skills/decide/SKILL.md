---
name: decide
description: Orient one decision with Kioku before making it, and record it after the user confirms so the decision graph stays consistent. Before proposing, it pulls the stored decisions on the subject and their linked decisions, says whether the question is already settled, constrained or contested, and checks the code where a decision names it. Use when a design, architecture, data-model, tooling or convention choice comes up, when the user weighs options, or when new work seems to contradict a stored decision. To save a whole session, use checkpoint; to audit stored decisions, reflect.
argument-hint: "[the question, e.g. which amount refund retries charge]"
---

# Decide

A decision made without the earlier ones in view tends to contradict them quietly, which is what `reflect` later has to untangle. Orient first, decide with the user, then record the decision so the graph holds it.

## 1. Orient, before proposing anything

1. **Find the stored decisions.** Run `memory_search` on the subject, its IDs (ticket, decision or PR numbers) and the user's own words. Then `memory_get` the one or two best hits.
   - Read with `"count_use": false`, since orienting isn't use. If Kioku answers that it doesn't accept `count_use` (an older server), repeat the call without it.
   - A reply stops at 2000 bytes by default. When it says `truncated`, pass `max_bytes` up to 8000.
   - List the domain's pending items (the note format in step 3 has the command). An open one on the subject is a question already waiting for the user.
   - Read the vault's rules, `~/.config/kioku/rules/<vault>.md`, when the file exists.
2. **Walk one hop.** `memory_get` the decisions the hits link to, read the same way. Decision keys contain `decisions/` or `DEC-`, and agent notes carry the `decision` tag. The hits and their linked decisions are the neighbourhood that applies.
3. **Check the code** when a decision names it (a file, table, column, constant or queue). Use the branch the project works on, which the vault's rules may name. A decision the code contradicts is stale or unbuilt: say which. The fix belongs to `memory-refresh`, after this decision.
4. **Classify the question:**

| Class | Meaning | What to do |
|---|---|---|
| Settled | A decision in force covers it | Follow it and cite it. Only reopen it if the user wants to. |
| Constrained | Decisions in force limit the options | Name them, and drop the options they rule out |
| Contested | Stored decisions disagree with each other, or with the code | Show the conflict before going further |
| Open | Nothing stored applies | Say so; the decision will be new |

## 2. Present

- Give the options, each with the stored decisions it relies on or would replace, cited by key or ID.
- Recommend one option, and say what it would replace or narrow.
- Keep it short. The user decides.

## 3. Record, after the user confirms

Follow the note format, `references/note-format.md` in the checkpoint skill (`../checkpoint/references/note-format.md` from this skill's folder), for the domain, key, aliases, tags, links, retiring, the pending list and the vault's rules.

- **One decision per note,** in a compact ADR shape: context in one sentence, then the decision, the options rejected and why, and the consequence.
- **`related`:** the decisions it builds on, narrows or replaces, by key or ID, so it joins the graph next to them.
- **Replacing an agent note:** write "Replaces <key> (<date>)" in the body. Ask to retire the old note, and retire it right after the user confirms.
- **Replacing or narrowing a KB decision** (backed by a file): write "Narrows DEC-xxx" or "Replaces DEC-xxx", and don't retire the KB note. The KB's change follows memory-refresh's "A KB decision changes": the external tracker first when the vault's rules name one, then the KB repository, then a re-import.
- **Don't record** proposals the user didn't confirm, or anything the code shows.
- **Report** the key you saved in one line. Put what's left for the user on the pending list, such as a `kb` item for the KB's change, and mark done any item this decision settled.
