export const meta = {
  name: 'kioku-dream',
  description: "Improve a Kioku area's notes: group them by topic (in a scheduled run, only the topics whose notes changed since the last dream), propose rewrites, splits, links, code-path anchors and one hub note per topic, verify each change adversarially, save the verified changes to agent notes after a fresh read, and report merges, replacements, key collisions and file changes for the user",
  whenToUse: 'Run by the dream skill with args built from memory_list',
  phases: [
    { title: 'Plan', detail: 'one agent groups the notes into topics; a scheduled run keeps those with the most changed notes' },
    { title: 'Improve', detail: 'one agent per topic proposes changes' },
    { title: 'Verify', detail: 'an independent skeptic per change' },
    { title: 'Apply', detail: 'one agent re-reads each note and saves the verified changes to agent notes' },
    { title: 'Report', detail: 'one agent writes the report' },
  ],
}

// args: {
//   domain, vault,
//   startedAt: '2026-11-07T18:59:31Z',  when the skill started; a note changed after it is skipped, not overwritten
//   repos: [{ path, github }],          the area's repositories, for code-path anchors
//   notes: [{ key, title, tags, origin, links, paths, updated }],   from memory_list
//   noteFormat: "<text of the checkpoint skill's references/note-format.md>",
//   topic: 'name' | null,               keep only this topic
//   since: '<ISO time>' | null,         the last successful dream, from dream_state.py status
//   maxTopics: 2 | null,                keep the topics with the most notes changed since `since` (scheduled runs)
//   kioku: { bin, envFile } | null,     the Kioku CLI, for a file's purpose when the code_map tool isn't loaded
//   maxVerify: 30
// }
const a = args
const maxVerify = a.maxVerify || 30
const startedAt = a.startedAt || null
if (!startedAt) log('args.startedAt is missing: the apply agent checks only that notes exist, not when they changed')

let agents = 0
function run(prompt, options) {
  agents += 1
  return agent(prompt, options)
}

const clip = (text, n) => {
  const s = String(text || '').replace(/\s+/g, ' ').trim()
  return s.length <= n ? s : `${s.slice(0, n - 1)}…`
}

const PLAN = {
  type: 'object',
  properties: {
    topics: {
      type: 'array',
      items: {
        type: 'object',
        properties: { name: { type: 'string' }, keys: { type: 'array', items: { type: 'string' } } },
        required: ['name', 'keys'],
      },
    },
  },
  required: ['topics'],
}
const NOTE = {
  type: 'object',
  properties: {
    key: { type: 'string' },
    title: { type: 'string' },
    body: { type: 'string' },
    tags: { type: 'array', items: { type: 'string' } },
    aliases: { type: 'array', items: { type: 'string' } },
    related: { type: 'array', items: { type: 'string' } },
    paths: { type: 'array', items: { type: 'string' } },
    source: { type: 'string' },
  },
  required: ['key', 'title', 'body'],
}
const CHANGES = {
  type: 'object',
  properties: {
    changes: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          action: { type: 'string', enum: ['rewrite', 'split', 'link', 'anchor', 'hub', 'merge', 'replace'] },
          origin: { type: 'string', enum: ['agent', 'file', 'new'] },
          note: NOTE,
          also: { type: 'array', items: NOTE },
          retire: { type: 'array', items: { type: 'string' } },
          why: { type: 'string' },
          evidence: { type: 'string' },
        },
        required: ['action', 'origin', 'note', 'why', 'evidence'],
      },
    },
  },
  required: ['changes'],
}
const VERDICT = {
  type: 'object',
  properties: { real: { type: 'boolean' }, reason: { type: 'string' } },
  required: ['real', 'reason'],
}
const APPLIED = {
  type: 'object',
  properties: {
    results: {
      type: 'array',
      items: {
        type: 'object',
        properties: { key: { type: 'string' }, ok: { type: 'boolean' }, skipped: { type: 'boolean' }, detail: { type: 'string' } },
        required: ['key', 'ok'],
      },
    },
  },
  required: ['results'],
}

