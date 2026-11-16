import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import type { AgentRow, AgentTask, Snapshot, Spend } from '../types'

const PANE = 'section9'
const HQ = 'petterfranzen/section9-hq'
const ROSTER = 'repos/petterfranzen/agent-manager/contents/agents/roster.toml'
const ORDER = ['Batou', 'Ishikawa', 'Togusa', 'Tachikoma']
const STATUS_RANK: Record<string, number> = { working: 0, todo: 1, review: 2, blocked: 3 }
const REFRESH_MS = 5 * 60 * 1000

const snapshot = atom({ plugin: 'section9', key: 'snapshot' } as const, null, {
  shape: 'snapshot-1',
})

type Issue = {
  number: number
  title: string
  body: string | null
  pull_request?: unknown
  labels: { name: string }[]
}

async function gh($: EngineInterface, args: string[]): Promise<string> {
  const r = await $.process.run(['gh', 'api', ...args], { timeoutMs: 60_000 })
  if (r.exitCode !== 0) throw new Error(`gh api ${args[args.length - 1]}: ${r.stderr.trim().slice(0, 160)}`)
  return r.stdout
}

function cet(when: string | number): string {
  const d = new Date(when)
  if (isNaN(d.getTime())) return String(when)
  try {
    const t = d.toLocaleTimeString('sv-SE', { timeZone: 'Europe/Stockholm', hour: '2-digit', minute: '2-digit' })
    const day = d.toLocaleDateString('en-GB', { timeZone: 'Europe/Stockholm', weekday: 'short', day: 'numeric', month: 'short' })
    return `${t} CET ${day}`
  } catch {
    const p = new Date(d.getTime() + 3600_000)
    return `${String(p.getUTCHours()).padStart(2, '0')}:${String(p.getUTCMinutes()).padStart(2, '0')} CET`
  }
}

function asOfUtc(body: string): string {
  const m = body.match(/As of (\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}) UTC/)
  return m ? `${m[1]}T${m[2]}:00Z` : ''
}

function parseSpend(body: string): Spend {
  const pick = (label: string) => body.match(new RegExp(`${label}[^:]*:\\s*(\\$[\\d.,]+)`))?.[1] ?? '?'
  return { asOf: asOfUtc(body), month: pick('This month'), cap: pick('Cap'), left: pick('Left'), today: pick('Today') }
}

function parseCapacity(body: string): Map<string, AgentRow['week']> {
  const out = new Map<string, AgentRow['week']>()
  const start = body.indexOf('Last 7 days by agent**')
  if (start < 0) return out
  for (const line of body.slice(start).split('\n').slice(1)) {
    if (line.includes('by agent and model')) break
    const [, name = '', runs = '', busyH = '', spend = '', , timeouts = ''] = line.split('|').map(c => c.trim())
    if (!ORDER.includes(name)) continue
    out.set(name, { runs, busyH, spend, timeouts })
  }
  return out
}

