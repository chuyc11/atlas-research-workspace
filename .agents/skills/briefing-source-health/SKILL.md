---
name: briefing-source-health
description: Assess evidence reliability for this global briefing project. Use when checking source freshness, RSS/API failures, quote fallback quality, stale data, citation diversity, or report limitations before saving a briefing.
---

# Briefing Source Health

## Purpose

Use this skill to decide how much confidence to assign to a daily briefing's evidence base. The output should improve the saved report, not become a standalone essay unless the user asks for one.

## Inputs

Read or inspect:

- `work/global-briefing/config/sources.json`
- `work/global-briefing/config/settings.json`
- RSS output from `work/global-briefing/scripts/rss_collect.py`
- China snapshot from `work/global-briefing/scripts/china_market.py`
- US/global quote helper output from `work/global-briefing/scripts/market_snapshot.py`
- Fresh web sources used in the current briefing

## Health Checks

Classify each important evidence path:

- `primary`: official, regulator, exchange, central bank, company release, or direct data source.
- `authoritative_media`: Reuters/AP/AFP/BBC/FT/NYT/Guardian/DW/France 24/Al Jazeera/Nikkei/The Hindu/SCMP/Caixin or similar.
- `market_data`: exchange, AKShare/Tushare/BaoStock, Yahoo-compatible fallback, yfinance, or helper script output.
- `fallback`: source used only because preferred paths failed or timed out.
- `stale_or_limited`: old timestamp, missing fields, timeout, 403, partial quote, thin liquidity, or unverified claim.

For every high-impact claim or ticker, look for at least one independent confirmation path. Prefer multi-country and primary-source confirmation when geopolitics, regulation, public health, military/security, energy, or market-moving policy is involved.

## Limitation Wording

Record limitations plainly in the report, for example:

- `RSS baseline was partial because SOURCE returned HTTP 403.`
- `China spot quote collection timed out; Yahoo-compatible fallback was used for selected tickers.`
- `Helper data was unavailable, so market observations rely on public quotes and should be treated as lower confidence.`

Do not hide missing data. Convert uncertainty into lower confidence, HOLD decisions, or explicit verification signals.

## Quality Gate

Before saving the briefing, check:

- Dates and time zones are explicit where relative dates could confuse.
- Each market observation has ticker, market/exchange, thesis, risk, verification signal, and source/skill.
- China market facts distinguish A-share, Hong Kong, ETFs/indices, and policy drivers.
- Paper-trading prices include source and stale-data handling.
- The Sources section contains enough public links or named primary/authoritative sources for audit.

After the dated report and prediction records exist, run `python atlas.py quality
--date YYYY-MM-DD`. Use its report-domain, link, section, and thesis-layer audit as
the saved machine-readable quality result. Do not infer forecast calibration from
source quality; calibration requires resolved v2 prediction samples.
