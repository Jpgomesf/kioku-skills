// Tests for workflow.js. Run: node --test tests/
// The harness lives with project-sync's tests, which load that skill's workflow the same way.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { loadWorkflow, stubs, itemsOf } from '../../project-sync/tests/workflow_harness.mjs'

const here = dirname(fileURLToPath(import.meta.url))
const workflow = loadWorkflow(join(here, '..', 'workflow.js'))
const DOMAIN = 'personal/demo'
const STARTED = '2026-11-07T20:00:00Z'

const NOTES = [
  { key: 'auth-ttl', title: 'Tokens live one hour', tags: ['decision'], origin: 'agent', links: 1, updated: '2026-11-07' },
  { key: 'b-note', title: 'B', tags: ['gotcha'], origin: 'agent', links: 0, updated: '2026-11-01' },
  { key: 'c-note', title: 'C', tags: ['convention'], origin: 'agent', links: 2, updated: '2026-10-01' },
  { key: 'd-note', title: 'D', tags: ['fix'], origin: 'agent', links: 1, updated: '2026-11-05' },
  { key: 'kb-fact', title: 'From the KB', tags: [], origin: 'file', links: 3, updated: '2026-09-01' },
]

function baseArgs(extra = {}) {
  return {
    domain: DOMAIN, vault: 'personal', startedAt: STARTED, repos: [{ path: '/r/demo', github: 'o/demo' }],
    notes: NOTES, noteFormat: '(note format)', topic: null, since: null, maxTopics: null,
    kioku: { bin: '/k/bin/kioku', envFile: '/k/.env' }, maxVerify: 30,
    ...extra,
  }
}

const note = (key, extra = {}) => ({ key, title: `Title of ${key}`, body: `Body of ${key}.`, ...extra })
const change = (action, key, extra = {}) => ({
  action, origin: 'agent', note: note(key), why: `${action} ${key}`, evidence: 'the stored text', ...extra,
})

function responder({ topics, changes = {}, verdict, apply } = {}) {
  return call => {
    const { label, prompt } = call
    if (label === 'plan') return { topics: topics || [{ name: 'core', keys: NOTES.map(n => n.key) }] }
    if (label.startsWith('improve:')) return { changes: changes[label.slice('improve:'.length)] || [] }
    if (label.startsWith('verify:')) return verdict ? verdict(call) : { real: true, reason: 'every fact survives' }
    if (label === 'apply') return apply ? apply(call) : { results: itemsOf(prompt).map(i => ({ key: i.key, ok: true })) }
    if (label === 'report') return 'the report'
    throw new Error(`unexpected agent ${label}`)
  }
}

test('meta is a pure literal with the phases the script uses', () => {
  assert.equal(workflow.meta.name, 'kioku-dream')
  assert.deepEqual(workflow.meta.phases.map(p => p.title), ['Plan', 'Improve', 'Verify', 'Apply', 'Report'])
})

test('split keys that collide wait for the user, as do merges, file notes and second changes', async () => {
  const s = stubs(baseArgs(), responder({
    changes: {
      core: [
        change('rewrite', 'auth-ttl'),
        change('split', 'b-note', { also: [note('c-note')] }),
        change('split', 'd-note', { also: [note('d-part', { related: ['d-note'], aliases: ['part of d'] })] }),
        change('split', 'c-note', { also: [note('twice'), note('twice')] }),
        change('anchor', 'auth-ttl', { note: note('auth-ttl', { paths: ['src/auth/**'] }) }),
        change('merge', 'b-note', { retire: ['c-note'] }),
        change('rewrite', 'kb-fact', { origin: 'file' }),
        change('link', 'ghost-note', { note: note('ghost-note', { related: ['auth-ttl'] }) }),
      ],
    },
  }))
  const result = await workflow.run(s)
  const waiting = Object.fromEntries(result.needs_user.map(c => [`${c.action} ${c.note.key}`, [c.pending.kind, c.wait_reason]]))
  assert.deepEqual(waiting, {
    'split b-note': ['other', 'split note key c-note is already a stored note'],
    'split c-note': ['other', 'split note key twice is used by two new notes'],
    'anchor auth-ttl': ['other', 'another change in this run writes auth-ttl'],
    'merge b-note': ['retire', 'it retires c-note'],
    'rewrite kb-fact': ['kb', 'it changes a note backed by a file'],
    'link ghost-note': ['other', 'ghost-note is not a stored note'],
  })
  assert.equal(result.needs_user.find(c => c.action === 'merge').pending.id, `dream:merge:${DOMAIN}/b-note:c-note`)
  const items = itemsOf(s.prompt('apply'))
  assert.deepEqual(items.map(i => [i.key, i.expect]), [['auth-ttl', 'stored'], ['d-note', 'stored'], ['d-part', 'new']])
  assert.deepEqual(items[2].aliases, ['part of d'], 'a new note keeps its aliases')
  for (const item of items) assert.equal(item.not_changed_since, STARTED)
})

