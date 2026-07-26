---
name: paper-attribution-review
description: Manage and review the project's virtual US and China paper-trading accounts. Use when initializing accounts, reading positions, applying virtual orders, marking prices, attributing P/L, or enforcing paper-trading guardrails.
---

# Paper Attribution Review

## Scope

Use this skill only for virtual simulation. Never place real orders or imply real execution.

## Required Files

Read:

- `work/global-briefing/config/paper_trading.json`
- `work/global-briefing/config/paper_theme_registry.json`
- `work/global-briefing/data/paper_portfolio_us.json`
- `work/global-briefing/data/paper_portfolio_china.json`
- `work/global-briefing/data/paper_trades_us.jsonl`
- `work/global-briefing/data/paper_trades_china.jsonl`
- `work/global-briefing/data/paper_valuations_us.jsonl`
- `work/global-briefing/data/paper_valuations_china.jsonl`

Initialize and inspect state:

```powershell
python work\global-briefing\scripts\paper_trading.py init
python work\global-briefing\scripts\paper_trading.py previous
```

## Account Separation

Keep the accounts independent:

- `US`: US stocks/ETFs only.
- `CHINA`: A-share and Hong Kong stocks/ETFs only.

Never merge cash, equity, buying power, positions, P/L, or performance between accounts.

## Virtual Decisions

Each BUY/SELL/HOLD decision must include:

- stable `order_id` in the form `YYYY-MM-DD-ACCOUNT-ACTION-SYMBOL-PREDICTION_ID`
- account: `US` or `CHINA`
- symbol and exchange/market
- action
- quantity for BUY/SELL, or current position for HOLD when useful
- price and price source
- decision date and actual market-session price date
- canonical primary theme, supplied explicitly or by a verified registry entry
- thesis/reason
- risk
- linked prediction/scenario when possible

Use only `BUY`, `SELL`, and `HOLD` in the ledger. Express an addition as `BUY` and a reduction as `SELL`. Resolve a full liquidation to the finite held quantity; never write `ALL`.

Before a same-day retry, inspect orders and valuations. Reuse an identical stable ID without creating another economic action; stop on an ID/content conflict. Every BUY or SELL must reference an original prediction already present in `predictions.jsonl`.

Prefer HOLD or skip when data is stale, confidence is low, or risk limits would be breached.

## Risk Rules

Respect `paper_trading.json`:

- No leverage.
- No short selling.
- No options or derivatives by default.
- Respect maximum single-position percentage.
- Respect maximum daily turnover.
- Resolve every open position to one canonical primary risk theme and reject future BUY actions that would breach `maximum_theme_exposure_pct`.
- Keep minimum cash reserve.
- A-share BUY orders round down to 100-share lots.
- A-share same-day SELL is blocked by T+1.
- Hong Kong trades use integer shares.

## Apply And Mark

Apply virtual orders:

```powershell
python work\global-briefing\scripts\paper_trading.py apply-orders --date YYYY-MM-DD --input TEMP_ORDERS_JSON
```

Mark prices after orders:

```powershell
python work\global-briefing\scripts\paper_trading.py mark --date YYYY-MM-DD --input TEMP_PRICES_JSON
```

Do not run apply-orders and mark in parallel for the same account. Sequential execution avoids overwriting state.

## Attribution

Generate attribution for daily/weekly/monthly reviews:

```powershell
python work\global-briefing\scripts\evolution.py paper-attribution --period day --date YYYY-MM-DD --write
python work\global-briefing\scripts\evolution.py paper-attribution --period week --date YYYY-MM-DD --write
python work\global-briefing\scripts\evolution.py paper-attribution --period month --date YYYY-MM-DD --write
```

In the report, separate account equity, cash, positions, recent virtual trades, today's decisions, thesis, risk, and portfolio review.
Daily attribution is the change since the previous recorded valuation (or the reconstructed pre-period state), not lifetime unrealized P/L. Always expose the start-exclusive/end-inclusive window, starting and ending equity, period P/L, period return, and the reconciliation difference. Keep cumulative unrealized P/L as a separate position diagnostic.

The theme registry is non-economic risk master data. Audit its current content-addressed revision with:

```powershell
python work\global-briefing\scripts\paper_theme_registry.py audit --date YYYY-MM-DD
```

For any intentional registry edit, record a reasoned revision before applying a new BUY:

```powershell
python work\global-briefing\scripts\paper_theme_registry.py record-revision --date YYYY-MM-DD --reason "Explain the mandate or thesis change"
```

Do not rewrite historical trades to improve theme coverage. A verified sidecar assignment may classify an existing position for attribution. Registry conflicts, unknown BUY themes, unclassified open positions, an unrecorded current registry revision, and post-order theme exposure above the configured cap must fail closed before a BUY append. A registry-history problem must not block SELL or HOLD.
