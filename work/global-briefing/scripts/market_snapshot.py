#!/usr/bin/env python3
"""Optional market snapshot helper using yfinance when it is available."""

from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCRIPT_PATH = Path(__file__).resolve()
ROOT = SCRIPT_PATH.parents[3]
DATA_DIR = ROOT / "work" / "global-briefing" / "data"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0 Safari/537.36 CodexMarketSnapshot/1.0"
)
REQUEST_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "application/json,text/plain,*/*",
    "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.7,zh;q=0.6",
}


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_close_series(data: Any, ticker: str) -> dict[str, Any]:
    try:
        if getattr(data, "empty", True):
            return {"ticker": ticker, "error": "no price data"}
        frame = data
        if hasattr(data, "columns") and getattr(data.columns, "nlevels", 1) > 1:
            if ticker in data.columns.get_level_values(0):
                frame = data[ticker]
            else:
                return {"ticker": ticker, "error": "ticker missing from batch result"}
        closes = frame["Close"].dropna()
        if closes.empty:
            return {"ticker": ticker, "error": "no close data"}
        last = float(closes.iloc[-1])
        prev = float(closes.iloc[-2]) if len(closes) > 1 else None
        change_pct = ((last / prev) - 1) * 100 if prev else None
        last_date = str(closes.index[-1].date()) if hasattr(closes.index[-1], "date") else str(closes.index[-1])
        return {
            "ticker": ticker,
            "last_close": last,
            "previous_close": prev,
            "change_pct": change_pct,
            "price_date": last_date,
            "source": "yfinance.download batch",
            "fetched_at": now_utc(),
        }
    except Exception as exc:
        return {"ticker": ticker, "error": str(exc)}


def fetch_yfinance_batch(tickers: list[str], request_timeout: int, queue: mp.Queue) -> None:
    try:
        import yfinance as yf  # type: ignore
    except Exception as exc:
        queue.put({"items": [{"ticker": ticker, "error": f"yfinance import failed: {exc}"} for ticker in tickers]})
        return

    try:
        data = yf.download(
            " ".join(tickers),
            period="5d",
            interval="1d",
            progress=False,
            threads=True,
            group_by="ticker",
            timeout=request_timeout,
            auto_adjust=False,
        )
        queue.put({"items": [parse_close_series(data, ticker) for ticker in tickers]})
    except Exception as exc:
        queue.put({"items": [{"ticker": ticker, "error": str(exc)} for ticker in tickers]})


def fetch_batch_with_timeout(tickers: list[str], request_timeout: int, total_timeout: int) -> list[dict[str, Any]]:
    ctx = mp.get_context("spawn")
    queue: mp.Queue = ctx.Queue()
    proc = ctx.Process(target=fetch_yfinance_batch, args=(tickers, request_timeout, queue))
    proc.start()
    proc.join(total_timeout)
    if proc.is_alive():
        proc.terminate()
        proc.join(2)
        return [{"ticker": ticker, "error": f"batch timed out after {total_timeout}s"} for ticker in tickers]
    if not queue.empty():
        value = queue.get()
        return value.get("items", [])
    return [{"ticker": ticker, "error": f"worker exited with code {proc.exitcode}"} for ticker in tickers]


def fetch_yahoo_chart(ticker: str, timeout: int) -> dict[str, Any]:
    encoded = urllib.parse.quote(ticker, safe="")
    url = f"https://query2.finance.yahoo.com/v8/finance/chart/{encoded}?range=5d&interval=1d"
    request = urllib.request.Request(url, headers=REQUEST_HEADERS)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        return {"ticker": ticker, "error": f"yahoo chart failed: {exc}"}
    result = (payload.get("chart", {}).get("result") or [None])[0]
    if not result:
        return {"ticker": ticker, "error": "yahoo chart not_found"}
    quote = (result.get("indicators", {}).get("quote") or [{}])[0]
    closes = [as_float(value) for value in quote.get("close", [])]
    timestamps = result.get("timestamp", [])
    valid = [(idx, close) for idx, close in enumerate(closes) if close is not None]
    if not valid:
        return {"ticker": ticker, "error": "yahoo chart no close data"}
    last_idx, last = valid[-1]
    prev = valid[-2][1] if len(valid) > 1 else None
    price_date = None
    if timestamps and last_idx < len(timestamps):
        price_date = datetime.fromtimestamp(timestamps[last_idx], timezone.utc).date().isoformat()
    return {
        "ticker": ticker,
        "last_close": last,
        "previous_close": prev,
        "change_pct": ((last / prev) - 1) * 100 if prev else None,
        "price_date": price_date,
        "source": "Yahoo Finance chart direct",
        "fetched_at": now_utc(),
    }


def snapshot(tickers: list[str], per_ticker_timeout: int) -> dict:
    request_timeout = max(15, per_ticker_timeout)
    total_timeout = max(45, request_timeout * 2)
    items = fetch_batch_with_timeout(tickers, request_timeout=request_timeout, total_timeout=total_timeout)
    by_ticker = {item.get("ticker"): item for item in items}
    missing = [ticker for ticker in tickers if by_ticker.get(ticker, {}).get("last_close") is None]
    for ticker in missing:
        time.sleep(0.25)
        fallback = fetch_yahoo_chart(ticker, timeout=request_timeout)
        if fallback.get("last_close") is not None:
            by_ticker[ticker] = fallback
        else:
            existing = by_ticker.get(ticker, {"ticker": ticker})
            existing["fallback_error"] = fallback.get("error")
            by_ticker[ticker] = existing
    ordered = [by_ticker.get(ticker, {"ticker": ticker, "error": "missing result"}) for ticker in tickers]
    return {"generated_at": now_utc(), "items": ordered}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fetch a small market snapshot for candidate tickers.")
    parser.add_argument("tickers", nargs="+")
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--per-ticker-timeout", type=int, default=8)
    args = parser.parse_args(argv)

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    result = snapshot(args.tickers, args.per_ticker_timeout)
    output_path = args.output or DATA_DIR / f"market-snapshot-{datetime.now().strftime('%Y-%m-%d')}.json"
    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(output_path)
    error_count = sum(1 for item in result["items"] if item.get("last_close") is None)
    print(f"items={len(result['items'])} errors={error_count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