const area = `the Kioku domain ${a.domain} (vault ${a.vault})`
const repos = (a.repos || []).map(r => r.path).join(', ') || '(none listed)'
const KIOKU_READ = `Read notes with memory_get (domain ${a.domain}, at most 10 keys per call), passing "count_use": false, because improving notes isn't using them, and "max_bytes": 8000, so notes aren't cut at the default 2000 bytes. If Kioku answers that memory_get does not accept count_use (an older server), call it again without count_use. If a response says truncated, read fewer keys per call until it doesn't: never rewrite or judge a note from cut text. Load tools with ToolSearch ("select:mcp__kioku__memory_get,mcp__kioku__memory_search") if they aren't loaded.`
const CODEMAP = a.kioku && a.kioku.bin
  ? `the code_map tool (ToolSearch "select:mcp__kioku__code_map"), or where it isn't available, ${a.kioku.bin}${a.kioku.envFile ? ` --env-file ${a.kioku.envFile}` : ''} codemap show <file>`
  : 'the code_map tool (ToolSearch "select:mcp__kioku__code_map") where it is available'
const listing = (a.notes || []).map(n => `- ${n.key} (${n.origin}${n.links === 0 ? ', no links' : ''}${(n.paths || []).length ? `, paths ${n.paths.join(' ')}` : ''}): ${n.title} [${(n.tags || []).join(', ')}]`).join('\n')
const byKey = new Map((a.notes || []).map(n => [n.key, n]))
const isStored = key => byKey.has(key)
const isDecisionKey = key => typeof key === 'string' && (key.includes('decisions/') || key.includes('DEC-'))
const isDecision = key => isDecisionKey(key) || ((byKey.get(key) || {}).tags || []).map(t => String(t).toLowerCase()).includes('decision')

phase('Plan')
const planned = await run(`Group the notes of ${area} into topics, the parts of the project a reader would look for separately (for example "viewer", "hooks", "retrieval"). Use at most 8 topics; every key goes in exactly one topic; a hub note (tag hub) goes in the topic it covers.

${listing}`, { label: 'plan', phase: 'Plan', schema: PLAN })
const failedStages = planned ? [] : ['plan']
let topics = ((planned && planned.topics) || []).filter(t => t.keys && t.keys.length)
if (a.topic) topics = topics.filter(t => t.name.toLowerCase() === String(a.topic).toLowerCase())

// A scheduled run keeps the topics whose notes changed since the last dream: first by
// notes changed on a later day, then by notes changed that day (the last dream's own
// saves are among those), then by name.
const sinceDay = a.since ? String(a.since).slice(0, 10) : null
function changedIn(t) {
  const after = []
  const same = []
  for (const key of t.keys) {
    const updated = (byKey.get(key) || {}).updated || ''
    if (!sinceDay || updated > sinceDay) after.push(key)
    else if (updated === sinceDay) same.push(key)
  }
  return { after, same }
}
topics = topics.map(t => ({ ...t, changed: changedIn(t) }))
if (a.maxTopics) {
  const all = topics.length
  topics = topics
    .filter(t => t.changed.after.length + t.changed.same.length > 0)
    .sort((x, y) => (y.changed.after.length - x.changed.after.length) || (y.changed.same.length - x.changed.same.length) || x.name.localeCompare(y.name))
    .slice(0, a.maxTopics)
  log(`kept ${topics.length} of ${all} topics with notes changed since ${sinceDay || 'the start'}`)
}
log(`${topics.length} topics: ${topics.map(t => `${t.name} (${t.keys.length})`).join(', ')}`)

