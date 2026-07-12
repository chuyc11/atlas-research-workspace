---
name: forecast-scorecard
description: Review and score briefing predictions for this project. Use when comparing prior forecasts with outcomes, writing daily/weekly/monthly self-iteration, validating prediction records, or updating forecast framework lessons.
---

# Forecast Scorecard

## Purpose

Use this skill to close the forecast loop: prediction -> evidence -> outcome -> error reason -> framework update. Keep the review grounded in saved prediction records and dated reports.

## Inputs

Read:

- `work/global-briefing/data/predictions.jsonl`
- `work/global-briefing/config/evolution.json`
- Prior dated reports in `outputs/每日全球晨间简报-YYYY-MM-DD.md`
- Existing scorecards and attribution files under `work/global-briefing/data/`

## Daily Review

Generate or refresh the daily scorecard:

```powershell
python work\global-briefing\scripts\evolution.py scorecard --period day --date YYYY-MM-DD --write
python work\global-briefing\scripts\evolution.py validate-records --period day --date YYYY-MM-DD
```

When writing the daily report, include a concise review of yesterday's open predictions:

- What happened.
- Whether the forecast was `hit`, `partial`, `miss`, or still `open`.
- Which evidence changed the view.
- Which failure reason applies if the forecast was weak.
- What rule should change in future forecasts.

## Weekly And Monthly Review

Use the longer cadence for pattern detection:

```powershell
python work\global-briefing\scripts\evolution.py scorecard --period week --date YYYY-MM-DD --write
python work\global-briefing\scripts\evolution.py scorecard --period month --date YYYY-MM-DD --write
python work\global-briefing\scripts\evolution.py write-review --period week --date YYYY-MM-DD
python work\global-briefing\scripts\evolution.py write-review --period month --date YYYY-MM-DD
```

Look for repeated errors:

- Wrong asset mapping from event to ticker/theme.
- Overweighting headlines that reversed quickly.
- Missing policy/regulatory trigger.
- Poor horizon choice.
- Weak falsification signal.
- Data quality or stale-price error.

## New Prediction Records

Before saving a new briefing, create JSON prediction records with:

- Stable id.
- Date and horizon.
- Scenario or claim.
- Trigger.
- Probability as low/medium/high or project-compatible value.
- Beneficiary and pressured themes.
- Verification/falsification signals.
- Linked market observations or virtual-paper decisions when applicable.

For predictions dated 2026-07-12 or later, the project-compatible probability is a
numeric value strictly between 0 and 1. Also include `deadline`, a pre-registered
`resolution` object, source snapshots in `evidence`, and benchmarked
`market_mapping` entries when tickers are named.
Each named ticker must be covered by a market-mapping entry with direction,
benchmark, verification rule, and a session-aligned `evaluation_deadline`.

Save records with:

```powershell
python work\global-briefing\scripts\briefing_store.py record --date YYYY-MM-DD --input TEMP_PREDICTIONS_JSON
```

Do not invent outcomes for forecasts that have not reached their horizon. Mark them open and list the next verification signal.

For a matured v2 prediction, append (never overwrite) a review with binary
`review.observed_outcome`, `review.review_date`, and resolution evidence. Premature
closure is ineligible for proper scoring unless `terminal_evidence=true`. Use Brier
score, log loss, and expected calibration error for research claims; the legacy
direction/timing/transmission/calibration rubric is diagnostic only.
