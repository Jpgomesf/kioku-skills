// Tests for workflow.js. Run: node --test tests/
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { loadWorkflow, stubs, itemsOf } from './workflow_harness.mjs'

const here = dirname(fileURLToPath(import.meta.url))
const workflow = loadWorkflow(join(here, '..', 'workflow.js'))
const DOMAIN = 'personal/demo'
const STARTED = '2026-11-07T20:00:00Z'

function baseArgs(extra = {}) {
  return {
    domain: DOMAIN, vault: 'personal', startedAt: STARTED,
    window: { since: '2026-11-01T00:00:00Z', until: '2026-11-07T19:59:00Z', first_run: false },
    inventory: '/tmp/inventory.json',
    batches: [{ id: 'commits-demo-1', kind: 'commits', repo: '/r/demo', items: 3, index: 0 }],
    slack: [], linear: null, docs: [], codemap: {},
    notes: [
      { key: 'auth-token-ttl', title: 'Tokens live one hour', tags: ['decision', 'auth'], origin: 'agent' },
      { key: 'deploy-steps', title: 'Deploys go through CI', tags: ['convention'], origin: 'agent' },
      { key: 'kb-note', title: 'A knowledge-base fact', tags: [], origin: 'file' },
    ],
    carried: [], noteFormat: '(note format)', maxVerify: 30,
    ...extra,
  }
}

const candidate = claim => ({
  kind: 'decision', claim, detail: claim, confirmed: 'merged in #1', topic: 'auth',
  evidence: [{ source: 'git', ref: 'abc1234', quote: claim }],
})
const proposal = (action, key, extra = {}) => ({
  action, key, domain: DOMAIN, title: `Title of ${key}`, body: `Body of ${key}.`, evidence: 'abc1234', source: 'abc1234', ...extra,
})

// Answers each agent of a sync run; tests override the parts they exercise.
// `proposals` is one list for every topic, or a list per topic name; `topics` replaces
// the merge's single topic.
function responder({ harvest, proposals = [], verdict, apply, topics } = {}) {
  return call => {
    const { label, prompt } = call
    if (label.startsWith('harvest:')) return harvest ? harvest(call) : { candidates: [candidate(`claim from ${label}`)] }
    if (label === 'merge') {
      if (topics) return { topics }
      const count = (prompt.match(/^\d+\. /gm) || []).length
      return { topics: [{ name: 'auth', groups: [Array.from({ length: count }, (_, i) => i)] }] }
    }
    if (label.startsWith('reconcile:')) {
      return { proposals: Array.isArray(proposals) ? proposals : (proposals[label.slice('reconcile:'.length)] || []) }
    }
    if (label.startsWith('verify:')) return verdict ? verdict(call) : { real: true, reason: 'the evidence says so' }
    if (label === 'apply') return apply ? apply(call) : { results: itemsOf(prompt).map(i => ({ key: i.key, domain: i.domain, ok: true })) }
    if (label === 'report') return 'the report'
    throw new Error(`unexpected agent ${label}`)
  }
}

test('meta is a pure literal with the phases the script uses', () => {
  assert.equal(workflow.meta.name, 'kioku-project-sync')
  assert.deepEqual(workflow.meta.phases.map(p => p.title), ['Harvest', 'Merge', 'Reconcile', 'Verify', 'Apply', 'Report'])
})