function parseRoster(toml: string): Map<string, Omit<AgentRow, 'tasks' | 'week'>> {
  const out = new Map<string, Omit<AgentRow, 'tasks' | 'week'>>()
  const defaults = toml.split('[[agents]]')[0] ?? ''
  const field = (src: string, key: string) =>
    src.match(new RegExp(`^${key}\\s*=\\s*"?([^"#\\n]*?)"?\\s*(#.*)?$`, 'm'))?.[1]?.trim()
  for (const chunk of toml.split('[[agents]]').slice(1)) {
    const own = chunk.split(/^\[agents\./m)[0] ?? ''
    const name = field(own, 'name')
    if (!name) continue
    out.set(name, {
      name,
      title: field(own, 'title') ?? '',
      model: (field(own, 'model') ?? field(defaults, 'model') ?? 'DeepSeek (default)').replace(/^.*\//, ''),
      runsPerDay: field(own, 'runs_per_day') ?? field(defaults, 'runs_per_day') ?? '?',
      budgetCents: Number(field(own, 'budget_monthly_cents') ?? 0),
    })
  }
  return out
}

// One agent's open tasks: working first, then todo, review and blocked; newest first within each.
function tasksOf(issues: Issue[], name: string): AgentTask[] {
  const label = `agent:${name.toLowerCase()}`
  const tasks: AgentTask[] = []
  for (const i of issues) {
    const labels = i.labels.map(l => l.name)
    const status = labels.find(l => l.startsWith('status:'))?.slice(7) ?? ''
    if (!labels.includes(label) || !(status in STATUS_RANK)) continue
    const waits = labels.find(l => l.startsWith('waits:'))?.slice(6) ?? ''
    tasks.push({ number: i.number, title: i.title, status, waits })
  }
  return tasks.sort((a, b) => (STATUS_RANK[a.status] ?? 9) - (STATUS_RANK[b.status] ?? 9) || b.number - a.number)
}

async function fetchSnapshot($: EngineInterface): Promise<Snapshot> {
  const [issuesRaw, capRaw, toml] = await Promise.all([
    gh($, [`repos/${HQ}/issues?state=open&per_page=100`]),
    gh($, [`repos/${HQ}/issues/80`]),
    gh($, ['-H', 'Accept: application/vnd.github.raw', ROSTER]),
  ])
  const issues = (JSON.parse(issuesRaw) as Issue[]).filter(i => !i.pull_request)
  const spendIssue = issues.find(i => /^spend\b/i.test(i.title))
  const capBody = (JSON.parse(capRaw) as Issue).body ?? ''
  const roster = parseRoster(toml)
  const week = parseCapacity(capBody)

  const agents: AgentRow[] = ORDER.map(name => {
    const r = roster.get(name) ?? { name, title: '', model: '?', runsPerDay: '?', budgetCents: 0 }
    return { ...r, tasks: tasksOf(issues, name), week: week.get(name) ?? null }
  })

  return {
    fetchedAt: Date.now(),
    agents,
    spend: spendIssue?.body ? parseSpend(spendIssue.body) : null,
    capacityAsOf: asOfUtc(capBody),
    error: '',
  }
}

async function refresh($: EngineInterface): Promise<void> {
  try {
    const next = await fetchSnapshot($)
    await update($, snapshot, () => next)
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err)
    await update($, snapshot, prev =>
      prev ? { ...prev, error: message } : { fetchedAt: Date.now(), agents: [], spend: null, capacityAsOf: '', error: message },
    )
  }
}

const STATUS_COLOR: Record<string, string> = {
  working: 'success',
  todo: 'suggestion',
  review: 'warning',
  blocked: 'error',
}

// The pane's element table: what `$.ui.resolve(e)` hands the render hook.
type Ui = ReturnType<EngineInterface['ui']['resolve']>

function runCap(a: AgentRow): string {
  return a.runsPerDay === '0' ? 'no run cap' : `${a.runsPerDay} runs/day`
}

function budgetCap(a: AgentRow): string {
  return a.budgetCents ? ` · cap $${(a.budgetCents / 100).toFixed(2)}/mo` : ''
}

function shortTitle(title: string): string {
  return title.length > 70 ? `${title.slice(0, 69)}…` : title
}

function capacityNote(snap: Snapshot): string {
  if (!snap.capacityAsOf) return ''
  return ` · 7-day figures from Capacity #80, ${cet(snap.capacityAsOf)} (books about half of real spend)`
}

function spendView({ Text }: Ui, s: Spend) {
  const spendAgeH = s.asOf ? (Date.now() - new Date(s.asOf).getTime()) / 3600_000 : 99
  const stale = spendAgeH > 3
  return (
    <>
      <Text>
        {s.month} of {s.cap} this month · <Text bold color="success">{s.left} left</Text> · today {s.today}
      </Text>
      <Text dimColor color={stale ? 'error' : undefined}>
        as of {s.asOf ? cet(s.asOf) : '?'}
        {stale ? ' (stale: section9-spend.timer may have stopped)' : ''}
      </Text>
    </>
  )
}

function budgetView(ui: Ui, s: Spend | null) {
  const { Box, Text } = ui
  return (
    <Box flexDirection="column">
      <Text bold>Budget (OpenRouter)</Text>
      {s ? spendView(ui, s) : <Text color="warning">No Spend issue found.</Text>}
    </Box>
  )
}

function taskView({ Text }: Ui, t: AgentTask) {
  return (
    <Text>
      {'  '}
      <Text color={STATUS_COLOR[t.status]}>{t.status.padEnd(7)}</Text>
      <Text dimColor>#{t.number} </Text>
      {shortTitle(t.title)}
      {t.waits ? <Text dimColor> (waits: {t.waits})</Text> : ''}
    </Text>
  )
}

function agentView(ui: Ui, a: AgentRow) {
  const { Box, Text } = ui
  return (
    <Box flexDirection="column">
      <Text>
        <Text bold>{a.name}</Text>
        <Text dimColor> {a.title}</Text>
      </Text>
      <Text dimColor>
        {a.model} · {runCap(a)}
        {budgetCap(a)}
      </Text>
      {a.week && (
        <Text dimColor>
          7d: {a.week.runs} runs · {a.week.busyH} h · {a.week.spend} · {a.week.timeouts} timeouts
        </Text>
      )}
      {a.tasks.length === 0 && <Text dimColor italic>  idle: nothing on the board</Text>}
      {a.tasks.slice(0, 4).map(t => taskView(ui, t))}
      {a.tasks.length > 4 && <Text dimColor>  +{a.tasks.length - 4} more</Text>}
    </Box>
  )
}

function footerView({ Box, Text, Button }: Ui, snap: Snapshot, onRefresh: () => Promise<void>) {
  return (
    <Box flexDirection="column">
      {snap.error ? <Text color="error">Last refresh failed: {snap.error}</Text> : null}
      <Text dimColor>
        Updated {cet(snap.fetchedAt)}
        {capacityNote(snap)}
      </Text>
      <Button key="refresh" label="Refresh" onPress={onRefresh} />
    </Box>
  )
}

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    await $.command.register({
      name: 'section9',
      description: 'Show what the Section 9 agents are doing and their budget',
    })
    void $.ui.open({ id: PANE, title: 'Section 9' })
    void refresh($)
    $.clock.every(REFRESH_MS, () => void refresh($))
    return next(e)
  })

  on('command.run', { command: 'section9' }, async $ => {
    const opened = await $.ui.open({ id: PANE, title: 'Section 9' })
    void refresh($)
    return {
      text: opened.isPlaced
        ? 'Section 9 pane opened; refreshing.'
        : 'Section 9 pane requested, but no surface placed it (isPlaced=false).',
    }
  })

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const ui = $.ui.resolve(e)
    const { Box, Text } = ui
    const snap = await read($, snapshot)

    if (!snap) return <Text dimColor>Loading the board…</Text>

    return (
      <Box flexDirection="column" gap={1}>
        {budgetView(ui, snap.spend)}

        {snap.agents.map(a => agentView(ui, a))}

        {footerView(ui, snap, () => refresh($))}
      </Box>
    )
  })
}
