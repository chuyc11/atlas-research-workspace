#!/usr/bin/env python3
"""China A-share/HK market snapshot helper for the global briefing project."""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import multiprocessing as mp
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


SCRIPT_PATH = Path(__file__).resolve()
if str(SCRIPT_PATH.parent) not in sys.path:
    sys.path.insert(0, str(SCRIPT_PATH.parent))

from report_clock import report_date as current_report_date  # noqa: E402

ROOT = SCRIPT_PATH.parents[3]
WATCHLIST_PATH = ROOT / "work" / "global-briefing" / "config" / "china_watchlist.json"
DATA_DIR = ROOT / "work" / "global-briefing" / "data"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0 Safari/537.36 CodexChinaMarket/1.0"
)
REQUEST_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "application/json,text/plain,*/*",
    "Accept-Language": "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7",
}
TENCENT_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/plain,*/*",
    "Accept-Language": "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": "https://finance.qq.com/",
}
EASTMONEY_FIELDS = "f43,f57,f58,f169,f170,f60,f47,f48,f46,f44,f45,f86,f152"
ALLOWED_QUOTE_HOSTS = {"query1.finance.yahoo.com", "qt.gtimg.cn", "push2.eastmoney.com"}


def open_quote_url(request: urllib.request.Request, timeout: int):
    parsed = urllib.parse.urlsplit(request.full_url)
    if parsed.scheme != "https" or parsed.hostname not in ALLOWED_QUOTE_HOSTS:
        raise ValueError(f"quote URL is not allowlisted: {request.full_url}")
    # The scheme and exact provider hostname are validated immediately above.
    return urllib.request.urlopen(request, timeout=timeout)  # nosec B310


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def timestamp_price_date(value: Any, timezone_name: str = "Asia/Shanghai") -> str | None:
    try:
        stamp = int(value)
    except (TypeError, ValueError):
        return None
    try:
        return datetime.fromtimestamp(stamp, timezone.utc).astimezone(ZoneInfo(timezone_name)).date().isoformat()
    except (OSError, OverflowError, ValueError):
        return None


def text_price_date(value: Any) -> str | None:
    text = str(value or "").strip()
    digits = "".join(character for character in text if character.isdigit())
    if len(digits) < 8:
        return None
    try:
        return datetime.strptime(digits[:8], "%Y%m%d").date().isoformat()
    except ValueError:
        return None


def load_watchlist(path: Path = WATCHLIST_PATH) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def all_watchlist_items(config: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for group in ("indices", "etfs", "a_shares", "hong_kong"):
        for item in config.get(group, []):
            next_item = dict(item)
            next_item["group"] = group
            items.append(next_item)
    return items


def normalize_symbol(symbol: str) -> tuple[str, str]:
    raw = symbol.strip().upper()
    if raw.endswith(".SH"):
        return raw[:-3], "SH"
    if raw.endswith(".SZ"):
        return raw[:-3], "SZ"
    if raw.endswith(".BJ"):
        return raw[:-3], "BJ"
    if raw.endswith(".HK"):
        code = raw[:-3]
        return code.zfill(5) if code.isdigit() else code, "HK"
    return raw, ""


def yahoo_symbol(symbol: str) -> str | None:
    raw = symbol.strip().upper()
    special = {
        "HSI.HK": "^HSI",
    }
    if raw in special:
        return special[raw]
    if raw.endswith(".SH"):
        return raw[:-3] + ".SS"
    if raw.endswith(".SZ"):
        return raw
    if raw.endswith(".HK"):
        return raw
    return None


def eastmoney_secid(item: dict[str, Any]) -> str | None:
    code, exchange = normalize_symbol(item["symbol"])
    if exchange == "SH":
        return f"1.{code}"
    if exchange == "SZ":
        return f"0.{code}"
    if exchange == "HK" and not code.isdigit():
        return None
    if exchange == "HK":
        return f"116.{code}"
    return None


def tencent_query_symbol(item: dict[str, Any]) -> str | None:
    code, exchange = normalize_symbol(item["symbol"])
    if exchange == "SH":
        return f"sh{code}"
    if exchange == "SZ":
        return f"sz{code}"
    if exchange == "HK":
        return f"hk{code}"
    return None


def build_lookup(df: Any, code_col: str = "代码") -> dict[str, dict[str, Any]]:
    lookup: dict[str, dict[str, Any]] = {}
    if df is None:
        return lookup
    for _, row in df.iterrows():
        record = row.to_dict()
        code = str(record.get(code_col, "")).strip().upper()
        if code:
            lookup[code] = record
    return lookup


def build_lookup_from_records(records: list[dict[str, Any]], code_col: str = "代码") -> dict[str, dict[str, Any]]:
    lookup: dict[str, dict[str, Any]] = {}
    for record in records:
        code = str(record.get(code_col, "")).strip().upper()
        if code:
            lookup[code] = record
    return lookup


def first_value(record: dict[str, Any], names: list[str]) -> Any:
    for name in names:
        if name in record and record[name] not in ("", None):
            return record[name]
    return None


def as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(str(value).replace(",", "").replace("%", ""))
    except ValueError:
        return None


def scaled_eastmoney_number(value: Any, scale: float) -> float | None:
    number = as_float(value)
    if number is None:
        return None
    return number / scale


def eastmoney_price_scale(item: dict[str, Any]) -> float:
    _, exchange = normalize_symbol(item["symbol"])
    group = item.get("group")
    if exchange == "HK" or group == "etfs":
        return 1000.0
    return 100.0


def quote_from_record(item: dict[str, Any], record: dict[str, Any], provider: str, source_detail: str) -> dict[str, Any]:
    price = as_float(first_value(record, ["最新价", "最新", "现价", "收盘", "close", "price"]))
    change_pct = as_float(first_value(record, ["涨跌幅", "涨幅", "change_pct", "pct_chg"]))
    amount = as_float(first_value(record, ["成交额", "amount"]))
    volume = as_float(first_value(record, ["成交量", "volume"]))
    name = first_value(record, ["名称", "name", "简称"]) or item.get("name")
    return {
        "symbol": item["symbol"],
        "normalized_code": normalize_symbol(item["symbol"])[0],
        "exchange": normalize_symbol(item["symbol"])[1],
        "market": item.get("market"),
        "group": item.get("group"),
        "name": name,
        "theme": item.get("theme"),
        "price": price,
        "change_pct": change_pct,
        "amount": amount,
        "volume": volume,
        "provider": provider,
        "source_detail": source_detail,
        "price_date": text_price_date(first_value(record, ["日期", "时间", "date", "trade_date", "更新时间"])),
        "fetched_at": now_utc(),
        "data_status": "ok" if price is not None else "missing_price",
    }


def fetch_yahoo_item(item: dict[str, Any], timeout: int) -> dict[str, Any]:
    mapped = yahoo_symbol(item["symbol"])
    if not mapped:
        return {
            "symbol": item["symbol"],
            "data_status": "unsupported_symbol",
            "provider": "Yahoo Finance chart fallback",
        }
    encoded = urllib.parse.quote(mapped, safe="")
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{encoded}?range=5d&interval=1d"
    request = urllib.request.Request(url, headers=REQUEST_HEADERS)
    try:
        with open_quote_url(request, timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        return {
            "symbol": item["symbol"],
            "yahoo_symbol": mapped,
            "data_status": "error",
            "provider": "Yahoo Finance chart fallback",
            "error": str(exc),
        }

    result = (payload.get("chart", {}).get("result") or [None])[0]
    if not result:
        return {
            "symbol": item["symbol"],
            "yahoo_symbol": mapped,
            "data_status": "not_found",
            "provider": "Yahoo Finance chart fallback",
        }
    meta = result.get("meta", {})
    price = meta.get("regularMarketPrice")
    previous = meta.get("chartPreviousClose")
    change_pct = None
    if price is not None and previous:
        try:
            change_pct = (float(price) / float(previous) - 1.0) * 100
        except (TypeError, ValueError, ZeroDivisionError):
            change_pct = None
    code, exchange = normalize_symbol(item["symbol"])
    timestamps = result.get("timestamp") or []
    timezone_name = str(meta.get("exchangeTimezoneName") or "Asia/Shanghai")
    price_date = timestamp_price_date(timestamps[-1], timezone_name) if timestamps else None
    return {
        "symbol": item["symbol"],
        "normalized_code": code,
        "exchange": exchange,
        "market": item.get("market"),
        "group": item.get("group"),
        "name": meta.get("longName") or meta.get("shortName") or item.get("name"),
        "theme": item.get("theme"),
        "price": as_float(price),
        "previous_close": as_float(previous),
        "change_pct": change_pct,
        "currency": meta.get("currency"),
        "provider": "Yahoo Finance chart fallback",
        "source_detail": f"chart/{mapped}",
        "price_date": price_date,
        "fetched_at": now_utc(),
        "data_status": "ok" if price is not None else "missing_price",
    }


def parse_tencent_response(text: str) -> dict[str, list[str]]:
    records: dict[str, list[str]] = {}
    for line in text.splitlines():
        if not line.startswith("v_") or "=\"" not in line:
            continue
        key, value = line.split("=\"", 1)
        query_symbol = key[2:]
        payload = value.rsplit("\"", 1)[0]
        records[query_symbol] = payload.split("~")
    return records


def quote_from_tencent_fields(item: dict[str, Any], query_symbol: str, fields: list[str]) -> dict[str, Any]:
    code, exchange = normalize_symbol(item["symbol"])
    name = fields[1] if len(fields) > 1 and fields[1] else item.get("name")
    price = as_float(fields[3]) if len(fields) > 3 else None
    full_quote = len(fields) > 32
    change_pct = as_float(fields[32]) if full_quote else as_float(fields[5]) if len(fields) > 5 else None
    return {
        "symbol": item["symbol"],
        "normalized_code": code,
        "exchange": exchange,
        "market": item.get("market"),
        "group": item.get("group"),
        "name": name,
        "theme": item.get("theme"),
        "price": price,
        "change": as_float(fields[31]) if full_quote else as_float(fields[4]) if len(fields) > 4 else None,
        "change_pct": change_pct,
        "volume": as_float(fields[36]) if full_quote and len(fields) > 36 else as_float(fields[6]) if len(fields) > 6 else None,
        "amount": as_float(fields[37]) if full_quote and len(fields) > 37 else as_float(fields[7]) if len(fields) > 7 else None,
        "provider": "Tencent quote",
        "source_detail": f"qt.gtimg.cn/{query_symbol}",
        "price_date": text_price_date(fields[30]) if full_quote and len(fields) > 30 else None,
        "fetched_at": now_utc(),
        "data_status": "ok" if price is not None else "missing_price",
    }


def fetch_tencent_batch(items: list[dict[str, Any]], timeout: int) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    query_by_symbol: dict[str, str] = {}
    item_by_query: dict[str, dict[str, Any]] = {}
    for item in items:
        query_symbol = tencent_query_symbol(item)
        if not query_symbol:
            continue
        query_by_symbol[item["symbol"]] = query_symbol
        item_by_query[query_symbol] = item

    if not item_by_query:
        return [], []

    errors: list[dict[str, str]] = []
    quotes: list[dict[str, Any]] = []
    query_symbols = list(item_by_query)
    chunk_size = 60
    for start in range(0, len(query_symbols), chunk_size):
        chunk = query_symbols[start : start + chunk_size]
        encoded = urllib.parse.quote(",".join(chunk), safe=",")
        url = f"https://qt.gtimg.cn/q={encoded}"
        request = urllib.request.Request(url, headers=TENCENT_HEADERS)
        try:
            with open_quote_url(request, timeout) as response:
                raw = response.read()
        except Exception as exc:
            errors.append({"scope": "tencent_quote", "error": str(exc)})
            continue

        try:
            text = raw.decode("gbk")
        except UnicodeDecodeError:
            text = raw.decode("utf-8", errors="replace")
        records = parse_tencent_response(text)
        for query_symbol in chunk:
            item = item_by_query[query_symbol]
            fields = records.get(query_symbol)
            if not fields:
                quotes.append(
                    {
                        "symbol": item["symbol"],
                        "provider": "Tencent quote",
                        "source_detail": f"qt.gtimg.cn/{query_symbol}",
                        "data_status": "not_found",
                    }
                )
                continue
            quotes.append(quote_from_tencent_fields(item, query_symbol, fields))

    for symbol, query_symbol in query_by_symbol.items():
        if not any(quote.get("symbol") == symbol for quote in quotes):
            quotes.append({"symbol": symbol, "provider": "Tencent quote", "source_detail": f"qt.gtimg.cn/{query_symbol}", "data_status": "not_found"})
    return quotes, errors


def fetch_eastmoney_item(item: dict[str, Any], timeout: int) -> dict[str, Any]:
    secid = eastmoney_secid(item)
    if not secid:
        return {
            "symbol": item["symbol"],
            "data_status": "unsupported_symbol",
            "provider": "Eastmoney quote",
        }
    query = f"secid={urllib.parse.quote(secid, safe='')}&fields={EASTMONEY_FIELDS}"
    url = f"https://push2.eastmoney.com/api/qt/stock/get?{query}"
    request = urllib.request.Request(url, headers=REQUEST_HEADERS)
    try:
        with open_quote_url(request, timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        return {
            "symbol": item["symbol"],
            "eastmoney_secid": secid,
            "data_status": "error",
            "provider": "Eastmoney quote",
            "error": str(exc),
        }
    data = payload.get("data") or {}
    if payload.get("rc") != 0 or not data:
        return {
            "symbol": item["symbol"],
            "eastmoney_secid": secid,
            "data_status": "not_found",
            "provider": "Eastmoney quote",
            "error": f"rc={payload.get('rc')}",
        }
    scale = eastmoney_price_scale(item)
    price = scaled_eastmoney_number(data.get("f43"), scale)
    previous = scaled_eastmoney_number(data.get("f60"), scale)
    change_pct = None
    if price is not None and previous:
        try:
            change_pct = (price / previous - 1.0) * 100
        except (TypeError, ValueError, ZeroDivisionError):
            change_pct = None
    if change_pct is None:
        change_pct = scaled_eastmoney_number(data.get("f170"), 100.0)
    code, exchange = normalize_symbol(item["symbol"])
    return {
        "symbol": item["symbol"],
        "normalized_code": code,
        "exchange": exchange,
        "market": item.get("market"),
        "group": item.get("group"),
        "name": data.get("f58") or item.get("name"),
        "theme": item.get("theme"),
        "price": price,
        "previous_close": previous,
        "change_pct": change_pct,
        "amount": as_float(data.get("f48")),
        "volume": as_float(data.get("f47")),
        "provider": "Eastmoney quote",
        "source_detail": f"push2.eastmoney.com/{secid}",
        "price_date": timestamp_price_date(data.get("f86")),
        "fetched_at": now_utc(),
        "data_status": "ok" if price is not None else "missing_price",
    }


def apply_yahoo_fallback(result: dict[str, Any], config: dict[str, Any], timeout: int, max_workers: int) -> dict[str, Any]:
    items_by_symbol = {item["symbol"]: item for item in all_watchlist_items(config)}
    current_items = result.get("items", [])
    current_by_symbol = {item.get("symbol"): item for item in current_items}
    missing = [
        items_by_symbol[symbol]
        for symbol, current in current_by_symbol.items()
        if current.get("price") is None and symbol in items_by_symbol
    ]
    if not current_items:
        missing = all_watchlist_items(config)
        current_by_symbol = {}

    fallback_items: list[dict[str, Any]] = []
    if missing:
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [executor.submit(fetch_yahoo_item, item, timeout) for item in missing]
            for future in concurrent.futures.as_completed(futures):
                fallback_items.append(future.result())

    for fallback in fallback_items:
        symbol = fallback.get("symbol")
        if fallback.get("price") is not None and symbol in current_by_symbol:
            current_by_symbol[symbol].update(fallback)
        elif symbol not in current_by_symbol:
            current_by_symbol[symbol] = fallback
        elif fallback.get("error"):
            result.setdefault("errors", []).append({"scope": f"yahoo_fallback:{symbol}", "error": fallback["error"]})

    ordered = []
    for item in all_watchlist_items(config):
        symbol = item["symbol"]
        if symbol in current_by_symbol:
            ordered.append(current_by_symbol[symbol])
    result["items"] = ordered
    result["fallback_provider"] = "Yahoo Finance chart"
    return result


def apply_tencent_fallback(result: dict[str, Any], config: dict[str, Any], timeout: int) -> dict[str, Any]:
    items_by_symbol = {item["symbol"]: item for item in all_watchlist_items(config)}
    current_items = result.get("items", [])
    current_by_symbol = {item.get("symbol"): item for item in current_items}
    missing = [
        items_by_symbol[symbol]
        for symbol, current in current_by_symbol.items()
        if current.get("price") is None and symbol in items_by_symbol
    ]
    if not current_items:
        missing = all_watchlist_items(config)
        current_by_symbol = {}

    fallback_items, errors = fetch_tencent_batch(missing, timeout) if missing else ([], [])
    filled = 0
    missing_after = 0
    for fallback in fallback_items:
        symbol = fallback.get("symbol")
        if fallback.get("price") is not None and symbol in current_by_symbol:
            current_by_symbol[symbol].update(fallback)
            filled += 1
        elif fallback.get("price") is not None:
            current_by_symbol[symbol] = fallback
            filled += 1
        elif symbol not in current_by_symbol:
            current_by_symbol[symbol] = fallback
            missing_after += 1
        else:
            missing_after += 1

    ordered = []
    for item in all_watchlist_items(config):
        symbol = item["symbol"]
        if symbol in current_by_symbol:
            ordered.append(current_by_symbol[symbol])
    result["items"] = ordered
    result.setdefault("structured_fallbacks", []).append(
        {
            "provider": "Tencent quote",
            "attempted": len(missing),
            "items": filled,
            "missing": missing_after,
            "errors": len(errors),
            "sample_errors": errors[:3],
        }
    )
    return result


def apply_eastmoney_fallback(result: dict[str, Any], config: dict[str, Any], timeout: int, max_workers: int) -> dict[str, Any]:
    items_by_symbol = {item["symbol"]: item for item in all_watchlist_items(config)}
    current_items = result.get("items", [])
    current_by_symbol = {item.get("symbol"): item for item in current_items}
    missing = [
        items_by_symbol[symbol]
        for symbol, current in current_by_symbol.items()
        if current.get("price") is None and symbol in items_by_symbol
    ]
    if not current_items:
        missing = all_watchlist_items(config)
        current_by_symbol = {}

    fallback_items: list[dict[str, Any]] = []
    if missing:
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [executor.submit(fetch_eastmoney_item, item, timeout) for item in missing]
            for future in concurrent.futures.as_completed(futures):
                fallback_items.append(future.result())

    filled = 0
    error_samples: list[dict[str, str]] = []
    error_count = 0
    for fallback in fallback_items:
        symbol = fallback.get("symbol")
        if fallback.get("price") is not None and symbol in current_by_symbol:
            current_by_symbol[symbol].update(fallback)
            filled += 1
        elif fallback.get("price") is not None:
            current_by_symbol[symbol] = fallback
            filled += 1
        elif fallback.get("error"):
            error_count += 1
            if len(error_samples) < 3:
                error_samples.append({"scope": f"eastmoney_quote:{symbol}", "error": fallback["error"]})

    ordered = []
    for item in all_watchlist_items(config):
        symbol = item["symbol"]
        if symbol in current_by_symbol:
            ordered.append(current_by_symbol[symbol])
    result["items"] = ordered
    if filled or error_count:
        result.setdefault("structured_fallbacks", []).append(
            {
                "provider": "Eastmoney quote",
                "attempted": len(fallback_items),
                "items": filled,
                "errors": error_count,
                "sample_errors": error_samples,
            }
        )
    return result


DATASET_FUNCTIONS = {
    "a_share_spot": "stock_zh_a_spot_em",
    "etf_spot": "fund_etf_spot_em",
    "hk_spot": "stock_hk_spot_em",
    "index_spot": "stock_zh_index_spot_em",
}


def dataset_worker(dataset: str, queue: mp.Queue) -> None:
    try:
        import akshare as ak  # type: ignore
    except Exception as exc:
        queue.put({"dataset": dataset, "records": [], "error": f"import failed: {exc}", "install_hint": "python -m pip install akshare"})
        return

    try:
        func_name = DATASET_FUNCTIONS[dataset]
        df = getattr(ak, func_name)()
        queue.put({"dataset": dataset, "records": df.to_dict(orient="records"), "error": None})
    except Exception as exc:
        queue.put({"dataset": dataset, "records": [], "error": str(exc)})


def fetch_dataset(dataset: str, timeout: int) -> dict[str, Any]:
    ctx = mp.get_context("spawn")
    queue: mp.Queue = ctx.Queue()
    proc = ctx.Process(target=dataset_worker, args=(dataset, queue))
    proc.start()
    proc.join(timeout)
    if proc.is_alive():
        proc.terminate()
        proc.join(2)
        return {"dataset": dataset, "records": [], "error": f"timed out after {timeout}s"}
    if not queue.empty():
        return queue.get()
    return {"dataset": dataset, "records": [], "error": f"worker exited with code {proc.exitcode}"}


def fetch_with_akshare(config: dict[str, Any], dataset_timeout: int) -> dict[str, Any]:
    output = {"provider": "AKShare", "items": [], "errors": []}
    items = all_watchlist_items(config)
    groups = {item["symbol"]: item for item in items}

    datasets = {name: fetch_dataset(name, dataset_timeout) for name in DATASET_FUNCTIONS}
    for name, result in datasets.items():
        if result.get("error"):
            error = {"scope": name, "error": result["error"]}
            if result.get("install_hint"):
                error["install_hint"] = result["install_hint"]
            output["errors"].append(error)

    a_stock_lookup = build_lookup_from_records(datasets["a_share_spot"].get("records", []))
    etf_lookup = build_lookup_from_records(datasets["etf_spot"].get("records", []))
    hk_lookup = build_lookup_from_records(datasets["hk_spot"].get("records", []))
    index_lookup = build_lookup_from_records(datasets["index_spot"].get("records", []))

    for symbol, item in groups.items():
        code, exchange = normalize_symbol(symbol)
        record = None
        source_detail = ""
        group = item.get("group")
        if group == "indices":
            record = index_lookup.get(code)
            source_detail = "stock_zh_index_spot_em"
        elif group == "etfs":
            record = etf_lookup.get(code) or a_stock_lookup.get(code)
            source_detail = "fund_etf_spot_em/stock_zh_a_spot_em"
        elif exchange == "HK":
            record = hk_lookup.get(code) or hk_lookup.get(code.lstrip("0"))
            source_detail = "stock_hk_spot_em"
        else:
            record = a_stock_lookup.get(code)
            source_detail = "stock_zh_a_spot_em"

        if record:
            output["items"].append(quote_from_record(item, record, "AKShare", source_detail))
        else:
            output["items"].append(
                {
                    "symbol": symbol,
                    "normalized_code": code,
                    "exchange": exchange,
                    "market": item.get("market"),
                    "group": group,
                    "name": item.get("name"),
                    "theme": item.get("theme"),
                    "price": None,
                    "provider": "AKShare",
                    "source_detail": source_detail,
                    "fetched_at": now_utc(),
                    "data_status": "not_found",
                }
            )
    return output


def fetch_with_tencent(config: dict[str, Any], timeout: int) -> dict[str, Any]:
    """Fetch the bounded watchlist directly instead of timing out on full-market tables."""
    watchlist = all_watchlist_items(config)
    quotes, errors = fetch_tencent_batch(watchlist, timeout)
    return {
        "provider": "Tencent quote",
        "primary_provider": "Tencent quote",
        "items": quotes,
        "errors": errors,
        "provider_attempts": [
            {
                "provider": "Tencent quote",
                "role": "primary",
                "attempted": len(watchlist),
                "items": sum(1 for quote in quotes if quote.get("price") is not None),
                "errors": len(errors),
            }
        ],
    }


def snapshot(
    timeout: int,
    dry_run: bool,
    dataset_timeout: int,
    tencent_timeout: int,
    use_tencent: bool,
    eastmoney_timeout: int,
    eastmoney_workers: int,
    yahoo_timeout: int,
    yahoo_workers: int,
    use_eastmoney: bool,
    watchlist_path: Path = WATCHLIST_PATH,
) -> dict[str, Any]:
    config = load_watchlist(watchlist_path)
    if dry_run:
        return {
            "generated_at": now_utc(),
            "watchlist_path": str(watchlist_path),
            "items": [
                {
                    "symbol": item["symbol"],
                    "name": item.get("name"),
                    "market": item.get("market"),
                    "group": item.get("group"),
                    "theme": item.get("theme"),
                    "data_status": "dry_run",
                }
                for item in all_watchlist_items(config)
            ],
            "errors": [],
        }

    bounded_dataset_timeout = max(3, min(dataset_timeout, timeout))
    configured_primary = str(config.get("data_sources", {}).get("primary") or "AKShare").lower()
    if use_tencent and configured_primary.startswith("tencent"):
        result = fetch_with_tencent(config, timeout=tencent_timeout)
    else:
        result = fetch_with_akshare(config, bounded_dataset_timeout)
        if use_tencent:
            result = apply_tencent_fallback(result, config, timeout=tencent_timeout)
    if use_eastmoney:
        result = apply_eastmoney_fallback(result, config, timeout=eastmoney_timeout, max_workers=eastmoney_workers)
    result = apply_yahoo_fallback(result, config, timeout=yahoo_timeout, max_workers=yahoo_workers)
    result["generated_at"] = now_utc()
    result["watchlist_path"] = str(watchlist_path)
    result["dataset_timeout_seconds"] = bounded_dataset_timeout
    result["configured_primary_provider"] = config.get("data_sources", {}).get("primary")
    result["tencent_enabled"] = use_tencent
    result["eastmoney_enabled"] = use_eastmoney
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fetch China A-share/HK market watchlist snapshots.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    snapshot_parser = subparsers.add_parser("snapshot", help="Fetch or dry-run the China market watchlist.")
    snapshot_parser.add_argument("--watchlist", type=Path, default=WATCHLIST_PATH)
    snapshot_parser.add_argument("--timeout", type=int, default=45)
    snapshot_parser.add_argument("--dataset-timeout", type=int, default=12)
    snapshot_parser.add_argument("--tencent-timeout", type=int, default=8)
    snapshot_parser.add_argument("--no-tencent", action="store_true")
    snapshot_parser.add_argument("--eastmoney-timeout", type=int, default=8)
    snapshot_parser.add_argument("--eastmoney-workers", type=int, default=8)
    snapshot_parser.add_argument("--no-eastmoney", action="store_true")
    snapshot_parser.add_argument("--yahoo-timeout", type=int, default=8)
    snapshot_parser.add_argument("--yahoo-workers", type=int, default=8)
    snapshot_parser.add_argument("--dry-run", action="store_true")
    snapshot_parser.add_argument("--output", type=Path, default=None)

    args = parser.parse_args(argv)
    if args.command == "snapshot":
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        result = snapshot(
            timeout=args.timeout,
            dry_run=args.dry_run,
            dataset_timeout=args.dataset_timeout,
            tencent_timeout=args.tencent_timeout,
            use_tencent=not args.no_tencent,
            eastmoney_timeout=args.eastmoney_timeout,
            eastmoney_workers=args.eastmoney_workers,
            yahoo_timeout=args.yahoo_timeout,
            yahoo_workers=args.yahoo_workers,
            use_eastmoney=not args.no_eastmoney,
            watchlist_path=args.watchlist,
        )
        output_path = args.output or DATA_DIR / f"china-market-snapshot-{current_report_date()}.json"
        output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"items={len(result.get('items', []))} errors={len(result.get('errors', []))}")
        print(output_path)
        return 0
    return 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
