---
name: global-briefing-runbook
description: Generate, repair, or audit the Phase-A file-backed Chinese global briefing artifacts under work/global-briefing, including source collection, dated reports, prediction records, China mapping, paper-trading updates, and weekly/monthly reviews. Use for briefing research and economic-record preparation; use atlas-daily-operator for the full cross-project cycle and atlas-site-publisher for website deployment.
---

# Global Briefing Runbook

## Scope

Use this skill as the Phase-A orchestrator for `work/global-briefing`. Keep the output file-first: save durable Markdown/JSON artifacts, then hand off to `$atlas-daily-operator` for cross-project controls.

## Required Context

Read these files before a full run:

- `work/global-briefing/config/settings.json`
- `work/global-briefing/config/sources.json`
- `work/global-briefing/config/skills.json`
- `work/global-briefing/config/china_watchlist.json`
- `work/global-briefing/config/paper_trading.json`
- `work/global-briefing/config/paper_theme_registry.json`
- Prior dated report through `briefing_store.py previous --date YYYY-MM-DD`
- Active gated evolution policy in `work/global-briefing/data/evolution_state.json` (also printed by `briefing_store.py previous`)
- Prior paper-trading state through `paper_trading.py previous`

Use `skills.json` as the authoritative routing table. Load only the skills that add evidence, quality control, or market interpretation for the current run.

## Run Date And Resume

Resolve one `RUN_DATE` from `settings.json.timezone` before any command. Never switch to a UTC calendar date or pass a literal placeholder.

Treat an existing dated report or same-day ledger entry as a resume. Inspect prediction, order, and valuation identities before mutation. Once cross-project publication has started, do not use this skill to regenerate Phase A.

## Daily Workflow

Run setup and context commands first:

```powershell
python work\global-briefing\scripts\briefing_store.py init
python work\global-briefing\scripts\briefing_store.py previous --date YYYY-MM-DD
python work\global-briefing\scripts\paper_trading.py init
python work\global-briefing\scripts\paper_trading.py previous
python work\global-briefing\scripts\paper_theme_registry.py audit --date YYYY-MM-DD
python work\global-briefing\scripts\evolution.py update-policy --period month --date YYYY-MM-DD
```

`previous` must be the first analytical context step. It scans the entire prediction
ledger, writes `work/global-briefing/data/review-queue-YYYY-MM-DD.json`, and prints the
bounded `review_now` set before the prior report. Work through that set before creating
new forecasts. Do not limit review discovery to yesterday or the last N ledger rows.
It also prints `CALIBRATION_RECOVERY_GUIDANCE`. When research promotion is still
shadow, use its independent-family gap and advisory batch target to prefer genuinely
decision-useful novel event families. Never create filler, relabel a rolling update as
novel, automatically change a probability, or rewrite history to satisfy the target.
Date-only forecast and market deadlines cover the full named day in the configured
report timezone; an ordinary review is valid on a later date, while same-day closure
requires explicit `terminal_evidence=true`.

`previous --date` automatically prepares a no-network version of the bounded resolution
workbench so startup cannot forget prior review debt. During evidence collection, refresh
it with market access when useful:

```powershell
python work\global-briefing\scripts\resolution_evidence.py prepare --date YYYY-MM-DD --timeout 8 --workers 4
python work\global-briefing\scripts\drift_diagnostics.py --date YYYY-MM-DD --write
```

Read `data/resolution-evidence-YYYY-MM-DD.json` before web research. It turns every
`review_now` item into an explicit resolution question and may produce objective
market-price candidates, but it never appends outcomes. Verify event evidence on the
web and independently verify any price candidate before recording a review.

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

Mark each primary thesis with `### 核心主线：...`. Coverage sections may be shorter and must not be padded into six artificial theses. For reports dated 2026-07-15 onward, every core thesis must include a `证据角色` line with linked `一手来源=...；事件地区来源=...；外部核验=...`; repeated syndication does not count as independent confirmation.

## Predictions And Storage

Prepare a complete temporary report draft and prediction JSON before any ledger mutation. Run the read-only preflight first:

```powershell
python work\global-briefing\scripts\briefing_store.py validate-records --date YYYY-MM-DD --input TEMP_PREDICTIONS_JSON
```

Read `batch_family_audit` in the preflight result. Its shadow warning identifies when
the proposed batch adds too few independent event families or overuses rolling
restatements. Improve the batch only when the evidence supports a distinct causal
proposition; otherwise keep the useful forecast and disclose the sample shortfall.
The family audit is not permission to add low-value forecasts and is not an
operational blocker.

Only after it succeeds, append structured prediction records:

```powershell
python work\global-briefing\scripts\briefing_store.py record --date YYYY-MM-DD --input TEMP_PREDICTIONS_JSON
```

Confirm every intended original/review is present exactly once, then run the non-mutating operational report gate before paper orders or marks:

```powershell
python work\global-briefing\scripts\research_quality.py --date YYYY-MM-DD --report TEMP_REPORT_MD --predictions work\global-briefing\data\predictions.jsonl --dry-run
```

If either preflight fails, save a clearly marked partial report when useful; do not append incomplete predictions, apply orders, mark valuations, or enter publication.

For predictions dated 2026-07-12 or later, use the v2 pre-registration contract in
`work/global-briefing/config/prediction.schema.json`. Probability must be numeric
between 0 and 1; include an explicit deadline, resolution question, success/failure
criteria, evidence snapshots, and benchmarked market-mapping rules. Do not translate
`high/medium/low` into probabilities after the outcome is known.
Every named ticker also needs its own `market_mapping.evaluation_deadline` aligned
to an observable market session. Event and asset deadlines may differ; never use a
stale weekend/holiday close to resolve a new asset forecast.
For predictions dated 2026-07-14 or later, each mapping also requires a machine-readable
`evaluation` object: `metric=total_return`, `window_start`, `price_field`, and an explicit
comparison. This freezes the measurement window before the result is known and prevents
post-hoc interpretation of prose rules.
For predictions dated 2026-07-15 or later, preregister `event_family_id`,
`baseline_state`, `novelty_delta`, and `independence_rationale`. A rolled deadline or
paraphrase stays in the same family unless the baseline evidence or causal proposition
materially changes. Every asset mapping also needs a stable `market_thesis_id`.
Market quotes must use a direct auditable URL or a content-addressed local artifact
with retrieval time, query, and SHA-256.
The storage layer idempotently skips an existing review with the same
`prediction_id`, `status`, and `review.review_date`. Never rewrite the original.
An unresolved matured v2 forecast blocks the operational deployment gate until a
valid resolution review is appended or the run is explicitly saved as partial.
Event reviews use `review.resolution_scope=event` (or `combined`) and feed Brier/log-loss/ECE.
Market-only reviews use `status=active`, `review.resolution_scope=market`, and a
`market_resolution` list. They never close or score the event. Conversely, an event
review does not remove unresolved asset mappings from the queue. Publish event-scoring
and benchmark-relative mapping coverage separately.

Write the report as a dated Markdown file:

```powershell
python work\global-briefing\scripts\briefing_store.py write --date YYYY-MM-DD --input TEMP_REPORT_MD
python atlas.py quality --date YYYY-MM-DD
```

After the final dated report and ledgers are verified, stop Phase A and hand off to `$atlas-daily-operator`. Do not run website deployment from this skill.

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
- Keep `date` (decision/ledger date) and `price_date` (the actual quoted market session) separate. From the configured enforcement date, every priced order must provide `price_date`; a stale close recorded today is never treated as today's close.
- From `paper_trading.json.strategy_context_contract.enforce_from_date` (currently 2026-08-02), every BUY must carry `strategy_context` with `schema_version=1`, numeric `signal_score`, `intent=OPEN|ADD`, `thesis_status=intact|strengthening|watch|failed`, `account_risk={as_of_date,daily_return_pct,portfolio_drawdown_pct}`, and a `confirming_evidence` list whose entries contain `source`, `as_of_date`, and `summary`. Percentages are decimal fractions. Derive account risk from the authoritative prior/current valuation state; never invent confirming evidence. Missing, stale, below-threshold, over-add-limit, failed-thesis, averaging-down-without-new-evidence, or circuit-breaker BUY must fail closed while SELL/HOLD remain available for risk reduction.
- From the configured enforcement date, every A-share BUY/SELL must also carry a source-dated finite `previous_close`; board/ST/IPO regime determines the applicable price band. Missing reference-close evidence blocks the priced action.
- Resolve every BUY to one verified primary theme and enforce the account-local theme cap before append. The sidecar registry may classify legacy positions without rewriting historical orders; conflicts, unknown themes, or incomplete open-position coverage fail closed.
- Never edit the theme registry silently. Record each intentional change with `paper_theme_registry.py record-revision --date YYYY-MM-DD --reason "..."`, then require `audit` to pass. From the configured date, BUY embeds the current revision ID and SHA-256; an unrecorded change blocks BUY while SELL and HOLD remain available.
- Treat `drift-diagnostics-YYYY-MM-DD.json` as a shadow control plane with four separate states: source concentration, event-calibration drift, theme/instrument crowding, and account-separated paper attribution. Do not combine them into one score or let an insufficient sample become a quality conclusion.
- Self-healing may refresh the deterministic drift artifact, but must never respond to it by changing sources, probabilities, reviews, themes, or virtual orders automatically.
- Use BUY/SELL/HOLD only as virtual simulation records.
- Do not give personalized real-money advice, target prices, stop losses, or guaranteed returns.
- If data is stale, low-confidence, or limit-breaching, prefer HOLD or skip the virtual trade.