function improvePrompt(t) {
  return `Improve the "${t.name}" notes of ${area}: make them easier to find, read and trust, without adding knowledge they don't hold. ${KIOKU_READ}

Notes in this topic: ${t.keys.join(', ')}
All notes in the domain, for links:
${listing}

The area's repositories: ${repos}. Check code paths with git (git -C <repo> ls-files -- ':(glob)<glob>' lists the tracked files a glob matches), and a file's purpose with ${CODEMAP}.

Read every note in the topic, then propose changes of these kinds, only where one clearly helps:
- rewrite: a title that doesn't state the claim, missing aliases (IDs, old names, the words people search for), a body that is unclear or longer than it needs to be, a missing kind tag. Keep every fact and the source.
- split: a note that holds two facts. The note keeps one under its key; "also" holds the new notes, each linking back. Give each also note a new key that no stored note and no other change uses.
- link: a related link the note needs (what it builds on, narrows or replaces). Give the full related list.
- anchor: the code paths a decision, convention or gotcha governs, as globs relative to the repository root that match tracked files (src/vault/**, viewer/src/pages/vaults). Only code the note is truly about; never a path just because a file mentions it. A note about the project as a whole gets no anchor.
- hub: one note per topic, key "${a.domain.split('/').pop()}-<topic>-hub", tags ["hub", <topic>]: two to four sentences on what this part of the project is and the decisions in force, with related listing those notes (up to 12). Create it if it's missing, rewrite it if it is out of date.
- merge: two notes that say the same thing. "note" is the merged note; "retire" lists the duplicates.
- replace: a decision a newer one replaced without saying so. "note" is the newer decision with "Replaces <key> (<date>): <why>" in its body; "retire" lists the old one.
Give one change per note: when a note needs a rewrite, a link and an anchor, make them one change under its main action.
A decision note (tag decision, or a key with decisions/ or DEC-) may be reworded, never changed in what it decides: its choice, scope, reason, rejected options and consequence stay as stored.
For every change give "note": the key, title and body, plus only the fields the change sets (tags, related or paths, as full lists, and source); leave out the fields you don't change, so Kioku keeps them. Aliases only grow: on a stored note give just the aliases to add (memory_get returns its current ones; don't repeat them), and the apply step keeps every stored alias; on a new note give its full list. Also give "origin" (agent, file, or new for a new note); "why" in one sentence; and "evidence": the stored text, and for anchors the files the globs match. Nothing that describes what code does, no secrets, no new facts. Notes of origin file can be proposed but are never saved by you.

Note format:
${a.noteFormat}`
}

function verifyPrompt(c) {
  const alsoText = c.action === 'split' && (c.also || []).length
    ? `\nThis change splits ${c.note.key}: check each "also" note too. Each holds only facts of the note it splits off from, links back to it, has a key that fits the note format, and is a new note: memory_get must find no note with its key.`
    : ''
  const keys = [c.note.key, ...(c.retire || [])]
  const decisionText = keys.some(isDecision)
    ? `\nThis change touches a decision note. Set real=false if it alters what the decision decides: the choice, its scope, the reason, the rejected options or the consequence. Rewording, a clearer title, links and anchors are fine.`
    : ''
  return `Try to refute this proposed change to the notes of ${area}:
${JSON.stringify(c, null, 1)}

${KIOKU_READ} Read the stored notes it touches and compare. For anchors, run git -C <repo> ls-files -- ':(glob)<glob>' in ${repos} for each glob.
Set real=false unless all of these hold: every fact of the stored notes survives; nothing new is claimed that the notes don't hold; the result is clearer or better connected; each anchor matches tracked files and the note is truly about that code; a hub states only decisions its linked notes hold; keys and domains fit the note format; no other note already does this.${alsoText}${decisionText}
When unsure, set real=false. Give your reason in two sentences or fewer.`
}

