export const meta = {
  name: 'kioku-reflect',
  description: 'Audit a Kioku domain: check each topic for contradicting decisions, stale records, out-of-date architecture notes and diagrams, and code drift; verify every finding adversarially; report',
  whenToUse: 'Run by the reflect skill with args built from reflect_inventory.py',
  phases: [
    { title: 'Check', detail: 'one agent per topic cluster' },
    { title: 'Verify', detail: 'an independent skeptic per high or medium finding' },
    { title: 'Report', detail: 'one agent writes the report' },
  ],
}

// args: {
//   domain, root,                         Kioku domain and the source's root folder
//   source: 'okf' | 'markdown',
//   clusters: [{ name, topic_note, decisions: [paths], kind? }],
//                                        kind 'kioku': decisions are keys of
//                                        notes agents saved, read with memory_get
//   code: [{ repo, path, ref }],          clones and the ref their decisions describe
//   deterministic: { counts, samples },   from the inventory, for the report
//   maxVerify: 4                          verifications per cluster
// }
const a = args
const maxVerify = a.maxVerify || 4

// Counted, so the skill can record what a run cost per agent.
let agents = 0
function run(prompt, options) {
  agents += 1
  return agent(prompt, options)
}
log(`${a.clusters.length} topics: up to ${a.clusters.length * (1 + maxVerify) + 1} agents`)

const FINDINGS = {
  type: 'object',
  properties: {
    findings: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          kind: { type: 'string', enum: ['contradiction', 'unrecorded-override', 'status-mismatch', 'topic-out-of-date', 'diagram-out-of-date', 'code-contradicts', 'answered-question', 'other'] },
          keys: { type: 'array', items: { type: 'string' } },
          claim: { type: 'string' },
          evidence: { type: 'string' },
          severity: { type: 'string', enum: ['high', 'medium', 'low'] },
          fix: { type: 'string' },
          needs_user: { type: 'boolean' },
        },
        required: ['kind', 'keys', 'claim', 'evidence', 'severity', 'fix', 'needs_user'],
      },
    },
  },
  required: ['findings'],
}

const VERDICT = {
  type: 'object',
  properties: {
    real: { type: 'boolean' },
    reason: { type: 'string' },
    corrected_fix: { type: 'string' },
  },
  required: ['real', 'reason'],
}

const where = a.source === 'okf'
  ? `Its source is an OKF knowledge base. Paths below are relative to the bundle root ${a.root}; read them with the Read tool as ${a.root}<path>.`
  : `Its source is a folder of Markdown notes; the paths below are absolute.`
const kiokuWhere = `These are notes agents saved in Kioku, with no files behind them. Read them with the memory_get tool (domain ${a.domain}, at most 10 keys per call), passing "count_use": false, because an audit isn't use, and "max_bytes": 8000, so notes aren't cut at the default 2000 bytes. If Kioku answers that memory_get does not accept count_use (an older server), call it again without count_use. If a response says truncated, read fewer keys per call until it doesn't: never judge a note from cut text. If the tool isn't loaded, load it with ToolSearch "select:mcp__kioku__memory_get". memory_get also returns each note's linked keys: follow them to the decisions a note relies on.`
const whereFor = c => c.kind === 'kioku' ? kiokuWhere : where
const listFor = c => c.kind === 'kioku'
  ? `Notes agents saved in this domain (${c.decisions.length}); they may be decisions, conventions, gotchas or fixes`
  : `Decisions in force in this topic (${c.decisions.length})`
const codeText = (a.code || []).length
  ? `Code it describes: ${a.code.map(c => `${c.repo} at ${c.path}, ref ${c.ref}`).join('; ')}. Use git in those folders (git show <ref>:<file>, git grep -n <pattern> <ref>, git log --oneline -5 <ref> -- <file>), never the working tree, which may be on another branch.`
  : 'No code repository is configured; skip code checks.'

