# Daily state machine

## Authority and resume table

| State | Required evidence | Next action | Never repeat |
|---|---|---|---|
| `not_started` | No dated report or same-day records | Initialize context and build drafts | None |
| `drafted` | Complete temporary report and prediction JSON | Validate predictions | Paper mutation |
| `predictions_recorded` | Every intended original/review exists exactly once | Run non-mutating operational report gate | Prediction creation |
| `paper_committed` | Stable order/valuation IDs and account summaries exist | Write and verify dated report | Orders or marks |
| `phase_a_complete` | Dated report, ledgers, references, and quality checks pass | Run unified cycle | All Phase A work |
| `candidate_staged` | Cycle audit passes and staged candidate identity exists | Resume `atlas.py publish` or site publisher | Phase A and core cycle |
| `frozen_or_pending` | Frozen manifest and matching payload/snapshot hashes | Use `$atlas-site-publisher` | Candidate regeneration |
| `deployed` | Fresh live verifier and deployed marker match | Report success | All economic and deployment actions |

## Phase A commit order

Replace `RUN_DATE` with the resolved date before running any command.

```powershell
python work\global-briefing\scripts\briefing_store.py init
python work\global-briefing\scripts\briefing_store.py previous --date RUN_DATE
python work\global-briefing\scripts\paper_trading.py init
python work\global-briefing\scripts\paper_trading.py previous
python work\global-briefing\scripts\paper_theme_registry.py audit --date RUN_DATE
python work\global-briefing\scripts\evolution.py update-policy --period month --date RUN_DATE
python work\global-briefing\scripts\briefing_store.py validate-records --date RUN_DATE --input TEMP_PREDICTIONS_JSON
python work\global-briefing\scripts\briefing_store.py record --date RUN_DATE --input TEMP_PREDICTIONS_JSON
python work\global-briefing\scripts\research_quality.py --date RUN_DATE --report TEMP_REPORT_MD --predictions work\global-briefing\data\predictions.jsonl --dry-run
python work\global-briefing\scripts\paper_trading.py apply-orders --date RUN_DATE --input TEMP_ORDERS_JSON
python work\global-briefing\scripts\paper_trading.py mark --date RUN_DATE --input TEMP_PRICES_JSON
python work\global-briefing\scripts\briefing_store.py write --date RUN_DATE --input TEMP_REPORT_MD
python atlas.py quality --date RUN_DATE
```

Use `RUN_DATE-ACCOUNT-ACTION-SYMBOL-PREDICTION_ID` as each stable order ID. A full liquidation must resolve the held quantity to a finite number; never write `ALL`. For BUY orders at or after the configured strategy-context enforcement date, include the schema-v1 signal, intent, thesis, account-risk and confirming-evidence object required by `paper_trading.json`; percentages use decimal fractions. A-share priced actions at or after their configured enforcement date require finite, source-dated `previous_close`. Skip `apply-orders` or `mark` when the intended stable IDs already exist with identical content. Stop on an identity/content conflict. A valid `HOLD` is preferable to a forced trade when configured blockers apply.

## Closed loop

Run the core cycle without crossing into publication:

```powershell
python atlas.py cycle --date RUN_DATE --skip-site --skip-publication
```

Then execute the durable closed-loop stages:

```powershell
python atlas.py improvements --date RUN_DATE --apply-safe --strict
python atlas.py heal --date RUN_DATE --apply-safe --deep --strict
python atlas.py alerts --date RUN_DATE
python atlas.py improvements --date RUN_DATE --apply-safe --strict
python atlas.py alerts --date RUN_DATE
python atlas.py backup --date RUN_DATE
```

Read the date-aligned JSON artifacts under `work/shared/atlas/` after each command. A recent authenticated and restore-verified snapshot makes recovery capability observable before strict controls, so the daily path writes only the final post-verification checkpoint. If bootstrap readiness reports a stale or absent snapshot, create one daily checkpoint before strict controls and stop if it does not restore-verify. The daily checkpoint must reference a recent restore-verified full baseline; schedule `python atlas.py backup --date RUN_DATE --full` at the configured low frequency rather than embedding it in every run. An alert may be nonblocking only when its artifact explicitly classifies every finding and configured blocking severities remain absent.

Do not infer success from a zero exit code alone. Verify the final self-healing counts/status, restore result, alert findings, cycle audit, and artifact dates directly. Proper-scoring sample insufficiency may remain shadow-only; operational, ledger, recovery, or publication failures do not.

After these stages pass, invoke `$atlas-site-publisher`. Use `python atlas.py publish --date RUN_DATE` only for a deliberate retry of an existing staged candidate; it must not rerun Phase A.

## Required final artifacts

- `outputs/每日全球晨间简报-RUN_DATE.md`
- `work/shared/atlas/run_audits/atlas-cycle-RUN_DATE.json`
- `work/shared/atlas/improvements/latest.json`
- `work/shared/atlas/self_healing/latest.json`
- `work/shared/atlas/alerts/latest.json`
- `work/shared/atlas/backups/latest.json`
- `work/shared/atlas/publication_snapshots/atlas-publication-RUN_DATE.json` when publication is frozen

All date-bearing artifacts must use the same `RUN_DATE`.