let changes = []
const failedTopics = []
if (topics.length) {
  phase('Improve')
  const proposed = await pipeline(
    topics,
    t => run(improvePrompt(t), { label: `improve:${t.name}`, phase: 'Improve', schema: CHANGES })
      .then(r => (r ? (r.changes || []).map(c => ({ ...c, topic: t.name })) : null)),
  )
  proposed.forEach((r, i) => { if (!r) failedTopics.push(topics[i].name) })
  changes = proposed.filter(Boolean).flat().filter(c => c && c.note && c.note.key)
}
const toVerify = changes.slice(0, maxVerify)
const overCap = changes.slice(maxVerify)
if (overCap.length) log(`${changes.length} changes; verifying ${toVerify.length}, reporting ${overCap.length} unverified`)

let verified = []
let refuted = []
let unchecked = []
if (toVerify.length) {
  phase('Verify')
  const verdicts = await parallel(toVerify.map(c => () =>
    run(verifyPrompt(c), { label: `verify:${c.action}:${c.note.key}`, phase: 'Verify', schema: VERDICT, model: 'sonnet', effort: 'high' })
      .then(v => ({ ...c, verdict: v }))))
  unchecked = toVerify.filter((c, i) => !verdicts[i] || !verdicts[i].verdict)
  verified = verdicts.filter(v => v && v.verdict && v.verdict.real)
  refuted = verdicts.filter(v => v && v.verdict && !v.verdict.real)
}
const unverified = [...unchecked, ...overCap]

// The write policy and the key checks, in plain code. Rewrites, splits, links, anchors
// and hubs on agent notes go live; merges, replacements, anything on a file-backed note,
// and any change whose keys collide wait for the user.
const liveActions = ['rewrite', 'split', 'link', 'anchor', 'hub']
const alsoKeys = c => (c.action === 'split' ? (c.also || []).map(n => n && n.key).filter(Boolean) : [])
const primaryCount = new Map()
const alsoCount = new Map()
for (const c of verified) {
  primaryCount.set(c.note.key, (primaryCount.get(c.note.key) || 0) + 1)
  for (const key of alsoKeys(c)) alsoCount.set(key, (alsoCount.get(key) || 0) + 1)
}
function collision(c) {
  for (const key of alsoKeys(c)) {
    if (isStored(key)) return `split note key ${key} is already a stored note`
    if (key === c.note.key) return `split note key ${key} is the key of the note it splits`
    if (primaryCount.has(key)) return `split note key ${key} is a note another change writes`
    if (alsoCount.get(key) > 1) return `split note key ${key} is used by two new notes`
  }
  if (c.action !== 'hub' && !isStored(c.note.key)) return `${c.note.key} is not a stored note`
  return null
}
function waitReason(c, seen) {
  if (c.origin === 'file') return ['kb', 'it changes a note backed by a file']
  if (c.action === 'merge') return ['retire', `it retires ${(c.retire || []).join(', ') || 'a note'}`]
  if (c.action === 'replace') return ['retire', `it replaces and retires ${(c.retire || []).join(', ') || 'a decision'}`]
  if (!liveActions.includes(c.action)) return ['other', `action ${c.action} is never saved live`]
  const clash = collision(c)
  if (clash) return ['other', clash]
  if (seen.has(c.note.key)) return ['other', `another change in this run writes ${c.note.key}`]
  return null
}
const live = []
const needsUser = []
const seen = new Set()
for (const c of verified) {
  const reason = waitReason(c, seen)
  if (!reason) {
    live.push(c)
    seen.add(c.note.key)
    continue
  }
  const [kind, why] = reason
  const what = c.action === 'merge' ? `Merge into ${c.note.key}` : c.action === 'replace' ? `Replace ${(c.retire || []).join(', ')} with ${c.note.key}` : `${c.action} of ${c.note.key}`
  const retired = (c.retire || []).slice().sort().join(',')
  needsUser.push({ ...c, wait_reason: why, pending: { kind, id: `dream:${c.action}:${a.domain}/${c.note.key}${retired ? `:${retired}` : ''}`, text: clip(`${what}: waits because ${why}. ${c.why || ''}`, 240) } })
}

