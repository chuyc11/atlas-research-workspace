---
name: china-market-routing
description: Prepare China market mapping for this briefing project. Use when collecting A-share/Hong Kong/China ETF data, mapping policy and macro drivers, handling China quote limitations, or selecting eligible China paper-trading candidates.
---

# China Market Routing

## Scope

Use this skill for the China section of the daily briefing and the CHINA virtual account. Cover A-share, Hong Kong, China ETFs/indices, policy/macro drivers, and quote-source limitations.

## Required Inputs

Read:

- `work/global-briefing/config/china_watchlist.json`
- `work/global-briefing/config/paper_trading.json`
- `work/global-briefing/config/sources.json`
- latest `work/global-briefing/data/china-market-snapshot-YYYY-MM-DD.json` when present

Collect a fresh snapshot when relevant:

```powershell
python work\global-briefing\scripts\china_market.py snapshot --timeout 60 --dataset-timeout 8 --yahoo-timeout 8 --yahoo-workers 8
```

If a source times out, continue with cited public sources and record the limitation.

## Source Preference

Prefer, in order:

1. Exchange, regulator, central bank, government, company announcement, and index provider sources.
2. AKShare, Tushare, BaoStock, or other structured China market datasets when available.
3. Yahoo-compatible quote fallback for selected tickers.
4. Caixin, SCMP, Nikkei Asia, Reuters, AP, Bloomberg-quality reporting, and other authoritative media.

Do not treat social posts, unsourced market chatter, or stale quote pages as primary evidence.

## Mapping Template

For the China market section, include:

- A-share index and sector tone.
- Hong Kong market tone.
- China ETFs/indices.
- Policy, credit, property, consumption, technology, export, RMB, and geopolitical drivers.
- Eligible CHINA paper-trading candidates.
- Quote-source limitations.

## Candidate Rules

Use practical suffixes:

- Shanghai: `510300.SH`, `588000.SH`, `512480.SH`
- Shenzhen: `159915.SZ`, `512660.SH` when configured as China-related ETF universe
- Hong Kong: `0700.HK`, `9988.HK`, `3033.HK`

Before naming a candidate, check:

- It belongs to the CHINA account universe: A-share or Hong Kong.
- Price source is current enough for a virtual decision.
- Liquidity and tradability are acceptable for simulation.
- A-share orders comply with 100-share lot and T+1 constraints.
- The idea links to a policy, macro, sector, or scenario thesis.

If confidence is low, use HOLD or observation-only language.
