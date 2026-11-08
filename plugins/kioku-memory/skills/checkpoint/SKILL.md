---
name: checkpoint
description: Checkpoint one session into Kioku. Look back over the conversation and save what a future session will need and can't get from the code, namely decisions the user confirmed (with reasons and rejected options), verified fixes and their root cause, gotchas, conventions and lasting preferences, updating existing notes instead of duplicating them. Use when the user asks for a checkpoint, before they compact or end a session, or when they say remember this. To weigh a decision now, use decide; to audit what is stored, reflect; to fix stale notes, memory-refresh.
argument-hint: "[optional focus, e.g. the router decision]"
---

# Checkpoint

Save the few things from this session that the next session needs and cannot rediscover by reading the code. Usually that is zero to three notes. A session with nothing worth saving is a normal outcome.

## 1. Collect candidates

Look back over this conversation, or over the summary if it was compacted, for:

- **Decisions the user confirmed**, with the reason and the options rejected. "Let's go with B because A breaks X" counts; your own proposal that wasn't confirmed doesn't.
- **Fixes you verified** (a test or command passed) and their root cause.
- **Gotchas:** behavior that surprised you and would surprise the next session.
- **Conventions and preferences** the user stated or corrected you on ("no, use X").
- **Environment facts** about this machine or setup that aren't written anywhere else.

Drop:
- anything the code, docs or git history already show;
- progress and to-do state;
- transcripts and summaries of the conversation;
- guesses and unconfirmed plans;
- secrets of any kind.

## 2. Find each one's home

- **Read [references/note-format.md](references/note-format.md) before the first save.** It covers which domain a note goes in, the shape that makes a note findable and linked, how a decision replaces another, retiring, and the pending list.
- **Search first.** Run `memory_search` with the candidate's subject and IDs, then `memory_get` the best one or two hits and follow their links one hop.
  - Read with `"count_use": false`, since comparing isn't use. If Kioku answers that it doesn't accept `count_use` (an older server), repeat the call without it.
  - A reply stops at 2000 bytes by default. When it says `truncated`, pass `max_bytes` up to 8000.
- **What the search finds decides the write:**
  - **It's there and still true:** skip it.
  - **It's there but outdated,** and isn't a decision: update it with the same key.
  - **It changes a stored decision:** a decision's text changes only to say the same decision more clearly. Changing the decision itself means a new note that replaces it, as the note format says.
  - **It's not there:** create it, linked to the notes it relates to.

## 3. Write

- One fact per note, shaped as the note format says, with `related` links so it joins the graph.
- Use `source` "user" for decisions the user made in this conversation.
- Check `unresolved_related` in each save's response and fix what it lists.
- If you're unsure the user confirmed something, ask one short question rather than saving.
- Ask before retiring a replaced note, and retire it right after the user confirms (note format).

## 4. Report

- In two or three lines, list the keys you saved or updated, and anything you held back and why. Report a save only after `memory_save` succeeded. If nothing qualified, say so in one line.
- Put what's left for the user, such as an unanswered question, a retirement or a KB change, on the pending list. Mark done the items this session settled (note format).