// What the apply agent saves. A link or an anchor keeps the stored title and text, read
// fresh; other changes save their own. Only the fields a change sets are sent, so Kioku
// keeps the rest. A list of aliases replaces the stored one, so a stored note gets
// add_aliases: the apply agent saves the stored aliases plus these, never dropping one.
function applyItem(note, change, isNew) {
  const item = { key: note.key, expect: isNew ? 'new' : 'stored', not_changed_since: startedAt }
  if (!isNew && (change.action === 'link' || change.action === 'anchor')) {
    item.keep_text = true
    for (const field of ['related', 'paths']) if (Array.isArray(note[field])) item[field] = note[field]
    return item
  }
  Object.assign(item, { title: note.title, body: note.body })
  for (const field of ['tags', 'related', 'paths', 'source']) if (note[field] !== undefined && note[field] !== null) item[field] = note[field]
  if ((note.aliases || []).length) {
    if (isNew) item.aliases = note.aliases
    else item.add_aliases = note.aliases
  }
  return item
}
const items = live.flatMap(c => [
  applyItem(c.note, c, !isStored(c.note.key)),
  ...(c.action === 'split' ? (c.also || []).filter(n => n && n.key).map(n => applyItem(n, c, true)) : []),
])

let applied = []
if (items.length) {
  phase('Apply')
  const result = await run(`Save these verified notes to Kioku with memory_save, one at a time, in the order given, all with domain ${a.domain}. Load the tools with ToolSearch ("select:mcp__kioku__memory_save,mcp__kioku__memory_get,mcp__kioku__memory_inspect") if they aren't loaded.

Read each note fresh right before saving it, because another session may have saved it since this run read it:
1. Call memory_get with {"keys": [<key>], "domain": "${a.domain}", "count_use": false, "max_bytes": 8000}. If Kioku answers that memory_get does not accept count_use (an older server), call it again without count_use.
2. expect "new": if memory_get returns the note, don't save it: report it skipped, "exists". If it says no such memory, save it.
3. expect "stored": if memory_get says no such memory, skip it ("missing"). ${startedAt ? 'If the note\'s "updated" date is on or after the date of not_changed_since, call memory_inspect with its key and domain: if its updated_at is later than not_changed_since, skip it ("changed at <updated_at>, after this run read it"). If memory_inspect isn\'t available, skip it when the updated date is later than that date.' : ''}
4. keep_text: if the memory_get response says truncated, skip it ("truncated"). Otherwise save its title and its text exactly as memory_get returned them, as title and body, with the item's related or paths list and nothing else.
5. Otherwise save exactly the fields the item has: key, title, body, and whichever of tags, aliases, related, paths and source it lists. Leave out every field it doesn't list, so Kioku keeps the stored value.
   add_aliases: the note in step 1's memory_get response lists its stored "aliases". Send aliases as that list followed by each add_aliases entry it doesn't already hold (compared case-insensitively), so no stored alias is dropped. If the response has no "aliases" field (an older Kioku), send no aliases and write "aliases not saved" in detail.
6. If a save's response lists unresolved_related, save that note again without those entries.
Report every note: ok true when it was saved; skipped true with the reason in detail when you skipped it; ok false with the error text otherwise.

${JSON.stringify(items, null, 1)}`, { label: 'apply', phase: 'Apply', schema: APPLIED })
  if (!result) failedStages.push('apply')
  applied = (result && result.results) || []
}
const outcome = new Map(applied.map(r => [r.key, r]))
const aliasesNotSaved = items
  .filter(i => i.add_aliases && /aliases not saved/i.test((outcome.get(i.key) || {}).detail || ''))
  .map(i => ({ key: i.key, aliases: i.add_aliases }))