test('an update to a decision note waits for the user; refreshed agent notes go live', async () => {
  const s = stubs(baseArgs(), responder({
    proposals: [
      proposal('update', 'auth-token-ttl', { existing_origin: 'agent', tags: ['decision', 'auth'] }),
      proposal('update', 'decisions/DEC-7', { existing_origin: 'agent' }),
      proposal('update', 'deploy-steps', { existing_origin: 'agent', aliases: ['ci deploy'] }),
      proposal('new', 'cache-ttl', { aliases: ['cache lifetime'] }),
      proposal('new', 'deploy-steps'),
      proposal('link', 'auth-token-ttl', { related: ['cache-ttl'], existing_origin: 'agent' }),
      proposal('contradiction', 'kb-note', { existing_origin: 'file', conflict: 'the KB says otherwise' }),
    ],
  }))
  const result = await workflow.run(s)
  const waiting = Object.fromEntries(result.needs_user.map(p => [`${p.action} ${p.key}`, p.pending.kind]))
  assert.deepEqual(waiting, {
    'update auth-token-ttl': 'decision',
    'update decisions/DEC-7': 'decision',
    'new deploy-steps': 'other',
    'contradiction kb-note': 'decision',
  })
  for (const p of result.needs_user) assert.match(p.pending.text, /waits because/)
  const items = itemsOf(s.prompt('apply'))
  assert.deepEqual(items.map(i => `${i.action} ${i.key}`), ['update deploy-steps', 'new cache-ttl', 'link auth-token-ttl'])
  const [update, created, link] = items
  assert.equal(update.expect, 'stored')
  assert.equal(update.not_changed_since, STARTED)
  assert.equal(update.aliases, undefined, 'a stored note never gets a replacing list')
  assert.deepEqual(update.add_aliases, ['ci deploy'], 'its new aliases are added to the stored ones')
  assert.deepEqual(created.aliases, ['cache lifetime'])
  assert.equal(created.expect, 'new')
  assert.equal(link.title, undefined, 'a link keeps the stored title and text')
  assert.deepEqual(link.related, ['cache-ttl'])
  assert.deepEqual(result.aliases_not_saved, [], 'saved on a server that returns aliases')
  assert.match(s.prompt('apply'), /no stored alias is dropped/)
  assert.match(s.prompt('apply'), /"count_use": false, "max_bytes": 8000/)
  assert.match(s.prompt('apply'), /memory_inspect/)
  assert.equal(result.counts.saved, 3)
})

test('carried proposals are verified first, within maxVerify', async () => {
  const carried = [
    proposal('new', 'carried-a', { drafted_at: '2026-11-01T00:00:00Z' }),
    proposal('new', 'carried-b', { drafted_at: '2026-11-01T00:00:00Z' }),
  ]
  const s = stubs(baseArgs({ carried, maxVerify: 2 }), responder({
    proposals: [proposal('new', 'fresh-a'), proposal('new', 'fresh-b'), proposal('new', 'carried-b')],
  }))
  const result = await workflow.run(s)
  assert.deepEqual(s.labels('verify:'), ['verify:carried-a', 'verify:fresh-a'])
  assert.match(s.prompt('verify:carried-a'), /An earlier run drafted this proposal \(2026-11-01T00:00:00Z\)/)
  assert.deepEqual(result.unverified.map(p => p.key), ['fresh-b', 'carried-b'])
  assert.equal(result.unverified[1].carried, undefined, 'the fresh proposal replaced the carried one for the same note')
  const items = itemsOf(s.prompt('apply'))
  assert.deepEqual(items.map(i => [i.key, i.not_changed_since]), [['carried-a', '2026-11-01T00:00:00Z'], ['fresh-a', STARTED]])
  assert.equal(result.counts.carried, 2)
})

test('fresh proposals are verified one topic at a time, so a cap reaches every topic', async () => {
  const batches = [
    { id: 'commits-demo-1', kind: 'commits', repo: '/r/demo', items: 3, index: 0 },
    { id: 'commits-demo-2', kind: 'commits', repo: '/r/demo', items: 3, index: 1 },
  ]
  const s = stubs(baseArgs({ batches, maxVerify: 2 }), responder({
    topics: [{ name: 'auth', groups: [[0]] }, { name: 'deploys', groups: [[1]] }],
    proposals: {
      auth: [proposal('new', 'auth-a'), proposal('new', 'auth-b'), proposal('new', 'auth-c')],
      deploys: [proposal('new', 'deploy-a')],
    },
  }))
  const result = await workflow.run(s)
  assert.deepEqual(s.labels('verify:'), ['verify:auth-a', 'verify:deploy-a'])
  assert.deepEqual(result.unverified.map(p => p.key), ['auth-b', 'auth-c'])
})

test('a verification that fails leaves its proposal unverified, to be carried', async () => {
  const s = stubs(baseArgs(), responder({
    proposals: [proposal('new', 'kept'), proposal('new', 'lost-check')],
    verdict: call => (call.label === 'verify:lost-check' ? null : { real: true, reason: 'ok' }),
  }))
  const result = await workflow.run(s)
  assert.deepEqual(result.unverified.map(p => p.key), ['lost-check'])
  assert.deepEqual(result.saved, [`kept (${DOMAIN})`])
})

