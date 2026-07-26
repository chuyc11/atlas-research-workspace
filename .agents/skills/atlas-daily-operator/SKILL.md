---
name: atlas-daily-operator
description: Run or resume the project-specific ATLAS daily workflow from a dated Chinese global briefing through prediction preflight, paper-account updates, the unified control cycle, improvements, deep self-healing, alerts, verified external backup, frozen publication, and website handoff. Use for requests to run, continue, recover, babysit, or diagnose the daily automation; never use it to replay Phase A after publication has started.
---

# ATLAS Daily Operator

Operate the daily workflow as a resumable state machine. Treat files and audited ledgers as authority; chat history is only context.

## Start

1. Read `work/global-briefing/config/settings.json` and resolve one `RUN_DATE` in its configured timezone. Keep that date for the entire run.
2. Read `paper_trading.json`, then `sources.json`, `skills.json`, and the remaining domain configs. Configuration overrides prompt defaults.
3. Inspect the dated report, prediction ledger, paper ledgers, cycle audit, publication state, self-healing, improvements, alerts, and backup artifacts before choosing a resume point.
4. Read [references/state-machine.md](references/state-machine.md) for the command order and state-specific resume rules.

## Route Work

- Load `$global-briefing-runbook` for Phase A report and record production.
- Load `$briefing-source-health`, `$forecast-scorecard`, `$china-market-routing`, and `$paper-attribution-review` only when their domain is active.
- Load `$atlas-integrity-maintainer` for integrity defects, failed gates, or deep repair. It must not silently change research or economic actions.
- Load `$atlas-site-publisher` only after a frozen or pending publication candidate exists.

Use `work/global-briefing/config/skills.json` as the authoritative router. Optional credentials, desktop state, and heavy analytical dependencies are nonfatal during a daily run.

## Preserve Commit Boundaries

Prepare the complete report draft and prediction JSON before any ledger mutation. Run prediction validation, append predictions idempotently, and pass the non-mutating operational report gate before paper orders or marks.

Every paper order must reference an original prediction already in the ledger and use a stable order ID. On retry, reuse existing prediction, order, and valuation identities. Never create a second economic action because a control, backup, or website stage failed.

After Phase B begins, do not regenerate news analysis, predictions, orders, marks, or account valuations. Resume only the failed control or publication stage.

## Run the Unified Control Plane

After the dated report and ledgers are complete, run the audited core cycle while explicitly deferring publication:

```powershell
python atlas.py cycle --date RUN_DATE --skip-site --skip-publication
```

Do not use `--skip-tests` or `--skip-trading-core` for a normal daily run. `--skip-publication` is required here because the current CLI otherwise enters post-gate publication automatically, before the explicit recovery and alert re-verification sequence below has completed.

Then run the closed loop in configuration order:

```powershell
python atlas.py improvements --date RUN_DATE --apply-safe --strict
python atlas.py heal --date RUN_DATE --apply-safe --deep --strict
python atlas.py backup --date RUN_DATE
python atlas.py alerts --date RUN_DATE
python atlas.py improvements --date RUN_DATE --apply-safe --strict
python atlas.py alerts --date RUN_DATE
python atlas.py backup --date RUN_DATE
```

Verify that the final improvement, self-healing, alert, and backup artifacts are date-aligned; the final backup must be external and restore-verified. Then hand off to `$atlas-site-publisher`.

Use `python atlas.py publish --date RUN_DATE` only to resume the control-plane publication sequence for an existing staged candidate when that is the chosen recovery path. It must not replay Phase A.

## Stop Conditions

Stop website work when any of these is true:

- prediction preflight or operational research gate failed;
- required ledger reference is absent or duplicated semantically;
- the dated cycle audit is failed or missing;
- deep self-healing has unresolved configured blocking severity;
- external backup is not authenticated and restore-verified;
- publication manifest is unfrozen or its payload/snapshot identity mismatches;
- a control command failed without a fresh date-aligned artifact.
- independent publication gates disagree about whether an alert or candidate is blocking.

Preserve successful earlier phases. Save a clearly marked partial report when useful; never compensate by deleting or replaying committed economic actions.

## Finish

Confirm the dated report path, prediction/review counts, paper-account actions or explicit skips, cycle status, self-healing status, backup verification, alert findings, and publication/deployment state. Deliver every unresolved alert ID in the final response.
