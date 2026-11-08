// Runs a Workflow tool script under Node with stubbed globals, for tests.
// Like the Workflow tool, it runs the body as an async function, so top-level await
// and return work; `meta` must be a pure literal, and Date.now(), Math.random() and
// new Date() with no arguments throw.
import { readFileSync } from 'node:fs'
import vm from 'node:vm'

const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor

function strictDate() {
  function StrictDate(...given) {
    if (!given.length) throw new Error('new Date() with no arguments is not available in a workflow')
    return new Date(...given)
  }
  StrictDate.now = () => { throw new Error('Date.now() is not available in a workflow') }
  StrictDate.parse = Date.parse
  StrictDate.UTC = Date.UTC
  return StrictDate
}

const strictMath = Object.assign(Object.create(Math), {
  random: () => { throw new Error('Math.random() is not available in a workflow') },
})

// The meta object's text: from `export const meta = {` to the first line that is `}`.
function metaText(text, path) {
  const start = text.indexOf('export const meta = {')
  if (start !== 0) throw new Error(`${path} must begin with export const meta`)
  const end = text.indexOf('\n}\n', start)
  return text.slice('export const meta = '.length, end + 2)
}

export function loadWorkflow(path) {
  const text = readFileSync(path, 'utf8')
  // A pure literal evaluates in an empty context: a variable, a call or a spread of one throws.
  // Copied through JSON, so its objects belong to this realm.
  const meta = JSON.parse(JSON.stringify(vm.runInNewContext(`(${metaText(text, path)})`, {})))
  const body = text.replace('export const meta = ', 'const meta = ')
  const fn = new AsyncFunction('agent', 'parallel', 'pipeline', 'phase', 'log', 'args', 'budget', 'Date', 'Math', body)
  return {
    meta,
    run: s => fn(s.agent, s.parallel, s.pipeline, s.phase, s.log, s.args, s.budget, strictDate(), strictMath),
  }
}

// Stubs that record every agent call. `respond(call)` answers each one; returning null
// is an agent that died, and throwing is an agent that errored.
export function stubs(args, respond) {
  const calls = []
  const logs = []
  const phases = []
  const active = new Map()
  const peak = new Map()
  const agent = async (prompt, options = {}) => {
    const call = { prompt, label: options.label || '', phase: options.phase || null, options }
    calls.push(call)
    const group = call.phase || 'none'
    active.set(group, (active.get(group) || 0) + 1)
    peak.set(group, Math.max(peak.get(group) || 0, active.get(group)))
    try {
      await new Promise(resolve => setTimeout(resolve, 2))
      return await respond(call)
    } finally {
      active.set(group, active.get(group) - 1)
    }
  }
  const parallel = thunks => Promise.all(thunks.map(thunk => Promise.resolve().then(thunk).catch(() => null)))
  const pipeline = (items, ...stages) => Promise.all(items.map(async (item, index) => {
    let value = item
    try {
      for (const stage of stages) value = await stage(value, item, index)
      return value
    } catch {
      return null
    }
  }))
  return {
    agent, parallel, pipeline, args,
    phase: title => phases.push(title),
    log: message => logs.push(message),
    budget: { total: null, spent: () => 0, remaining: () => Infinity },
    calls, logs, phases,
    peak: group => peak.get(group) || 0,
    labels: prefix => calls.filter(c => c.label.startsWith(prefix)).map(c => c.label),
    prompt: label => (calls.find(c => c.label === label) || {}).prompt,
  }
}

// The JSON list a save prompt ends with: the notes the apply agent is asked to save.
export function itemsOf(prompt) {
  return JSON.parse(prompt.slice(prompt.lastIndexOf('\n\n[') + 2))
}