test('failed harvest sources are named in the result and the report', async () => {
  const args = baseArgs({
    batches: [1, 2, 3].map(n => ({ id: `commits-demo-${n}`, kind: 'commits', repo: '/r/demo', items: 2, index: n - 1 })),
    slack: [{ id: 'C1', name: '#demo', since: '2026-11-01T00:00:00Z' }],
  })
  const s = stubs(args, responder({
    proposals: [proposal('new', 'from-ok-sources')],
    harvest: call => {
      if (call.label === 'harvest:commits-demo-2') return null
      if (call.label === 'harvest:commits-demo-3') throw new Error('agent errored')
      if (call.label === 'harvest:slack #demo') return { candidates: [], unavailable: 'the channel is private' }
      return { candidates: [candidate('a fact')] }
    },
  }))
  const result = await workflow.run(s)
  assert.deepEqual(result.failed_sources, ['commits-demo-2', 'commits-demo-3'])
  assert.deepEqual(result.unavailable, [{ source: 'slack:C1', name: 'slack #demo', reason: 'the channel is private' }])
  assert.equal(result.counts.failed, 2)
  assert.match(s.prompt('report'), /Failed: commits-demo-2, commits-demo-3\. The window must not be marked/)
})

test('a topic whose reconcile failed is reported as failed, so the window is read again', async () => {
  const s = stubs(baseArgs(), call => (call.label.startsWith('reconcile:') ? null : responder()(call)))
  const result = await workflow.run(s)
  assert.deepEqual(result.failed_sources, ['reconcile:auth'])
  assert.match(s.prompt('report'), /Failed: reconcile:auth\. The window must not be marked/)
})

test('with no candidates, the early result still names the failed sources', async () => {
  const s = stubs(baseArgs(), responder({ harvest: () => null }))
  const result = await workflow.run(s)
  assert.deepEqual(result.failed_sources, ['commits-demo-1'])
  assert.match(result.report, /Failed: commits-demo-1\. The window must not be marked/)
  assert.deepEqual(s.labels(''), ['harvest:commits-demo-1'])
})

test('harvest runs at most 6 agents at a time', async () => {
  const batches = Array.from({ length: 10 }, (_, n) => ({ id: `commits-demo-${n + 1}`, kind: 'commits', repo: '/r/demo', items: 1, index: n }))
  const s = stubs(baseArgs({ batches }), responder({ harvest: () => ({ candidates: [] }) }))
  await workflow.run(s)
  assert.equal(s.labels('harvest:').length, 10)
  assert.equal(s.peak('Harvest'), 6)
})

test('carried proposals are verified even when nothing new was harvested', async () => {
  const carried = [proposal('new', 'carried-a', { drafted_at: '2026-11-01T00:00:00Z' })]
  const s = stubs(baseArgs({ carried }), responder({ harvest: () => ({ candidates: [] }) }))
  const result = await workflow.run(s)
  assert.deepEqual(s.labels('verify:'), ['verify:carried-a'])
  assert.deepEqual(s.labels('merge'), [])
  assert.deepEqual(result.saved, [`carried-a (${DOMAIN})`])
})

test('the verifier rejects instructions and pasted text, and agents read with count_use false', async () => {
  const s = stubs(baseArgs(), responder({ proposals: [proposal('new', 'a-note')] }))
  await workflow.run(s)
  const verify = s.prompt('verify:a-note')
  assert.match(verify, /instructions addressed to an agent/)
  assert.match(verify, /pasted verbatim from a PR, commit, issue or thread/)
  assert.match(verify, /"count_use": false/)
  assert.match(verify, /does not accept count_use \(an older server\), call it again without count_use/)
  assert.match(verify, /"max_bytes": 8000/)
  assert.match(verify, /truncated/)
})

test('a note skipped at save time is filed for the user; a failed save is retried next run', async () => {
  const s = stubs(baseArgs(), responder({
    proposals: [proposal('new', 'saved-one'), proposal('update', 'deploy-steps', { existing_origin: 'agent' }), proposal('new', 'broken')],
    apply: () => ({ results: [
      { key: 'saved-one', domain: DOMAIN, ok: true },
      { key: 'deploy-steps', domain: DOMAIN, ok: false, skipped: true, detail: 'changed at 2026-11-07T20:05:00Z, after this run read it' },
      { key: 'broken', domain: DOMAIN, ok: false, detail: 'server error' },
    ] }),
  }))
  const result = await workflow.run(s)
  assert.deepEqual(result.skipped.map(p => [p.key, p.pending.kind]), [['deploy-steps', 'other']])
  assert.deepEqual(result.retry.map(p => p.key), ['broken'])
  assert.deepEqual(result.failed_saves, [{ key: 'broken', domain: DOMAIN, detail: 'server error' }])
})