function checkPrompt(c) {
  return `Audit the "${c.name}" topic of the Kioku domain ${a.domain}. ${whereFor(c)}

${listFor(c)}:
${c.decisions.map(d => `- ${d}`).join('\n')}

Topic note: ${c.topic_note || 'none'}. Topic notes are long: list their headings first (for example with Grep for '^#'), then read only the sections these decisions touch, plus any diagram (fenced blocks, box drawings) and the sections around it.
${codeText}

Report only problems a careful owner would fix:
1. contradiction: two decisions in force state incompatible rules for the same thing.
2. unrecorded-override: a later decision changes a rule that an earlier decision in force still states, and neither records it (no supersedes or superseded_by, no "Narrowed by" or "Narrows" link, no dated Status line).
3. status-mismatch: a decision's Status text and its decision_status disagree.
4. topic-out-of-date: the topic note states something the decisions in force no longer say.
5. diagram-out-of-date: a diagram shows components, flows or names that the decisions in force or the code no longer have.
6. code-contradicts: a decision names concrete code (a file, function, table, column, constant, queue or topic) and the code at the given ref says otherwise. Check at most 6 such claims, the most concrete first.
7. answered-question: a question still open that a decision in this topic answers.

Not problems: superseded decisions that say so; decisions marked proposed or contested, unless they contradict one in force; differences the decision itself explains; wording and style.

For each finding give the kind, the decision ids or note paths involved, the claim in one sentence, evidence (quote the conflicting sentences or the code line, with their paths), severity (high: two rules in force conflict, or the code contradicts a decision in force; medium: a missing link, a status mismatch, an out-of-date topic note or diagram; low: anything else), the smallest fix (which file and what edit), and needs_user (true when someone must choose between two rules). Return at most 8 findings, highest severity first, and none if the topic is consistent.`
}

function verifyPrompt(f, c) {
  return `Try to refute this finding from an audit of the "${c.name}" topic of the Kioku domain ${a.domain}. ${whereFor(c)}
${codeText}

Finding:
${JSON.stringify(f, null, 1)}

Read the cited files yourself, and run the git commands for code claims. Set real=false unless the evidence holds exactly as stated: the quotes exist, every decision it calls "in force" has decision_status active, and no Status line, "Narrowed by" or "Narrows" link, supersedes field or later decision already resolves it. If it is real but the proposed fix is wrong or too broad, give corrected_fix. Give your reason in two sentences or fewer.`
}

const results = await pipeline(
  a.clusters,
  c => run(checkPrompt(c), { label: `check:${c.name}`, phase: 'Check', schema: FINDINGS }),
  (found, c) => {
    const all = (found && found.findings) || []
    const toVerify = all.filter(f => f.severity !== 'low').slice(0, maxVerify)
    const unverified = all.filter(f => !toVerify.includes(f))
    if (all.length > toVerify.length) {
      log(`${c.name}: ${all.length} findings, verifying ${toVerify.length}; ${unverified.length} reported unverified`)
    }
    return parallel(toVerify.map(f => () =>
      run(verifyPrompt(f, c), { label: `verify:${c.name}`, phase: 'Verify', schema: VERDICT })
        .then(v => ({ ...f, topic: c.name, verdict: v }))))
      .then(verified => ({ topic: c.name, decisions: c.decisions.length, verified: verified.filter(Boolean), unverified }))
  },
)

const done = results.filter(Boolean)
const confirmed = done.flatMap(r => r.verified.filter(f => f.verdict && f.verdict.real))
const refuted = done.flatMap(r => r.verified.filter(f => !f.verdict || !f.verdict.real))
const unverified = done.flatMap(r => r.unverified.map(f => ({ ...f, topic: r.topic })))
const failed = a.clusters.length - done.length

phase('Report')
const report = await run(`Write the reflect report for the Kioku domain ${a.domain}, in plain Markdown with no preamble. Be brief: one bullet per item.

Confirmed findings (each survived an independent attempt to refute it; use corrected_fix where given):
${JSON.stringify(confirmed, null, 1)}

Reported but not verified (low severity, or over the verification cap):
${JSON.stringify(unverified, null, 1)}

Refuted findings: ${refuted.length}. Topics checked: ${done.map(r => `${r.topic} (${r.decisions} decisions)`).join(', ')}.${failed ? ` Topics whose check failed: ${failed}.` : ''}

Deterministic checks: ${JSON.stringify(a.deterministic || {}, null, 1)}

Sections, in this order, leaving out any that would be empty:
1. Needs your decision: confirmed findings with needs_user. Give the ids, the conflict in one sentence, what each side says, and a suggested resolution.
2. Fix in the records: other confirmed findings. Give the file and the exact change.
3. Code has moved: confirmed code-contradicts findings, with the commit or file evidence.
4. Unverified: one line each, marked as unverified.
5. Hygiene: the deterministic counts, with the top items to look at.
6. What was checked: topics, decisions, and how many findings were raised, confirmed and refuted.`, { label: 'report', phase: 'Report' })

return { report, confirmed, refuted: refuted.length, unverified: unverified.length, topics: done.length, failed, agents }
