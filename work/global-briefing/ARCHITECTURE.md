# ATLAS Global Briefing Architecture

## System objective

ATLAS is a closed-loop research system, not only a report generator:

`evidence -> briefing -> pre-registered forecast -> resolution -> scoring -> learned rule -> paper attribution -> audited site publication`

The dated report is the human-readable output. JSON/JSONL ledgers are the canonical state.

## Canonical layers

1. Evidence: RSS, web verification, China/global market snapshots and source-health artifacts.
2. Intelligence: dated Chinese briefing with explicit facts, claims, mechanisms, counterevidence and falsification signals.
3. Forecast ledger: immutable originals plus append-only reviews in `data/predictions.jsonl`.
4. Review control: deterministic full-ledger queue in `data/review-queue-YYYY-MM-DD.json`.
5. Resolution evidence: bounded workbench in `data/resolution-evidence-YYYY-MM-DD.json`.
6. Evolution: event proper-scoring and independent benchmark-relative asset scoring.
7. Simulation: independent US and CHINA paper accounts with idempotent orders and marks.
8. Risk master data: verified non-economic position-theme registry with one primary cap bucket per instrument, content-addressed snapshots, and a hash-chained revision history.
9. Control plane: ATLAS cycle, improvement actions, self-healing, alerts and disaster recovery.
10. Drift control: point-in-time source, calibration, theme/instrument and account-separated paper diagnostics.
11. Publication: content-addressed site payload and isolated Sites deployment.

## Non-negotiable invariants

- One RUN_DATE and configured timezone govern the entire daily run.
- Originals are immutable; reviews and virtual actions are append-only and idempotent.
- Date-only deadlines include the full named day. Same-day closure requires terminal evidence.
- Event resolution and market-mapping evaluation have independent deadlines.
- An event review never closes an unresolved market mapping; a market-only review never closes the event.
- Event Brier/log-loss/ECE and asset benchmark-relative hit/coverage metrics are never mixed.
- Promotion sample size and calibration weight use independent event families, not raw rolling prediction IDs. The append-only family registry repairs historical grouping without changing immutable originals.
- A matured v2 forecast without a valid review blocks production publication.
- From 2026-07-15, new forecasts and mappings require stable family IDs, baseline/novelty declarations, and reproducible evidence provenance; due market mappings fail closed.
- Missing or stale market data produces HOLD or a partial run, never invented evidence.
- Self-healing may repair derived artifacts but not conclusions, probabilities, orders, source code or deployment state.
- Decision dates and market price dates are separate ledger facts; recording an older close today never makes it a current close.
- Historical paper orders stay immutable. A sidecar registry may classify their open positions, while every future BUY must resolve a canonical primary theme, reference the current audited registry revision, and pass the account-local theme cap before append. An unrecorded registry edit blocks BUY but never blocks SELL or HOLD.
- Drift dimensions remain separate and sample-aware. There is no composite quality score, and shadow diagnostics cannot block deployment or mutate research.
- Site publication must use an isolated worktree and the exact audited payload.
- Coverage sections remain mandatory, but only explicitly marked core theses carry the five-layer and linked source-role contract; at most five core theses are allowed.
- A10-complete publication payloads are immutable. Retries reuse the frozen payload; a same-day revision is explicit and preserves history.

## Daily review policy

The daily run scans the complete prediction ledger before reading recent rows. The queue prioritizes scoring-eligible v2 forecasts and newly due records, exposes a bounded `review_now` set, and preserves the remainder as `review_backlog`. Event and mapping resolution states are partitioned independently. The full artifact is fingerprinted from the prediction ledger and relevant policy, and self-healing verifies it before deployment. A second derived workbench turns `review_now` into explicit event research tasks and pre-registered price calculations; it cannot mutate the ledger. A third deterministic artifact, `drift-diagnostics-YYYY-MM-DD.json`, compares only information known by the run date and keeps source concentration, event calibration, theme/instrument crowding, and the two paper accounts independent.

## Current optimization priorities

1. Burn down legacy early-closure debt without crowding out newly due v2 reviews.
2. Add primary-source adapters for the highest-frequency event-resolution domains without auto-deciding outcomes.
3. Burn down pre-machine-contract mapping debt through explicit non-destructive rule translations.
4. Accumulate four weekly drift artifacts, review false positives, and promote only individual dimensions whose data coverage passes the configured policy.
5. Require an explicit reason and audited revision for every registry classification change; review the recorded before/after diff when an instrument mandate or thesis moves a position between primary cap buckets.
6. Add snapshot recovery verification that can reconstruct the exact deployed payload from an external backup.

## Publication snapshot

Once cycle, deep self-healing, improvement verification, final alerts, and external restore verification are all date-aligned, site sync freezes the full validated payload under `work/shared/atlas/publication_snapshots/`. Operational telemetry is displayed inside that payload but excluded from editorial content identity. Later retries load the frozen payload rather than rebuilding it from mutable ledgers. Any report or ledger drift fails closed and requires an explicit revision after all gates are rerun; the previous revision is archived.