test('a split key used by another change collides', async () => {
  const s = stubs(baseArgs(), responder({
    changes: { core: [change('split', 'b-note', { also: [note('new-hub')] }), change('hub', 'new-hub', { origin: 'new' })] },
  }))
  const result = await workflow.run(s)
  assert.deepEqual(result.needs_user.map(c => [c.note.key, c.wait_reason]),
    [['b-note', 'split note key new-hub is a note another change writes']])
  assert.deepEqual(itemsOf(s.prompt('apply')).map(i => [i.key, i.expect]), [['new-hub', 'new']])
})

test('the verifier checks split notes, and rejects a change to what a decision decides', async () => {
  const s = stubs(baseArgs(), responder({
    changes: { core: [change('rewrite', 'auth-ttl'), change('split', 'd-note', { also: [note('d-part')] }), change('rewrite', 'b-note')] },
  }))
  const result = await workflow.run(s)
  const decision = s.prompt('verify:rewrite:auth-ttl')
  assert.match(decision, /Set real=false if it alters what the decision decides/)
  assert.doesNotMatch(s.prompt('verify:rewrite:b-note'), /alters what the decision decides/)
  assert.match(s.prompt('verify:split:d-note'), /check each "also" note too/)
  assert.match(s.prompt('verify:split:d-note'), /memory_get must find no note with its key/)
  for (const label of s.labels('verify:')) {
    assert.match(s.prompt(label), /"count_use": false/)
    assert.match(s.prompt(label), /"max_bytes": 8000/)
  }
  assert.deepEqual(result.saved, ['auth-ttl', 'd-note', 'd-part', 'b-note'], 'decision rewrites stay live')
})

test('links and anchors keep the stored text; aliases on stored notes are added to the stored ones', async () => {
  const s = stubs(baseArgs(), responder({
    changes: {
      core: [
        change('anchor', 'd-note', { note: note('d-note', { paths: ['src/d/**'] }) }),
        change('link', 'c-note', { note: note('c-note', { related: ['d-note'] }) }),
        change('rewrite', 'b-note', { note: note('b-note', { aliases: ['bee'], tags: ['gotcha', 'auth'] }) }),
      ],
    },
  }))
  const result = await workflow.run(s)
  const [anchor, link, rewrite] = itemsOf(s.prompt('apply'))
  assert.deepEqual(anchor, { key: 'd-note', expect: 'stored', not_changed_since: STARTED, keep_text: true, paths: ['src/d/**'] })
  assert.deepEqual(link, { key: 'c-note', expect: 'stored', not_changed_since: STARTED, keep_text: true, related: ['d-note'] })
  assert.equal(rewrite.aliases, undefined, 'a stored note never gets a replacing list')
  assert.deepEqual(rewrite.add_aliases, ['bee'])
  assert.deepEqual(rewrite.tags, ['gotcha', 'auth'])
  assert.deepEqual(result.aliases_not_saved, [], 'saved on a server that returns aliases')
  assert.match(s.prompt('apply'), /call memory_inspect/)
  assert.match(s.prompt('apply'), /no stored alias is dropped/)
})

