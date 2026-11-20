"""The Section 9 dispatcher's parts. `vps/dispatch.py` is the entry point and the only script.

  net        secrets, redaction and the HTTP calls (GitHub, OpenRouter, Hermes)
  board      the task board: issues in section9-hq, their labels and who may take them
  limits     the roster and the root-only limits file
  ledger     the run ledger and what a run cost
  cpu        /proc/stat samples and the "CPU credits" issue
  openrouter the key's cap, usage and the pace line
  lanes      the optional second lane and its fallback latch
  pick       sweep, choose and claim one task
  run        one Hermes run and its ledger record
  routines   cron routines that open issues
  reports    the "Spend", "Capacity" and "Sync" issues, and `status`
  health     the weekly "Code Health effect" issue: the ledger joined with the PRs' Code Health lines

Standard library only. Functions in another module are called through the module
(`cpu.cpu_sample()`), so a test can replace one in the module that defines it.
"""
