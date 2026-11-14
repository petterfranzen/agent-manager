import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import type { AgentRow, AgentTask, Snapshot, Spend } from '../types'

const PANE = 'section9'
const HQ = 'petterfranzen/section9-hq'
const ROSTER = 'repos/petterfranzen/agent-manager/contents/agents/roster.toml'
const ORDER = ['Batou', 'Ishikawa', 'Saito', 'Togusa', 'Tachikoma']
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
    const lower = name.toLowerCase()
    const tasks: AgentTask[] = []
    for (const i of issues) {
      const labels = i.labels.map(l => l.name)
      if (!labels.includes(`agent:${lower}`)) continue
      const status = labels.find(l => l.startsWith('status:'))?.slice(7) ?? ''
      if (!(status in STATUS_RANK)) continue
      const waits = labels.find(l => l.startsWith('waits:'))?.slice(6) ?? ''
      tasks.push({ number: i.number, title: i.title, status, waits })
    }
    tasks.sort((a, b) => (STATUS_RANK[a.status] ?? 9) - (STATUS_RANK[b.status] ?? 9) || b.number - a.number)
    const r = roster.get(name) ?? { name, title: '', model: '?', runsPerDay: '?', budgetCents: 0 }
    return { ...r, tasks, week: week.get(name) ?? null }
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
    const { Box, Text, Button } = $.ui.resolve(e)
    const snap = await read($, snapshot)

    if (!snap) return <Text dimColor>Loading the board…</Text>

    const s = snap.spend
    const spendAgeH = s?.asOf ? (Date.now() - new Date(s.asOf).getTime()) / 3600_000 : 99

    return (
      <Box flexDirection="column" gap={1}>
        <Box flexDirection="column">
          <Text bold>Budget (OpenRouter)</Text>
          {s ? (
            <>
              <Text>
                {s.month} of {s.cap} this month · <Text bold color="success">{s.left} left</Text> · today {s.today}
              </Text>
              <Text dimColor color={spendAgeH > 3 ? 'error' : undefined}>
                as of {s.asOf ? cet(s.asOf) : '?'}
                {spendAgeH > 3 ? ' (stale: section9-spend.timer may have stopped)' : ''}
              </Text>
            </>
          ) : (
            <Text color="warning">No Spend issue found.</Text>
          )}
        </Box>

        {snap.agents.map(a => (
          <Box flexDirection="column">
            <Text>
              <Text bold>{a.name}</Text>
              <Text dimColor> {a.title}</Text>
            </Text>
            <Text dimColor>
              {a.model} · {a.runsPerDay === '0' ? 'no run cap' : `${a.runsPerDay} runs/day`}
              {a.budgetCents ? ` · cap $${(a.budgetCents / 100).toFixed(2)}/mo` : ''}
            </Text>
            {a.week && (
              <Text dimColor>
                7d: {a.week.runs} runs · {a.week.busyH} h · {a.week.spend} · {a.week.timeouts} timeouts
              </Text>
            )}
            {a.tasks.length === 0 && <Text dimColor italic>  idle: nothing on the board</Text>}
            {a.tasks.slice(0, 4).map(t => (
              <Text>
                {'  '}
                <Text color={STATUS_COLOR[t.status]}>{t.status.padEnd(7)}</Text>
                <Text dimColor>#{t.number} </Text>
                {t.title.length > 70 ? `${t.title.slice(0, 69)}…` : t.title}
                {t.waits ? <Text dimColor> (waits: {t.waits})</Text> : ''}
              </Text>
            ))}
            {a.tasks.length > 4 && <Text dimColor>  +{a.tasks.length - 4} more</Text>}
          </Box>
        ))}

        <Box flexDirection="column">
          {snap.error ? <Text color="error">Last refresh failed: {snap.error}</Text> : null}
          <Text dimColor>
            Updated {cet(snap.fetchedAt)}
            {snap.capacityAsOf ? ` · 7-day figures from Capacity #80, ${cet(snap.capacityAsOf)} (books about half of real spend)` : ''}
          </Text>
          <Button key="refresh" label="Refresh" onPress={() => refresh($)} />
        </Box>
      </Box>
    )
  })
}