test('aliases the apply agent could not merge, on an older server, are reported', async () => {
  const s = stubs(baseArgs(), responder({
    changes: { core: [change('rewrite', 'b-note', { note: note('b-note', { aliases: ['bee'] }) })] },
    apply: () => ({ results: [{ key: 'b-note', ok: true, detail: 'aliases not saved' }] }),
  }))
  const result = await workflow.run(s)
  assert.deepEqual(result.aliases_not_saved, [{ key: 'b-note', aliases: ['bee'] }])
  assert.deepEqual(result.saved, ['b-note'])
})

test('changes are verified one topic at a time, so a cap reaches every topic', async () => {
  const s = stubs(baseArgs({ maxVerify: 2 }), responder({
    topics: [{ name: 'first', keys: ['auth-ttl', 'b-note', 'c-note'] }, { name: 'second', keys: ['d-note'] }],
    changes: {
      first: [change('rewrite', 'auth-ttl'), change('rewrite', 'b-note'), change('rewrite', 'c-note')],
      second: [change('rewrite', 'd-note')],
    },
  }))
  const result = await workflow.run(s)
  assert.deepEqual(s.labels('verify:'), ['verify:rewrite:auth-ttl', 'verify:rewrite:d-note'])
  assert.deepEqual(result.unverified.map(c => c.note.key), ['b-note', 'c-note'])
})

test('the improve prompt asks for every added alias in the note itself', async () => {
  const s = stubs(baseArgs(), responder({ changes: { core: [change('rewrite', 'b-note')] } }))
  await workflow.run(s)
  assert.match(s.prompt('improve:core'), /an alias named only in why or evidence is never saved/)
})

test('a scheduled run keeps the topics with the most notes changed since the last dream', async () => {
  const topics = [
    { name: 'old', keys: ['c-note', 'kb-fact'] },
    { name: 'earlier', keys: ['b-note'] },
    { name: 'latest', keys: ['auth-ttl'] },
    { name: 'same-day', keys: ['d-note'] },
  ]
  const one = stubs(baseArgs({ since: '2026-11-05T10:00:00Z', maxTopics: 1 }), responder({ topics }))
  const first = await workflow.run(one)
  assert.deepEqual(one.labels('improve:'), ['improve:latest'])
  assert.equal(first.scheduled, true)
  const two = stubs(baseArgs({ since: '2026-11-05T10:00:00Z', maxTopics: 3 }), responder({ topics }))
  await workflow.run(two)
  assert.deepEqual(two.labels('improve:').sort(), ['improve:latest', 'improve:same-day'])
})

test('with no topic changed, a scheduled run proposes nothing and runs only the planner', async () => {
  const s = stubs(baseArgs({ since: '2026-11-08T00:00:00Z', maxTopics: 2 }), responder())
  const result = await workflow.run(s)
  assert.deepEqual(s.labels(''), ['plan'])
  assert.match(result.report, /no topic has notes changed since 2026-11-08/)
  assert.deepEqual(result.failed_stages, [])
  assert.equal(result.agents, 1)
})

test('a failed planner is reported as a failed stage', async () => {
  const s = stubs(baseArgs(), call => (call.label === 'plan' ? null : 'the report'))
  const result = await workflow.run(s)
  assert.deepEqual(result.failed_stages, ['plan'])
})

test('the code map comes from the code_map tool, or the Kioku CLI given in args', async () => {
  const withCli = stubs(baseArgs(), responder())
  await workflow.run(withCli)
  assert.match(withCli.prompt('improve:core'), /code_map tool .*or where it isn't available, \/k\/bin\/kioku --env-file \/k\/.env codemap show <file>/)
  const toolOnly = stubs(baseArgs({ kioku: null }), responder())
  await workflow.run(toolOnly)
  assert.doesNotMatch(toolOnly.prompt('improve:core'), /codemap show/)
})
