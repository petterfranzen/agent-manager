export type AgentTask = { number: number; title: string; status: string; waits: string }
export type AgentRow = {
  name: string
  title: string
  model: string
  runsPerDay: string
  budgetCents: number
  tasks: AgentTask[]
  week: { runs: string; busyH: string; spend: string; timeouts: string } | null
}
export type Spend = { asOf: string; month: string; cap: string; left: string; today: string }
export type Snapshot = {
  fetchedAt: number
  agents: AgentRow[]
  spend: Spend | null
  capacityAsOf: string
  error: string
}

declare module 'claude-code' {
  interface PluginState {
    section9: { snapshot: Shaped<Snapshot | null> }
  }
}