const savedResults = applied.filter(r => r.ok)
const skipped = items.filter(i => { const r = outcome.get(i.key); return r && !r.ok && r.skipped })
  .map(i => {
    const r = outcome.get(i.key)
    return { ...i, skip_reason: r.detail || 'skipped', pending: { kind: 'other', id: `dream:skipped:${a.domain}/${i.key}:${String(startedAt || '').slice(0, 10)}`, text: clip(`Dream change to ${i.key} was not saved: ${r.detail || 'skipped'}. Compare it with the stored note.`, 240) } }
  })
const failedSaves = items.filter(i => { const r = outcome.get(i.key); return !r || (!r.ok && !r.skipped) })
  .map(i => { const r = outcome.get(i.key); return { key: i.key, detail: (r && r.detail) || 'the apply agent did not report it' } })

const topicText = topics.map(t => `${t.name} (${t.keys.length} notes, ${t.changed.after.length + t.changed.same.length} changed since the last dream)`).join(', ')
let report
if (!changes.length && !failedStages.length && !failedTopics.length) {
  report = topics.length
    ? `Dream of ${area}: no changes proposed. Topics checked: ${topicText}.`
    : `Dream of ${area}: no topic ${a.maxTopics ? `has notes changed since ${sinceDay || 'the last dream'}` : 'to check'}, so nothing was proposed.`
} else {
  phase('Report')
  report = await run(`Write the dream report for ${area}, in plain Markdown with no preamble. Be brief: one bullet per item.

Saved: ${JSON.stringify(savedResults, null, 1)}
The changes they came from: ${JSON.stringify(live.map(c => ({ action: c.action, key: c.note.key, why: c.why, also: alsoKeys(c) })), null, 1)}
Skipped at save time (the note changed during the run, or already existed; filed for the user): ${JSON.stringify(skipped.map(i => ({ key: i.key, reason: i.skip_reason })), null, 1)}
Failed saves: ${JSON.stringify(failedSaves, null, 1)}
Needs the user (each with why it waits): ${JSON.stringify(needsUser.map(c => ({ action: c.action, origin: c.origin, key: c.note.key, also: alsoKeys(c), retire: c.retire, waits: c.wait_reason, why: c.why, note: c.note })), null, 1)}
Unverified: ${JSON.stringify(unverified.map(c => ({ action: c.action, key: c.note.key, why: c.why })), null, 1)}
Aliases proposed for stored notes, not saved (no read shows their stored aliases): ${JSON.stringify(aliasesNotSaved)}
Refuted: ${refuted.length}. Topics: ${topicText || 'none'}.${failedTopics.length ? ` Topics whose improving agent failed: ${failedTopics.join(', ')}.` : ''}${failedStages.length ? ` Stages that failed: ${failedStages.join(', ')}.` : ''}

Sections, in this order, leaving out empty ones:
1. Saved: grouped by topic, each note's key and what changed (rewritten, split into, linked, anchored to which paths, hub). Flag any save that failed or was skipped, and why.
2. Needs your decision: merges and replacements (what would be retired, and why), key collisions, and changes to file-backed notes (the file and the change), each with why it waits.
3. Unverified: one line each.
4. What was checked: topics, notes, and how many changes were proposed, verified and refuted, and any stage or topic that failed.`, { label: 'report', phase: 'Report' })
}

return {
  report,
  started_at: startedAt,
  agents,
  scheduled: !!a.maxTopics,
  topics: topics.map(t => ({ name: t.name, notes: t.keys.length, changed: t.changed.after.length + t.changed.same.length })),
  saved: savedResults.map(r => r.key),
  skipped,
  failed_saves: failedSaves,
  needs_user: needsUser,
  unverified,
  aliases_not_saved: aliasesNotSaved,
  failed_stages: failedStages,
  failed_topics: failedTopics,
  counts: { topics: topics.length, notes: (a.notes || []).length, changes: changes.length, verified: verified.length, refuted: refuted.length, needs_user: needsUser.length, saved: savedResults.length, skipped: skipped.length, unverified: unverified.length },
}
