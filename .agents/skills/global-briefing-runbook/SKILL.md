---
name: global-briefing-runbook
description: Run the file-backed Chinese daily global morning briefing workflow for this project. Use when generating, repairing, or auditing the daily briefing, weekly/monthly summaries, prediction records, source collection, or paper-trading update under work/global-briefing.
---

# Global Briefing Runbook

## Scope

Use this skill as the project-specific orchestrator for `work/global-briefing`. Keep the output file-first: save durable Markdown/JSON artifacts, then reply briefly in chat.

## Required Context

Read these files before a full run:

- `work/global-briefing/config/settings.json`
- `work/global-briefing/config/sources.json`
- `work/global-briefing/config/skills.json`
- `work/global-briefing/config/china_watchlist.json`
- `work/global-briefing/config/paper_trading.json`
- Prior dated report through `briefing_store.py previous --date YYYY-MM-DD`
- Active gated evolution policy in `work/global-briefing/data/evolution_state.json` (also printed by `briefing_store.py previous`)
- Prior paper-trading state through `paper_trading.py previous`

Use `skills.json` as the authoritative routing table. Load only the skills that add evidence, quality control, or market interpretation for the current run.

## Daily Workflow

Run setup and context commands first:

```powershell
python work\global-briefing\scripts\briefing_store.py init
python work\global-briefing\scripts\briefing_store.py previous --date YYYY-MM-DD
python work\global-briefing\scripts\paper_trading.py init
python work\global-briefing\scripts\paper_trading.py previous
python work\global-briefing\scripts\evolution.py update-policy --period month --date YYYY-MM-DD
```

Collect baseline evidence when relevant:

```powershell
python work\global-briefing\scripts\rss_collect.py --timeout 10
python work\global-briefing\scripts\china_market.py snapshot --timeout 60 --dataset-timeout 8 --yahoo-timeout 8 --yahoo-workers 8
python work\global-briefing\scripts\market_snapshot.py TICKER1 TICKER2 --per-ticker-timeout 15
```

Treat helper failures as nonfatal. Continue with cited public sources and record the limitation in the report.

## Report Requirements

Write a Chinese report with these sections:

- Core summary
- Yesterday forecast review and self-iteration
- Politics and diplomacy
- Technology and AI
- Environment and climate
- Military and security
- Economy and energy
- Public health and society
- Future forecasts and market mapping across 1 day, 1 week, and 1 month
- Stock/ETF observation list
- China market mapping
- Paper trading accounts
- Today watchlist
- Risk signals
- Forecast framework updates
- Sources

Clearly separate confirmed facts, analysis, and virtual simulation. Use multi-country authoritative sources plus fresh web verification. Avoid single-country or single-language dependency.

Optimize for decision density, not length. Follow `evolution_state.json.report_contract`: keep at most five primary theses, state background once, and give each thesis exactly the useful layers—conclusion, hard evidence, causal mechanism, counterevidence, and falsification signal. Remove generic transitions, repeated risk disclaimers, and stock lists that are not tied to a verification rule.

## Predictions And Storage

Before writing the report, save structured prediction records:

```powershell
python work\global-briefing\scripts\briefing_store.py record --date YYYY-MM-DD --input TEMP_PREDICTIONS_JSON
```

For predictions dated 2026-07-12 or later, use the v2 pre-registration contract in
`work/global-briefing/config/prediction.schema.json`. Probability must be numeric
between 0 and 1; include an explicit deadline, resolution question, success/failure
criteria, evidence snapshots, and benchmarked market-mapping rules. Do not translate
`high/medium/low` into probabilities after the outcome is known.
Every named ticker also needs its own `market_mapping.evaluation_deadline` aligned
to an observable market session. Event and asset deadlines may differ; never use a
stale weekend/holiday close to resolve a new asset forecast.

Write the report as a dated Markdown file:

```powershell
python work\global-briefing\scripts\briefing_store.py write --date YYYY-MM-DD --input TEMP_REPORT_MD
python atlas.py quality --date YYYY-MM-DD
```

After the dated report and unified cycle are complete, run the closed-loop
retrospective-action verification and self-healing gates before website deployment:

```powershell
python atlas.py improvements --date YYYY-MM-DD --apply-safe --strict
python atlas.py heal --date YYYY-MM-DD --apply-safe --deep --strict
```

`atlas.py improvements` converts every active retrospective recommendation into
a durable action with a stable id, owner domain, due date, acceptance rule,
evidence, and status. The next eligible run must verify it. A failed previously
verified rule becomes `regressed`; an unverified rule becomes `overdue` after its
deadline. Do not mark an action complete from prose alone.

The self-healing policy is defined in
`work/global-briefing/config/self_healing.json`. It may automatically repair
only allowlisted low-risk derived artifacts. It must register, but never
automatically change, source code, research conclusions, probabilities,
paper-trading orders, production access, or deployments. Every attempted repair
requires a pre-change snapshot, post-change verification, audit record, circuit
breaker, and rollback on verification failure. A remaining critical issue blocks
deployment; a high or medium issue must be disclosed and routed for review.

Treat `atlas.py quality --strict` as the research-promotion gate. A normal daily run
may remain operational while strict mode stays blocked for insufficient resolved
samples; report that state as `shadow`, never as calibrated.

Do not append new daily reports into `outputs/每日全球晨间简报.md`; that file is legacy archive only.

## Weekly And Monthly

When asked, or on scheduled review cadence, generate separate summary/review artifacts:

```powershell
python work\global-briefing\scripts\briefing_store.py summary-context --period week --date YYYY-MM-DD
python work\global-briefing\scripts\briefing_store.py summary-context --period month --date YYYY-MM-DD
python work\global-briefing\scripts\evolution.py write-review --period week --date YYYY-MM-DD
python work\global-briefing\scripts\evolution.py write-review --period month --date YYYY-MM-DD
```

## Guardrails

- Never place or imply real broker orders.
- Keep US and CHINA paper-trading accounts independent.
- Use BUY/SELL/HOLD only as virtual simulation records.
- Do not give personalized real-money advice, target prices, stop losses, or guaranteed returns.
- If data is stale, low-confidence, or limit-breaching, prefer HOLD or skip the virtual trade.
