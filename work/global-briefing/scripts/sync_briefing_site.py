#!/usr/bin/env python3
"""Incrementally sync the newest dated briefing into the ATLAS site.

The command is intentionally two-phase:
  1. Run without --mark-deployed to detect a content hash change and generate
     src/app/briefing.generated.json.
  2. After a successful Sites deployment, call --mark-deployed <sha256>.

This keeps failed deployments retryable and makes unchanged checks a no-op.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from difflib import SequenceMatcher
from datetime import date as Date
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parents[3]
OUTPUTS = ROOT / "outputs"
DATA_DIR = ROOT / "work" / "global-briefing" / "data"
SITE_DATA = ROOT / "src" / "app" / "briefing.generated.json"
STATE_FILE = ROOT / "work" / "global-briefing" / "data" / "site-sync-state.json"
REPORT_RE = re.compile(r"每日全球晨间简报-(\d{4}-\d{2}-\d{2})\.md$")
SITE_SCHEMA_VERSION = 4
PREDICTIONS_PATH = DATA_DIR / "predictions.jsonl"
EVOLUTION_STATE_PATH = DATA_DIR / "evolution_state.json"
SOURCES_CONFIG_PATH = ROOT / "work" / "global-briefing" / "config" / "sources.json"
SETTINGS_CONFIG_PATH = ROOT / "work" / "global-briefing" / "config" / "settings.json"
PAPER_CONFIG_PATH = ROOT / "work" / "global-briefing" / "config" / "paper_trading.json"
ATLAS_RUNTIME_ROOT = ROOT / "work" / "shared" / "atlas"
ATLAS_CYCLE_STATE = ATLAS_RUNTIME_ROOT / "cycle_state.json"
ATLAS_LEDGER_STATE = ATLAS_RUNTIME_ROOT / "virtual_execution" / "atlas_virtual_execution_state.json"
ATLAS_LEDGER_AUDIT = ATLAS_RUNTIME_ROOT / "virtual_execution" / "atlas_virtual_execution_audit.json"
ATLAS_SELF_HEALING_LATEST = ATLAS_RUNTIME_ROOT / "self_healing" / "latest.json"
ATLAS_IMPROVEMENTS_LATEST = ATLAS_RUNTIME_ROOT / "improvements" / "latest.json"


def compact(value: str, limit: int = 280) -> str:
    value = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1", value)
    value = value.replace("**", "").replace("`", "")
    value = re.sub(r"<[^>]+>", "", value)
    value = re.sub(r"\s+", " ", value).strip(" -–—\t\r\n")
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip("，。；;,. ") + "…"


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def valid_iso_date(value: str) -> str:
    try:
        parsed = Date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"Invalid date {value!r}; expected YYYY-MM-DD.") from exc
    canonical = parsed.isoformat()
    if value != canonical:
        raise argparse.ArgumentTypeError(f"Invalid date {value!r}; expected {canonical}.")
    return canonical


def report_for_date(requested_date: str | None = None) -> tuple[Path, str]:
    if requested_date:
        path = OUTPUTS / f"每日全球晨间简报-{requested_date}.md"
        if not path.is_file():
            raise FileNotFoundError(f"No briefing report found for {requested_date}: {path}")
        return path, requested_date

    candidates: list[tuple[str, Path]] = []
    for path in OUTPUTS.glob("每日全球晨间简报-*.md"):
        match = REPORT_RE.search(path.name)
        if not match:
            continue
        try:
            date_value = Date.fromisoformat(match.group(1)).isoformat()
        except ValueError:
            continue
        candidates.append((date_value, path))
    if not candidates:
        raise FileNotFoundError(f"No dated briefing found in {OUTPUTS}")
    date, path = max(candidates, key=lambda pair: pair[0])
    return path, date


def split_sections(text: str) -> dict[str, list[str]]:
    sections: dict[str, list[str]] = {}
    heading_levels = [len(match.group(1)) for line in text.splitlines() if (match := re.match(r"^(#{2,3})\s+", line))]
    section_level = min(heading_levels) if heading_levels else 2
    current = ""
    for line in text.splitlines():
        heading = re.match(r"^(#{2,3})\s+(.+)", line)
        if heading and len(heading.group(1)) == section_level:
            current = compact(heading.group(2), 100)
            sections[current] = []
        elif current:
            sections[current].append(line)
    return sections


def find_section(sections: dict[str, list[str]], *needles: str) -> list[str]:
    for title, lines in sections.items():
        normalized = title.replace(" ", "").lower()
        if any(needle.replace(" ", "").lower() in normalized for needle in needles):
            return lines
    return []


def numbered_items(lines: list[str]) -> list[str]:
    items: list[str] = []
    current: list[str] = []
    for line in lines:
        if re.match(r"^\s*\d+[\.、]\s+", line):
            if current:
                items.append(" ".join(current))
            current = [re.sub(r"^\s*\d+[\.、]\s+", "", line).strip()]
        elif current and line.strip() and not line.lstrip().startswith("|"):
            current.append(line.strip())
    if current:
        items.append(" ".join(current))
    return items


def bullet_items(lines: list[str]) -> list[str]:
    items: list[str] = []
    current: list[str] = []
    for line in lines:
        if re.match(r"^\s*[-*]\s+", line):
            if current:
                items.append(" ".join(current))
            current = [re.sub(r"^\s*[-*]\s+", "", line).strip()]
        elif current and line.strip() and not line.startswith("###") and not line.lstrip().startswith("|"):
            current.append(line.strip())
    if current:
        items.append(" ".join(current))
    return items


def markdown_links(text: str, limit: int = 12) -> list[dict[str, str]]:
    seen: set[str] = set()
    links: list[dict[str, str]] = []
    for label, href in re.findall(r"\[([^\]]+)\]\((https?://[^)]+)\)", text):
        if href in seen:
            continue
        seen.add(href)
        links.append({"label": compact(label, 48), "href": href})
        if len(links) >= limit:
            break
    return links


def plain_markdown(value: str, limit: int = 420) -> str:
    value = re.sub(r"\[([^\]]+)\]\((?:https?://[^)]+)\)", r"\1", value)
    return compact(value, limit)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return rows
    for line in lines:
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            rows.append(payload)
    return rows


def predictions_for_date(report_date: str) -> list[dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    for row in read_jsonl(PREDICTIONS_PATH):
        if str(row.get("date")) != report_date:
            continue
        if not all(row.get(field) for field in ("prediction_id", "scenario", "horizon", "probability")):
            continue
        by_id[str(row["prediction_id"])] = row
    rows = list(by_id.values())
    order = {"1d": 0, "1w": 1, "1m": 2}
    return sorted(rows, key=lambda row: (order.get(str(row.get("horizon")), 9), str(row.get("prediction_id", ""))))


def probability_label(value: Any) -> str:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        probability = float(value)
        return "高" if probability >= 0.7 else "低" if probability < 0.5 else "中"
    normalized = str(value or "medium").lower()
    return "高" if normalized in {"high", "高"} else "低" if normalized in {"low", "低"} else "中"


def horizon_label(value: Any) -> str:
    return {"1d": "1日", "1w": "1周", "1m": "1个月"}.get(str(value), str(value or "待定"))


EVENT_PREDICTION_SUFFIXES = {
    "地缘": ("P01",),
    "安全": ("P03",),
    "科技": ("P02",),
    "气候": ("P05",),
    "社会": ("P05",),
    "宏观": ("P01", "P04", "P06"),
    "中国": ("P04", "P06"),
}


def prediction_source_refs(prediction: dict[str, Any]) -> list[dict[str, str]]:
    """Return the prediction's own evidence links without inferring sources."""
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for evidence in prediction.get("evidence", []):
        if not isinstance(evidence, dict):
            continue
        href = str(evidence.get("url") or "").strip()
        if not re.match(r"^https?://", href, re.IGNORECASE) or href in seen:
            continue
        seen.add(href)
        result.append(
            {
                "label": compact(str(evidence.get("source") or "来源"), 48),
                "href": href,
            }
        )
    return result


def matching_predictions(event: dict[str, Any], predictions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    event_hrefs = {
        str(source.get("href") or "")
        for source in event.get("sources", [])
        if isinstance(source, dict) and source.get("href")
    }
    evidence_matches: list[tuple[int, str, dict[str, Any]]] = []
    for prediction in predictions:
        if prediction.get("schema_version") != 2:
            continue
        evidence_hrefs = {source["href"] for source in prediction_source_refs(prediction)}
        overlap = len(event_hrefs & evidence_hrefs)
        if overlap:
            evidence_matches.append((overlap, str(prediction.get("prediction_id") or ""), prediction))
    if evidence_matches:
        evidence_matches.sort(key=lambda item: (-item[0], item[1]))
        return [prediction for _overlap, _prediction_id, prediction in evidence_matches]

    # Legacy records do not have the v2 evidence contract. Preserve the old
    # category-to-suffix mapping for historical site regeneration only.
    suffixes = EVENT_PREDICTION_SUFFIXES.get(str(event.get("category")), ())
    explicit = [
        prediction
        for suffix in suffixes
        for prediction in predictions
        if prediction.get("schema_version") != 2
        if str(prediction.get("prediction_id") or "").endswith(suffix)
    ]
    if explicit:
        return explicit
    return []


def prediction_drivers(prediction: dict[str, Any], fallback: list[str] | None = None) -> list[str]:
    drivers = [compact(str(value), 180) for value in prediction.get("drivers", []) if str(value).strip()]
    if drivers:
        return drivers
    trigger = compact(str(prediction.get("trigger") or ""), 180)
    if trigger:
        return [trigger]
    return [compact(str(value), 180) for value in (fallback or []) if str(value).strip()][:3]


def fallback_event_sources(category: str, all_sources: list[dict[str, str]]) -> list[dict[str, str]]:
    keywords = {
        "社会": ("NCA", "热浪", "洪涝", "儿童", "电网"),
        "中国": ("中国政府", "新华", "人民币", "A股", "PBOC"),
        "宏观": ("霍尔木兹", "人民币", "A股", "PBOC"),
    }.get(category, ())
    selected = [source for source in all_sources if any(keyword.lower() in source["label"].lower() for keyword in keywords)]
    return selected[:5]


def instrument_rows(prediction: dict[str, Any]) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    for item in prediction.get("tickers", []):
        if not isinstance(item, dict) or not item.get("symbol"):
            continue
        result.append(
            {
                "symbol": str(item.get("symbol")),
                "exchange": str(item.get("exchange") or ""),
                "type": str(item.get("instrument_type") or "观察标的"),
                "thesis": compact(str(item.get("thesis") or "等待进一步验证"), 180),
                "risk": compact(str(item.get("risk") or "存在价格与执行偏差风险"), 180),
            }
        )
    return result


def event_from_item(item: str, category: str, time_label: str) -> dict[str, str]:
    link = re.search(r"\[([^\]]+)\]\((https?://[^)]+)\)", item)
    bold = re.search(r"\*\*([^*]+)\*\*", item)
    content_only = re.sub(r"\[([^\]]+)\]\((?:https?://[^)]+)\)", "", item)
    clean = compact(content_only, 900)
    title = compact(bold.group(1) if bold else re.split(r"[。！？]", clean, maxsplit=1)[0], 72)
    remainder = clean
    if title and remainder.startswith(title):
        remainder = remainder[len(title) :].lstrip("：:。 ")
    if len(title.rstrip("：:")) < 10 and remainder:
        first_clause = re.split(r"[。；;]", remainder, maxsplit=1)[0]
        title = compact(f"{title.rstrip('：:')}：{first_clause}", 58)
    sentences = [s.strip() for s in re.split(r"(?<=[。！？])", remainder) if s.strip()]
    implication = ""
    for sentence in sentences:
        if any(token in sentence for token in ("分析判断", "后续影响", "为什么重要", "验证")):
            implication = compact(re.sub(r"^(分析判断|后续影响|为什么重要)[：:]?", "", sentence), 260)
            break
    if not implication and len(remainder) <= 400:
        clauses = [compact(value, 260) for value in re.split(r"[；;]", remainder) if compact(value, 260)]
        if len(clauses) > 1:
            implication = clauses[-1]
    if not implication:
        implication = {
            "地缘": "关注外交表态是否转化为可核验的停火、航运与能源价格变化。",
            "安全": "关注设施损失、正式采购和交付节奏是否改变供应与防务资产定价。",
            "科技": "关注资本开支、基础设施约束和监管变化能否转化为订单与现金流。",
            "气候": "关注极端天气是否进一步影响电网、保险、农业与公共卫生数据。",
            "宏观": "关注价格、利率、汇率与库存数据能否确认当前市场方向。",
            "中国": "关注成交宽度、政策执行与盈利数据能否确认结构性行情。",
        }.get(category, "关注下一项可核验的执行与价格信号。")
    body = compact("".join(sentences[:2]) if sentences else remainder, 320)
    if len(body) < 12:
        body = compact(clean, 230)
    return {
        "category": category,
        "time": time_label,
        "title": title or f"{category}关键更新",
        "cardTitle": compact(title or f"{category}关键更新", 78),
        "body": body or "本期报告已记录新的事实、影响路径与后续验证点。",
        "implication": implication,
        "source": compact(link.group(1), 30) if link else "查看来源",
        "href": link.group(2) if link else "#sources",
    }


def parse_events(sections: dict[str, list[str]], predictions: list[dict[str, Any]], all_sources: list[dict[str, str]]) -> list[dict[str, Any]]:
    specs = [
        (("政治和外交", "政治与外交"), "地缘", "首要变量"),
        (("军事和安全", "军事与安全"), "安全", "一周窗口"),
        (("科技和AI", "科技与AI"), "科技", "结构迁移"),
        (("环境和气候", "环境与气候"), "气候", "复合冲击"),
        (("经济和能源", "经济与能源"), "宏观", "价格验证"),
        (("公共卫生和社会", "公共卫生与社会"), "社会", "中期影响"),
        (("中国市场映射",), "中国", "结构分化"),
    ]
    events: list[dict[str, Any]] = []
    for aliases, category, label in specs:
        lines = find_section(sections, *aliases)
        items = numbered_items(lines) or bullet_items(lines)
        if not items and lines:
            prose = compact(" ".join(line for line in lines if line.strip() and not line.startswith("###")), 700)
            items = [prose] if prose else []
        if items:
            event: dict[str, Any] = event_from_item(items[0], category, label)
            event["facts"] = [plain_markdown(item) for item in items[:3] if plain_markdown(item)]
            event["sources"] = markdown_links("\n".join(lines), limit=8) or fallback_event_sources(category, all_sources)
            events.append(event)
    if len(events) < 7:
        core = numbered_items(find_section(sections, "核心摘要"))
        for item in core:
            if len(events) >= 7:
                break
            candidate = event_from_item(item, "重点", "今日更新")
            if all(candidate["title"] != existing["title"] for existing in events):
                candidate["facts"] = [plain_markdown(item)]
                candidate["sources"] = markdown_links(item)
                events.append(candidate)
    for event in events:
        related_predictions = matching_predictions(event, predictions)
        # An event may legitimately cite evidence from multiple forecasts. It
        # becomes a primary forecast detail only when the evidence link is unique.
        prediction = related_predictions[0] if len(related_predictions) == 1 else None
        if prediction:
            event.update(
                {
                    "predictionId": str(prediction.get("prediction_id") or ""),
                    "relatedPredictionIds": [str(item.get("prediction_id") or "") for item in related_predictions],
                    "analysis": compact(str(prediction.get("scenario") or event["implication"]), 260),
                    "confidence": probability_label(prediction.get("probability")),
                    "horizon": horizon_label(prediction.get("horizon")),
                    "drivers": prediction_drivers(prediction, event.get("facts", [])),
                    "beneficiaries": [compact(str(value), 100) for value in prediction.get("beneficiaries", []) if str(value).strip()],
                    "pressures": [compact(str(value), 100) for value in prediction.get("pressure", []) if str(value).strip()],
                    "verificationSignals": [compact(str(value), 180) for value in prediction.get("verification_signals", []) if str(value).strip()],
                    "instruments": instrument_rows(prediction),
                }
            )
        else:
            event.update(
                {
                    "predictionId": "",
                    "relatedPredictionIds": [str(item.get("prediction_id") or "") for item in related_predictions],
                    "analysis": event["implication"],
                    "confidence": "待验证",
                    "horizon": event["time"],
                    "drivers": event.get("facts", [])[:3],
                    "beneficiaries": [],
                    "pressures": [],
                    "verificationSignals": [event["implication"]],
                    "instruments": [],
                }
            )
    return events[:7]


def parse_scenarios(lines: list[str]) -> list[dict[str, str]]:
    rows = []
    for line in lines:
        if not line.strip().startswith("|"):
            continue
        cells = [compact(cell, 180) for cell in line.strip().strip("|").split("|")]
        if not cells or all(re.fullmatch(r"[-: ]+", cell or "-") for cell in cells):
            continue
        rows.append(cells)
    if len(rows) < 2:
        result = []
        horizon = "待定"
        for line in lines:
            heading = re.match(r"^###\s+(.+)", line.strip())
            if heading:
                horizon = compact(heading.group(1), 20)
                continue
            if not re.match(r"^\s*[-*]\s+", line):
                continue
            item = re.sub(r"^\s*[-*]\s+", "", line).strip()
            bold = re.search(r"\*\*([^*]+)\*\*", item)
            title = compact(bold.group(1) if bold else re.split(r"[：:]", item, maxsplit=1)[0], 80)
            chance_match = re.search(r"[（(](高|中|低|high|medium|low)[）)]", title, re.I)
            chance_raw = chance_match.group(1).lower() if chance_match else "medium"
            chance = "高" if chance_raw in ("高", "high") else "低" if chance_raw in ("低", "low") else "中"
            title = re.sub(r"[（(](?:高|中|低|high|medium|low)[）)]", "", title, flags=re.I).strip()
            verification = re.search(r"验证(?:信号)?[：:]([^。]+)", compact(item, 500))
            watch = compact(verification.group(1), 100) if verification else "等待执行、价格与官方数据确认"
            result.append({"horizon": horizon, "scenario": title, "chance": chance, "watch": watch})
        return result[:5]
    header = [cell.lower() for cell in rows[0]]

    def column(names: tuple[str, ...], fallback: int) -> int:
        for index, value in enumerate(header):
            if any(name.lower() in value for name in names):
                return index
        return min(fallback, len(header) - 1)

    horizon_i = column(("期限", "horizon"), 0)
    scenario_i = column(("场景", "判断", "scenario"), 1)
    chance_i = column(("概率", "confidence"), 3)
    watch_i = column(("验证", "signal"), len(header) - 1)
    result = []
    for cells in rows[1:]:
        if len(cells) <= max(horizon_i, scenario_i, chance_i, watch_i):
            continue
        chance_raw = cells[chance_i].lower()
        chance = "高" if "high" in chance_raw or "高" in chance_raw else "中" if "medium" in chance_raw or "中" in chance_raw else "低"
        result.append({
            "horizon": cells[horizon_i],
            "scenario": cells[scenario_i],
            "chance": chance,
            "watch": cells[watch_i],
        })
    return result[:5]


def scenarios_from_predictions(predictions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for prediction in predictions:
        result.append(
            {
                "id": str(prediction.get("prediction_id") or f"scenario-{len(result) + 1}"),
                "horizon": horizon_label(prediction.get("horizon")),
                "scenario": compact(str(prediction.get("scenario") or "待验证场景"), 180),
                "chance": probability_label(prediction.get("probability")),
                "status": str(prediction.get("status") or "open"),
                "drivers": prediction_drivers(prediction),
                "beneficiaries": [compact(str(value), 100) for value in prediction.get("beneficiaries", []) if str(value).strip()],
                "pressures": [compact(str(value), 100) for value in prediction.get("pressure", []) if str(value).strip()],
                "verificationSignals": [compact(str(value), 180) for value in prediction.get("verification_signals", []) if str(value).strip()],
                "watch": compact(str((prediction.get("verification_signals") or ["等待执行、价格与官方数据确认"])[0]), 180),
                "instruments": instrument_rows(prediction),
                "sourceRefs": prediction_source_refs(prediction),
            }
        )
    return result


def snapshot_path(prefix: str, report_date: str) -> Path | None:
    exact = DATA_DIR / f"{prefix}-{report_date}.json"
    if exact.exists():
        return exact
    candidates: list[tuple[str, Path]] = []
    for path in DATA_DIR.glob(f"{prefix}-*.json"):
        match = re.search(r"(\d{4}-\d{2}-\d{2})\.json$", path.name)
        if match and match.group(1) <= report_date:
            candidates.append((match.group(1), path))
    return max(candidates, key=lambda item: item[0])[1] if candidates else None


def latest_snapshot(prefix: str, report_date: str) -> dict[str, Any]:
    path = snapshot_path(prefix, report_date)
    return load_json(path, {}) if path else {}


def parse_markets(report_date: str) -> dict[str, Any]:
    us_snapshot = latest_snapshot("market-snapshot", report_date)
    china_snapshot = latest_snapshot("china-market-snapshot", report_date)
    us_preferred = ["SPY", "QQQ", "SMH", "CIBR", "GLD"]
    us_map = {str(item.get("ticker")): item for item in us_snapshot.get("items", [])}
    us_items = []
    for ticker in us_preferred:
        item = us_map.get(ticker)
        if item:
            change = float(item.get("change_pct") or 0)
            us_items.append({"label": ticker, "change": f"{change:+.2f}%", "direction": "up" if change > 0 else "down" if change < 0 else "flat"})
    china_preferred = ["000300.SH", "399006.SZ", "000688.SH", "HSI.HK", "HSTECH.HK"]
    china_map = {str(item.get("symbol")): item for item in china_snapshot.get("items", [])}
    china_items = []
    for symbol in china_preferred:
        item = china_map.get(symbol)
        if item:
            change = float(item.get("change_pct") or 0)
            china_items.append({"label": str(item.get("name") or symbol).replace("指数", ""), "change": f"{change:+.2f}%", "direction": "up" if change > 0 else "down" if change < 0 else "flat"})
    us_date = next((str(item.get("price_date")) for item in us_snapshot.get("items", []) if item.get("price_date")), report_date)
    china_time = str(china_snapshot.get("generated_at") or report_date)[:10]
    china_note = "结构化行情源可用。"
    if china_snapshot.get("errors"):
        china_note = "行情使用回退数据源，方向可观察，精度为中等。"
    return {
        "us": {"label": "美国 / 全球", "asOf": us_date, "freshness": "最近收盘", "isStale": us_date != report_date, "items": us_items, "note": f"截至 {us_date} 最近有效收盘。"},
        "china": {"label": "中国 / 香港", "asOf": china_time, "freshness": "报告日", "isStale": china_time != report_date, "items": china_items, "note": china_note},
    }


def portfolio_review(lines: list[str], account: str) -> str:
    collecting = False
    collected: list[str] = []
    for line in lines:
        heading = re.match(r"^###\s+(.+)", line.strip())
        if heading:
            if collecting:
                break
            collecting = heading.group(1).strip().upper() == account
            continue
        if collecting and line.strip():
            collected.append(line.strip())
    return compact(" ".join(collected), 520)


def reconstruct_portfolio_for_date(account: str, report_date: str) -> dict[str, Any] | None:
    base = load_json(PAPER_CONFIG_PATH, {})
    account_settings = base.get("accounts", {}).get(account, {}) if isinstance(base, dict) else {}
    if not isinstance(account_settings, dict) or not account_settings:
        return None
    config = dict(base)
    config.pop("accounts", None)
    config.update(account_settings)
    trades = read_jsonl(ROOT / str(config["trades_file"]))
    positions: dict[str, dict[str, Any]] = {}
    prices: dict[str, dict[str, Any]] = {}
    cash = float(config.get("initial_cash", 0.0))
    realized = 0.0

    def fx_rate(currency: str, explicit: Any = None) -> float:
        if explicit not in (None, ""):
            return float(explicit)
        rates = config.get("fx_rates_to_base", {})
        return float(rates.get(currency, 1.0 if currency == str(config.get("base_currency") or "") else 0.0))

    for trade in trades:
        trade_date = str(trade.get("date") or "")[:10]
        if not trade_date or trade_date > report_date:
            continue
        action = str(trade.get("action") or "").upper()
        symbol = str(trade.get("symbol") or "").upper()
        exchange = str(trade.get("exchange") or "").upper()
        key = f"{exchange}:{symbol}" if exchange else symbol
        currency = str(trade.get("currency") or config.get("base_currency") or "").upper()
        rate = fx_rate(currency, trade.get("fx_to_base"))
        if rate <= 0:
            return None
        price = trade.get("price")
        if price not in (None, "") and float(price) > 0:
            prices[key] = {"price": float(price), "date": trade_date, "source": trade.get("source"), "currency": currency, "fx_to_base": rate}
        if action not in {"BUY", "SELL"}:
            continue
        quantity = float(trade.get("quantity", 0.0))
        gross_native = float(trade.get("gross_value", quantity * float(price or 0.0)))
        fee_native = float(trade.get("fee", 0.0))
        gross_base = float(trade.get("gross_value_base", gross_native * rate))
        fee_base = float(trade.get("fee_base", fee_native * rate))
        if action == "BUY":
            position = positions.setdefault(key, {"symbol": symbol, "exchange": exchange, "market_type": trade.get("market_type"), "currency": currency, "fx_to_base": rate, "quantity": 0.0, "avg_cost": 0.0, "cost_basis": 0.0})
            old_qty = float(position["quantity"])
            new_qty = old_qty + quantity
            position["avg_cost"] = (old_qty * float(position["avg_cost"]) + gross_native + fee_native) / new_qty if new_qty else 0.0
            position["quantity"] = new_qty
            position["cost_basis"] = new_qty * float(position["avg_cost"])
            position["fx_to_base"] = rate
            cash -= gross_base + fee_base
        else:
            position = positions.get(key)
            if not position:
                continue
            sell_qty = min(quantity, float(position["quantity"]))
            realized_native = (float(price or 0.0) - float(position["avg_cost"])) * sell_qty - fee_native
            realized += float(trade.get("realized_pnl_base", realized_native * rate))
            cash += gross_base - fee_base
            position["quantity"] = float(position["quantity"]) - sell_qty
            position["cost_basis"] = float(position["quantity"]) * float(position["avg_cost"])
            if float(position["quantity"]) <= 1e-9:
                positions.pop(key, None)
    if not trades:
        return None
    return {
        "account_id": config.get("account_id"),
        "mode": "paper_trading",
        "initial_cash": float(config.get("initial_cash", 0.0)),
        "cash": cash,
        "realized_pnl": realized,
        "positions": positions,
        "last_prices": prices,
        "base_currency": config.get("base_currency"),
        "fx_rates_to_base": config.get("fx_rates_to_base", {}),
        "as_of_date": report_date,
    }


def portfolio(account: str, report_date: str, review: str = "") -> dict[str, Any]:
    path = DATA_DIR / ("paper_portfolio_us.json" if account == "US" else "paper_portfolio_china.json")
    data = load_json(path, {})
    cash = float(data.get("cash") or 0)
    positions = data.get("positions") or {}
    prices = data.get("last_prices") or {}
    price_dates = [str(item.get("date") or "")[:10] for item in prices.values() if isinstance(item, dict) and item.get("date")]
    state_date = str(data.get("as_of_date") or (max(price_dates) if price_dates else "") or data.get("last_updated") or "")[:10]
    historical_summary: dict[str, Any] | None = None
    historical_reconstructed = False
    if state_date and state_date > report_date:
        reconstructed = reconstruct_portfolio_for_date(account, report_date)
        if reconstructed:
            data = reconstructed
            cash = float(data.get("cash") or 0)
            positions = data.get("positions") or {}
            prices = data.get("last_prices") or {}
            state_date = report_date
            historical_reconstructed = True
        else:
            valuation_path = DATA_DIR / ("paper_valuations_us.jsonl" if account == "US" else "paper_valuations_china.jsonl")
            candidates = [row for row in read_jsonl(valuation_path) if str(row.get("date") or "") <= report_date]
            historical_summary = max(candidates, key=lambda row: str(row.get("date") or "")) if candidates else None
            positions = {}
            prices = {}
            if historical_summary:
                cash = float(historical_summary.get("cash") or 0)
    values: list[tuple[str, float]] = []
    position_rows: list[dict[str, Any]] = []
    for key, position in positions.items():
        price_payload = prices.get(key) or {}
        price = float(price_payload.get("price") or position.get("avg_cost") or 0)
        currency = str(position.get("currency") or price_payload.get("currency") or data.get("base_currency") or "")
        fx_to_base = float(price_payload.get("fx_to_base", position.get("fx_to_base", data.get("fx_rates_to_base", {}).get(currency, 1.0))))
        native_value = float(position.get("quantity") or 0) * price
        value = native_value * fx_to_base
        symbol = str(position.get("symbol") or key)
        cost_basis_native = float(position.get("cost_basis") or 0)
        cost_basis = cost_basis_native * fx_to_base
        pnl = value - cost_basis
        values.append((symbol, value))
        position_rows.append(
            {
                "symbol": symbol,
                "exchange": str(position.get("exchange") or ""),
                "marketType": str(position.get("market_type") or ""),
                "currency": currency,
                "baseCurrency": str(data.get("base_currency") or ""),
                "fxToBase": fx_to_base,
                "quantity": round(float(position.get("quantity") or 0), 4),
                "avgCost": round(float(position.get("avg_cost") or 0), 4),
                "lastPrice": round(price, 4),
                "marketValue": round(value, 2),
                "marketValueNative": round(native_value, 2),
                "costBasis": round(cost_basis, 2),
                "unrealizedPnl": round(pnl, 2),
                "returnPct": round((pnl / cost_basis) * 100, 2) if cost_basis else None,
                "priceDate": str(price_payload.get("date") or report_date),
                "priceSource": compact(str(price_payload.get("source") or "本地最近标记"), 120),
            }
        )
    values.sort(key=lambda pair: pair[1], reverse=True)
    position_rows.sort(key=lambda row: float(row["marketValue"]), reverse=True)
    equity = cash + sum(value for _, value in values)
    if historical_summary:
        equity = float(historical_summary.get("equity") or equity)
        historical_positions_value = float(historical_summary.get("positions_value") or 0)
        values = [("历史持仓汇总", historical_positions_value)] if historical_positions_value else []
    initial = float(data.get("initial_cash") or 100000)
    total_return = ((equity / initial) - 1) * 100 if initial else 0
    allocations = [{"label": "现金", "pct": round(cash / equity * 100, 1) if equity else 0}]
    for symbol, value in values[:2]:
        allocations.append({"label": symbol, "pct": round(value / equity * 100, 1) if equity else 0})
    used = sum(item["pct"] for item in allocations)
    allocations.append({"label": "其他", "pct": round(max(0, 100 - used), 1)})
    portfolio_base_currency = str(data.get("base_currency") or (historical_summary or {}).get("base_currency") or "")
    return {
        "name": f"{account} 虚拟组合",
        "value": f"{equity:,.2f}",
        "return": f"{total_return:+.2f}%",
        "returnPct": round(total_return, 2),
        "allocations": allocations,
        "accountId": str(data.get("account_id") or ""),
        "asOf": str(historical_summary.get("date") if historical_summary else data.get("as_of_date") or state_date or report_date),
        "baseCurrency": portfolio_base_currency,
        "initialCash": round(initial, 2),
        "cash": round(cash, 2),
        "cashPct": round(cash / equity * 100, 2) if equity else 0,
        "equity": round(equity, 2),
        "realizedPnl": round(float(data.get("realized_pnl") or 0), 2),
        "positions": position_rows,
        "review": review or "本期维持虚拟研究账户，仅按最近可得价格完成标记。",
        "paperTradingOnly": data.get("mode") == "paper_trading",
        "detailAvailable": historical_summary is None,
        "limitations": ["历史持仓由截止报告日的虚拟成交逐笔重建，未使用未来持仓快照。"] if historical_reconstructed else ["历史日期仅有账户估值汇总，未使用未来持仓快照。"] if historical_summary else [],
    }


def top_sources(text: str) -> list[dict[str, str]]:
    seen: set[str] = set()
    result = []
    for label, href in re.findall(r"\[([^\]]+)\]\((https?://[^)]+)\)", text):
        key = href.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append({"label": compact(label, 34), "href": href})
        if len(result) == 20:
            break
    return result


def split_headline(value: str) -> list[str]:
    headline = compact(value.rstrip("。"), 46)
    for separator in ("，", "；", "：", "、"):
        if separator in headline:
            left, right = headline.split(separator, 1)
            if len(left) >= 6 and len(right) >= 6:
                return [left + separator, right]
    midpoint = max(8, len(headline) // 2)
    return [headline[:midpoint], headline[midpoint:]] if headline[midpoint:] else [headline, "等待下一项验证"]


def stable_generated_at(text: str, report_date: str) -> str:
    retrieved = re.search(r"资料检索时间[：:]\s*([^\n]+)", text)
    if retrieved:
        try:
            parsed = datetime.strptime(retrieved.group(1).strip(), "%Y-%m-%d %H:%M:%S")
            return parsed.replace(tzinfo=timezone(timedelta(hours=8))).isoformat()
        except ValueError:
            pass
    return f"{report_date}T00:00:00+08:00"


def evolution_for_date(report_date: str) -> dict[str, Any]:
    exact = DATA_DIR / f"evolution-state-{report_date}.json"
    if exact.exists():
        return load_json(exact, {})
    candidates: list[tuple[str, Path]] = []
    for path in DATA_DIR.glob("evolution-state-*.json"):
        match = re.search(r"evolution-state-(\d{4}-\d{2}-\d{2})\.json$", path.name)
        if match and match.group(1) <= report_date:
            candidates.append((match.group(1), path))
    if candidates:
        return load_json(max(candidates, key=lambda item: item[0])[1], {})
    latest = load_json(EVOLUTION_STATE_PATH, {})
    return latest if str(latest.get("as_of") or "") <= report_date else {}


def source_health(report_date: str) -> dict[str, Any]:
    rss = load_json(DATA_DIR / f"rss-items-{report_date}.json", {})
    china = load_json(DATA_DIR / f"china-market-snapshot-{report_date}.json", {})
    market = load_json(DATA_DIR / f"market-snapshot-{report_date}.json", {})
    sources_config = load_json(SOURCES_CONFIG_PATH, {})
    items = rss.get("items", []) if isinstance(rss, dict) else []
    errors = rss.get("errors", []) if isinstance(rss, dict) else []
    fallbacks = rss.get("fallbacks", []) if isinstance(rss, dict) else []
    reference = datetime.fromisoformat(str(rss.get("generated_at") or f"{report_date}T23:59:59+08:00"))
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone(timedelta(hours=8)))
    fresh_items = 0
    stale_items = 0
    unknown_timestamp_items = 0
    rss_sources: set[str] = set()
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        if item.get("source"):
            rss_sources.add(str(item["source"]))
        try:
            published = parsedate_to_datetime(str(item.get("published") or ""))
            if published.tzinfo is None:
                published = published.replace(tzinfo=timezone.utc)
            age_hours = (reference.astimezone(timezone.utc) - published.astimezone(timezone.utc)).total_seconds() / 3600
            if 0 <= age_hours <= 24:
                fresh_items += 1
            else:
                stale_items += 1
        except (TypeError, ValueError, OverflowError):
            unknown_timestamp_items += 1
    configured_rss_sources = {
        str(item.get("name"))
        for item in sources_config.get("sources", []) if isinstance(item, dict) and item.get("rss")
    }
    rss_coverage_pct = round(len(rss_sources & configured_rss_sources) / max(len(configured_rss_sources), 1) * 100, 2)
    china_errors = china.get("errors", []) if isinstance(china, dict) else []
    china_fallbacks = china.get("structured_fallbacks", []) if isinstance(china, dict) else []
    china_items = china.get("items", []) if isinstance(china, dict) else []
    china_fallback_items = sum(
        int(item.get("items") or 0) for item in china_fallbacks if isinstance(item, dict)
    )
    market_items = market.get("items", []) if isinstance(market, dict) else []
    prior_close_market_items = 0
    stale_market_items = 0
    report_day = Date.fromisoformat(report_date)
    for item in market_items:
        if not isinstance(item, dict) or not item.get("price_date"):
            continue
        try:
            age_days = (report_day - Date.fromisoformat(str(item["price_date"])[:10])).days
        except ValueError:
            stale_market_items += 1
            continue
        if age_days > 0:
            prior_close_market_items += 1
        if age_days > 3:
            stale_market_items += 1
    score = 100
    score -= min(24, len(errors) * 3)
    score -= min(12, len(fallbacks) * 6)
    score -= min(30, len(china_errors) * 5 + (10 if china_fallback_items else 0))
    score -= min(15, round((100 - rss_coverage_pct) * 0.15))
    if market_items and stale_market_items == len(market_items):
        score -= 8
    score = max(0, min(100, int(score)))
    label = "良好" if score >= 85 else "中等" if score >= 65 else "受限"
    limitations: list[str] = []
    if errors:
        limitations.append(f"RSS 采集出现 {len(errors)} 个错误。")
    if fallbacks:
        limitations.append(f"RSS 使用 {len(fallbacks)} 次回退。")
    if china_errors:
        limitations.append(f"中国结构化行情出现 {len(china_errors)} 个错误，{china_fallback_items}/{len(china_items)} 条使用备用报价。")
    if stale_market_items:
        limitations.append(f"全球行情 {stale_market_items}/{len(market_items)} 条价格日期早于报告日。")
    return {
        "label": label,
        "score": score,
        "rssItemCount": len(items),
        "rssFresh24hCount": fresh_items,
        "rssStaleCount": stale_items,
        "rssUnknownTimestampCount": unknown_timestamp_items,
        "rssErrorCount": len(errors),
        "rssFallbackCount": len(fallbacks),
        "rssSourceCoveragePct": rss_coverage_pct,
        "chinaErrorCount": len(china_errors),
        "chinaFallbackItemCount": china_fallback_items,
        "chinaItemCount": len(china_items),
        "staleMarketItemCount": stale_market_items,
        "priorCloseMarketItemCount": prior_close_market_items,
        "marketItemCount": len(market_items),
        "limitations": limitations,
        "method": "artifact_backed_source_health_v1",
    }


def site_input_hash(raw_report: bytes, report_date: str) -> str:
    digest = hashlib.sha256()
    digest.update(f"atlas-site-schema:{SITE_SCHEMA_VERSION}\n".encode("utf-8"))
    digest.update(raw_report)
    system_status = build_system_status(report_date)
    # Self-healing telemetry is downstream operational metadata. Excluding it
    # from the content identity prevents a health run from recursively creating
    # a new site update that triggers another health run.
    system_status.pop("selfHealing", None)
    system_status.pop("improvements", None)
    dated_inputs = {
        "predictions": predictions_for_date(report_date),
        "evolution": evolution_for_date(report_date),
        "sourceHealth": source_health(report_date),
        "markets": parse_markets(report_date),
        "portfolios": {
            "us": portfolio("US", report_date),
            "china": portfolio("CHINA", report_date),
        },
        "system": system_status,
    }
    digest.update(json.dumps(dated_inputs, ensure_ascii=False, sort_keys=True).encode("utf-8"))
    return digest.hexdigest()


def cycle_audit_path(report_date: str) -> Path | None:
    exact = ATLAS_RUNTIME_ROOT / "run_audits" / f"atlas-cycle-{report_date}.json"
    if exact.exists():
        return exact
    candidates: list[tuple[str, Path]] = []
    for path in (ATLAS_RUNTIME_ROOT / "run_audits").glob("atlas-cycle-*.json"):
        match = re.search(r"atlas-cycle-(\d{4}-\d{2}-\d{2})\.json$", path.name)
        if match and match.group(1) <= report_date:
            candidates.append((match.group(1), path))
    return max(candidates, key=lambda item: item[0])[1] if candidates else None


def build_system_status(report_date: str) -> dict[str, Any]:
    audit_path = cycle_audit_path(report_date)
    cycle = load_json(audit_path, {}) if audit_path else {}
    ledger_state = cycle.get("ledger", {}) if isinstance(cycle, dict) else {}
    ledger_audit = cycle.get("ledger_audit", {}) if isinstance(cycle, dict) else {}
    replay = cycle.get("replay_shadow_validation", {}) if isinstance(cycle, dict) else {}
    replay_result = replay.get("replay", {}) if isinstance(replay, dict) else {}
    shadow = replay.get("shadow_promotion_gate", {}) if isinstance(replay, dict) else {}
    shadow_payload = shadow.get("payload", {}) if isinstance(shadow, dict) else {}
    self_healing = load_json(ATLAS_SELF_HEALING_LATEST, {})
    if not isinstance(self_healing, dict) or str(self_healing.get("date") or "") != report_date:
        self_healing = {}
    healing_counts = self_healing.get("counts", {}) if isinstance(self_healing, dict) else {}
    improvements = load_json(ATLAS_IMPROVEMENTS_LATEST, {})
    if not isinstance(improvements, dict) or str(improvements.get("date") or "") != report_date:
        improvements = {}
    improvement_counts = improvements.get("counts", {}) if isinstance(improvements, dict) else {}
    stages = []
    for item in cycle.get("stages", []) if isinstance(cycle, dict) else []:
        if isinstance(item, dict):
            stages.append({
                "name": str(item.get("name") or "unknown"),
                "status": str(item.get("status") or "unknown"),
                "detail": item.get("detail"),
            })
    return {
        "asOf": str(cycle.get("date") or report_date) if isinstance(cycle, dict) else report_date,
        "cycleId": str(cycle.get("cycle_id") or "") if isinstance(cycle, dict) else "",
        "overallPassed": cycle.get("overall_passed") is True if isinstance(cycle, dict) else False,
        "blockingReasons": list(cycle.get("blocking_reasons") or []) if isinstance(cycle, dict) else [],
        "stages": stages,
        "ledger": {
            "auditPassed": ledger_audit.get("overall_passed") is True if isinstance(ledger_audit, dict) else False,
            "eventCount": int(ledger_state.get("event_count") or 0) if isinstance(ledger_state, dict) else 0,
            "accountCount": int(
                ledger_state.get("account_count")
                or ledger_audit.get("account_count")
                or 0
            ) if isinstance(ledger_state, dict) and isinstance(ledger_audit, dict) else 0,
            "contentHash": str(ledger_state.get("content_hash") or "") if isinstance(ledger_state, dict) else "",
            "sourceCounts": ledger_audit.get("source_counts") or {} if isinstance(ledger_audit, dict) else {},
        },
        "replay": {
            "passed": replay_result.get("passed") is True if isinstance(replay_result, dict) else False,
            "executionSafetyPassed": replay_result.get("execution_safety_passed", replay_result.get("passed")) is True if isinstance(replay_result, dict) else False,
            "strategyEvidencePassed": replay_result.get("strategy_evidence_passed") is True if isinstance(replay_result, dict) else False,
            "validationScope": str(replay_result.get("validation_scope") or "execution_isolation_only") if isinstance(replay_result, dict) else "execution_isolation_only",
            "periodStart": replay_result.get("period_start") if isinstance(replay_result, dict) else None,
            "periodEnd": replay_result.get("period_end") if isinstance(replay_result, dict) else None,
        },
        "shadow": {
            "passed": shadow.get("passed") is True if isinstance(shadow, dict) else False,
            "recommendedState": str(shadow_payload.get("recommended_state") or "shadow") if isinstance(shadow_payload, dict) else "shadow",
            "evidenceStatus": str(shadow_payload.get("evidence_status") or "unknown") if isinstance(shadow_payload, dict) else "unknown",
            "recommendation": str(shadow_payload.get("recommendation") or "") if isinstance(shadow_payload, dict) else "",
            "autoApplied": shadow_payload.get("auto_applied") is True if isinstance(shadow_payload, dict) else False,
        },
        "selfHealing": {
            "status": str(self_healing.get("overall_status") or "not_run"),
            "checks": int(healing_counts.get("checks") or 0) if isinstance(healing_counts, dict) else 0,
            "passed": int(healing_counts.get("passed") or 0) if isinstance(healing_counts, dict) else 0,
            "repairsAttempted": int(healing_counts.get("repairs_attempted") or 0) if isinstance(healing_counts, dict) else 0,
            "repairsVerified": int(healing_counts.get("repairs_verified") or 0) if isinstance(healing_counts, dict) else 0,
            "unresolved": int(healing_counts.get("unresolved") or 0) if isinstance(healing_counts, dict) else 0,
            "blocking": int(healing_counts.get("blocking") or 0) if isinstance(healing_counts, dict) else 0,
            "sourceCodeAutoModified": False,
            "productionAutoDeployed": False,
        },
        "improvements": {
            "status": str(improvements.get("status") or "not_run"),
            "total": int(improvement_counts.get("total") or 0) if isinstance(improvement_counts, dict) else 0,
            "verified": int(improvement_counts.get("verified") or 0) if isinstance(improvement_counts, dict) else 0,
            "monitoring": int(improvement_counts.get("monitoring") or 0) if isinstance(improvement_counts, dict) else 0,
            "open": int(improvement_counts.get("open") or 0) if isinstance(improvement_counts, dict) else 0,
            "regressed": int(improvement_counts.get("regressed") or 0) if isinstance(improvement_counts, dict) else 0,
            "overdue": int(improvement_counts.get("overdue") or 0) if isinstance(improvement_counts, dict) else 0,
            "blocking": int(improvement_counts.get("blocking") or 0) if isinstance(improvement_counts, dict) else 0,
            "capabilityGaps": int(improvement_counts.get("capability_gaps") or 0) if isinstance(improvement_counts, dict) else 0,
        },
        "boundary": {
            "paperTradingOnly": True,
            "realBrokerOrdersAllowed": False,
            "legacyLedgersReadOnly": True,
        },
    }


def report_quality_audit(text: str) -> dict[str, Any]:
    sections = split_sections(text)
    errors: list[str] = []
    warnings: list[str] = []
    settings = load_json(SETTINGS_CONFIG_PATH, {})
    research_policy = settings.get("news_research_policy", {}) if isinstance(settings, dict) else {}
    quality_gate = research_policy.get("quality_gate", {}) if isinstance(research_policy, dict) else {}
    report_date_match = re.search(r"20\d{2}-\d{2}-\d{2}", text[:500])
    report_date = report_date_match.group(0) if report_date_match else "9999-12-31"
    enforce_from_date = str(research_policy.get("enforce_from_date") or "0001-01-01")
    deep_research_enforced = report_date >= enforce_from_date
    minimum_characters = int(quality_gate.get("minimum_report_characters", 2500))
    maximum_characters = int(quality_gate.get("maximum_report_characters", 7000))
    minimum_links = int(quality_gate.get("minimum_distinct_links", 10))
    minimum_domains = int(quality_gate.get("minimum_distinct_domains", 6))
    minimum_topic_characters = int(quality_gate.get("minimum_topic_characters", 0))
    minimum_topic_links = int(quality_gate.get("minimum_topic_links", 1))
    filler_terms = ("值得关注", "持续关注", "总体来看", "未来可期", "不容忽视", "需密切关注", "需要密切关注")
    filler_hits = [term for term in filler_terms if term in text]
    if filler_hits:
        errors.append(f"generic filler phrases are forbidden: {filler_hits}")

    character_count = len(text)
    if character_count > maximum_characters:
        errors.append(f"report exceeds the {maximum_characters}-character deep-briefing limit: {character_count}")
    if character_count < minimum_characters:
        errors.append(f"report is too short for deep evidence chains: {character_count} < {minimum_characters}")

    topic_specs = [
        ("政治与外交",),
        ("科技与AI",),
        ("环境与气候",),
        ("军事与安全",),
        ("经济与能源",),
        ("公共卫生与社会",),
    ]
    required_layers = ("结论", "硬证据", "机制", "反证", "证伪")
    complete_topics = 0
    for aliases in topic_specs:
        lines = find_section(sections, *aliases)
        if not lines:
            errors.append(f"missing decision topic section: {aliases[0]}")
            continue
        clean_lines = [plain_markdown(line, 800) for line in lines if line.strip()]
        missing = [layer for layer in required_layers if not any(re.search(rf"(?:^|[-*]\s*)\*?\*?{layer}[：:]", line) for line in clean_lines)]
        if missing:
            errors.append(f"{aliases[0]} missing decision layers: {missing}")
        else:
            complete_topics += 1
        topic_text = " ".join(plain_markdown(line, 1000) for line in lines if line.strip())
        if deep_research_enforced and len(topic_text) < minimum_topic_characters:
            errors.append(
                f"{aliases[0]} is too shallow: {len(topic_text)} < {minimum_topic_characters} characters"
            )
        topic_links = {item["href"] for item in markdown_links("\n".join(lines), limit=40)}
        if len(topic_links) < minimum_topic_links:
            errors.append(
                f"{aliases[0]} needs at least {minimum_topic_links} clickable evidence source(s), found {len(topic_links)}"
            )

    def table_row_count(lines: list[str]) -> int:
        rows = [line for line in lines if line.strip().startswith("|")]
        rows = [line for line in rows if not re.fullmatch(r"[|:\- ]+", line.strip())]
        return max(0, len(rows) - 1)

    core_rows = table_row_count(find_section(sections, "核心摘要"))
    observation_rows = table_row_count(find_section(sections, "股票与ETF观察", "全球股票/ETF观察"))
    if not 3 <= core_rows <= 5:
        errors.append(f"core summary must contain 3-5 decision rows, found {core_rows}")
    if observation_rows > 7:
        errors.append(f"observation list must contain at most 7 rows, found {observation_rows}")

    report_links = markdown_links(text, limit=200)
    distinct_links = {item["href"] for item in report_links}
    source_count = len(distinct_links)
    if source_count < minimum_links:
        errors.append(f"report needs at least {minimum_links} distinct clickable sources, found {source_count}")
    source_domain_count = len({urlparse(href).netloc.lower() for href in distinct_links if urlparse(href).netloc})
    if source_domain_count < minimum_domains:
        errors.append(f"report needs at least {minimum_domains} independent source domains, found {source_domain_count}")

    comparable: list[str] = []
    for title, lines in sections.items():
        if "来源" in title:
            continue
        for line in lines:
            clean = plain_markdown(line, 500)
            clean = re.sub(r"^[-*\d.、\s]+(?:结论|硬证据|机制|反证|证伪)?[：:]?", "", clean)
            if len(clean) >= 28 and not clean.startswith("|"):
                comparable.append(clean)
    repeated_pairs = []
    for index, left in enumerate(comparable):
        for right in comparable[index + 1 :]:
            ratio = SequenceMatcher(None, left, right).ratio()
            if ratio >= 0.82:
                repeated_pairs.append({"left": compact(left, 80), "right": compact(right, 80), "similarity": round(ratio, 2)})
                if len(repeated_pairs) >= 8:
                    break
        if len(repeated_pairs) >= 8:
            break
    if repeated_pairs:
        warnings.append(f"found {len(repeated_pairs)} highly similar statement pairs")

    nonempty_lines = sum(1 for line in text.splitlines() if line.strip() and not line.startswith("#"))
    return {
        "passed": not errors,
        "characterCount": character_count,
        "sourceCount": source_count,
        "coreDecisionCount": core_rows,
        "primaryThesisCount": core_rows,
        "topicSectionCount": complete_topics,
        "sourceDomainCount": source_domain_count,
        "deepResearchMode": research_policy.get("mode") == "deep",
        "deepResearchEnforced": deep_research_enforced,
        "deepResearchEnforceFromDate": enforce_from_date,
        "minimumCharacters": minimum_characters,
        "maximumCharacters": maximum_characters,
        "minimumDistinctLinks": minimum_links,
        "minimumDistinctDomains": minimum_domains,
        "observationCount": observation_rows,
        "fillerCount": len(filler_hits),
        "repeatedPairCount": len(repeated_pairs),
        "decisionLayerDensityPct": round((complete_topics * len(required_layers)) / max(nonempty_lines, 1) * 100, 2),
        "errors": errors,
        "warnings": warnings,
        "repeatedPairs": repeated_pairs,
    }


def build_payload(text: str, report_date: str, report_count: int, sha256: str) -> dict[str, Any]:
    sections = split_sections(text)
    predictions = predictions_for_date(report_date)
    all_sources = markdown_links(text, limit=30)
    events = parse_events(sections, predictions, all_sources)
    for index, event in enumerate(events, 1):
        event["id"] = f"{report_date}-{index:02d}-{event['category']}"
    scenarios = scenarios_from_predictions(predictions) or parse_scenarios(find_section(sections, "未来预测与市场映射"))
    for index, scenario in enumerate(scenarios, 1):
        scenario.setdefault("id", f"{report_date}-scenario-{index:02d}")
        scenario.setdefault("status", "open")
        scenario.setdefault("drivers", [])
        scenario.setdefault("beneficiaries", [])
        scenario.setdefault("pressures", [])
        scenario.setdefault("verificationSignals", [scenario.get("watch", "等待执行、价格与官方数据确认")])
        scenario.setdefault("instruments", [])
        if not scenario.get("sourceRefs"):
            related_sources: list[dict[str, str]] = []
            seen_source_hrefs: set[str] = set()
            for event in events:
                if scenario["id"] not in event.get("relatedPredictionIds", []):
                    continue
                for source in event.get("sources", []):
                    if source["href"] not in seen_source_hrefs:
                        seen_source_hrefs.add(source["href"])
                        related_sources.append(source)
            scenario["sourceRefs"] = related_sources or all_sources[:3]
    watchlist = [compact(item, 120) for item in numbered_items(find_section(sections, "今日关注清单", "今日观察清单", "今日重点观察"))[:5]]
    risks = [compact(item, 140) for item in numbered_items(find_section(sections, "风险信号"))[:3]]
    retrieved = re.search(r"资料检索时间[：:]\s*([^\n]+)", text)
    high_count = sum(1 for item in scenarios if item.get("chance") == "高")
    medium_count = sum(1 for item in scenarios if item.get("chance") in {"中", "中等"})
    risk_temperature = min(100, 20 + high_count * 8 + medium_count * 4 + len(risks) * 5)
    posture = "选择性防御" if risk_temperature >= 72 else "谨慎均衡" if risk_temperature >= 64 else "适度进取"
    health = source_health(report_date)
    lead = events[0]["body"] if events else "最新一期全球简报已经生成，等待进一步验证。"
    core_items = numbered_items(find_section(sections, "核心摘要"))
    core_bold = re.search(r"\*\*([^*]+)\*\*", core_items[0]) if core_items else None
    core_headline = compact(core_bold.group(1), 34) if core_bold else plain_markdown(core_items[0], 34) if core_items else compact(events[0]["title"], 34) if events else "全球风险等待下一项验证"
    portfolio_lines = find_section(sections, "虚拟炒股账户", "虚拟交易账户", "虚拟账户")
    forecast_reviews = [plain_markdown(item, 360) for item in bullet_items(find_section(sections, "昨日预测复盘", "预测复盘"))[:8]]
    framework_updates = [plain_markdown(item, 260) for item in numbered_items(find_section(sections, "预测框架更新", "框架更新"))[:8]]
    limitation_lines = [
        compact(line, 300)
        for lines in sections.values()
        for line in lines
        if any(token in line for token in ("超时", "回退", "低时效", "数据源限制", "未作新信息解读"))
    ]
    return {
        "schemaVersion": SITE_SCHEMA_VERSION,
        "reportDate": report_date,
        "retrievedAt": compact(retrieved.group(1), 40) if retrieved else report_date,
        "issue": f"VOL. {report_count:03d}",
        "contentHash": sha256,
        "generatedAt": stable_generated_at(text, report_date),
        "hero": {
            "eyebrow": "每日全球晨间简报",
            "headline": split_headline(core_headline),
            "dek": lead,
            "editorNote": events[0]["analysis"] if events else "关注事实、执行与资产价格之间的传导。",
        },
        "metrics": {
            "riskTemperature": risk_temperature,
            "posture": posture,
            "signalCount": len(events),
            "freshness": health["label"],
            "sourceHealth": health,
            "riskModel": {
                "kind": "heuristic_signal_index",
                "calibrated": False,
                "formula": "20 + 高概率情景×8 + 中概率情景×4 + 风险条目×5，上限100",
                "highScenarioCount": high_count,
                "mediumScenarioCount": medium_count,
                "riskItemCount": len(risks),
            },
            "readingMinutes": max(6, min(20, round(len(text) / 850))),
        },
        "events": events,
        "scenarios": scenarios,
        "markets": parse_markets(report_date),
        "portfolios": {
            "us": portfolio("US", report_date, portfolio_review(portfolio_lines, "US")),
            "china": portfolio("CHINA", report_date, portfolio_review(portfolio_lines, "CHINA")),
        },
        "watchlist": watchlist,
        "risks": risks,
        "sources": top_sources(text),
        "system": build_system_status(report_date),
        "forecastReviews": forecast_reviews,
        "frameworkUpdates": framework_updates,
        "evolution": evolution_for_date(report_date),
        "reportQuality": report_quality_audit(text),
        "limitations": list(dict.fromkeys([*limitation_lines, *health["limitations"]]))[:8],
    }


def safe_public_href(value: Any) -> bool:
    if not isinstance(value, str) or not value:
        return False
    if value.startswith("#"):
        return bool(re.fullmatch(r"#[A-Za-z][A-Za-z0-9_-]*", value))
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc) and not parsed.username and not parsed.password


def validate_payload(payload: dict[str, Any]) -> list[str]:
    """Validate generated site data before it can replace the deployed payload."""
    errors: list[str] = []
    if payload.get("schemaVersion") != SITE_SCHEMA_VERSION:
        errors.append(f"schemaVersion must be {SITE_SCHEMA_VERSION}")
    try:
        valid_iso_date(str(payload.get("reportDate", "")))
    except argparse.ArgumentTypeError as exc:
        errors.append(str(exc))

    hero = payload.get("hero")
    if not isinstance(hero, dict) or not isinstance(hero.get("headline"), list) or len(hero["headline"]) < 2:
        errors.append("hero.headline must contain at least two lines")

    metrics = payload.get("metrics")
    risk_temperature = metrics.get("riskTemperature") if isinstance(metrics, dict) else None
    if not isinstance(risk_temperature, int) or not 0 <= risk_temperature <= 100:
        errors.append("metrics.riskTemperature must be an integer from 0 to 100")

    events = payload.get("events")
    if not isinstance(events, list) or not events:
        errors.append("events must contain at least one event")
    else:
        seen_ids: set[str] = set()
        for index, event in enumerate(events, 1):
            prefix = f"events[{index}]"
            if not isinstance(event, dict):
                errors.append(f"{prefix} must be an object")
                continue
            event_id = event.get("id")
            if not isinstance(event_id, str) or not event_id:
                errors.append(f"{prefix}.id is required")
            elif event_id in seen_ids:
                errors.append(f"{prefix}.id must be unique")
            else:
                seen_ids.add(event_id)
            for field, minimum in (("title", 8), ("body", 12), ("implication", 12), ("source", 1)):
                value = event.get(field)
                if not isinstance(value, str) or len(value.strip()) < minimum:
                    errors.append(f"{prefix}.{field} must contain at least {minimum} characters")
            if not safe_public_href(event.get("href")):
                errors.append(f"{prefix}.href must be an http(s) URL or local fragment")
            for field in ("facts", "drivers", "verificationSignals"):
                value = event.get(field)
                if not isinstance(value, list) or not value or not all(isinstance(item, str) and item.strip() for item in value):
                    errors.append(f"{prefix}.{field} must contain non-empty strings")
            event_sources = event.get("sources")
            if not isinstance(event_sources, list) or not event_sources:
                errors.append(f"{prefix}.sources must not be empty")
            elif any(not isinstance(item, dict) or not safe_public_href(item.get("href")) for item in event_sources):
                errors.append(f"{prefix}.sources contain an invalid URL")

    scenarios = payload.get("scenarios")
    if not isinstance(scenarios, list) or not scenarios:
        errors.append("scenarios must contain at least one scenario")
    else:
        scenario_ids: set[str] = set()
        for index, scenario in enumerate(scenarios, 1):
            prefix = f"scenarios[{index}]"
            if not isinstance(scenario, dict):
                errors.append(f"{prefix} must be an object")
                continue
            scenario_id = scenario.get("id")
            if not isinstance(scenario_id, str) or not scenario_id or scenario_id in scenario_ids:
                errors.append(f"{prefix}.id must be present and unique")
            else:
                scenario_ids.add(scenario_id)
            signals = scenario.get("verificationSignals")
            if not isinstance(signals, list) or not signals:
                errors.append(f"{prefix}.verificationSignals must not be empty")
            source_refs = scenario.get("sourceRefs")
            if not isinstance(source_refs, list) or not source_refs:
                errors.append(f"{prefix}.sourceRefs must not be empty")
            elif any(not isinstance(item, dict) or not safe_public_href(item.get("href")) for item in source_refs):
                errors.append(f"{prefix}.sourceRefs contain an invalid URL")
    watchlist = payload.get("watchlist")
    if not isinstance(watchlist, list) or not watchlist:
        errors.append("watchlist must contain at least one item")
    sources = payload.get("sources")
    if not isinstance(sources, list) or not sources:
        errors.append("sources must contain at least one source")
    elif any(not isinstance(item, dict) or not safe_public_href(item.get("href")) for item in sources):
        errors.append("sources contain an invalid URL")
    portfolios = payload.get("portfolios")
    if not isinstance(portfolios, dict):
        errors.append("portfolios must be an object")
    else:
        for account in ("us", "china"):
            portfolio_payload = portfolios.get(account)
            if not isinstance(portfolio_payload, dict):
                errors.append(f"portfolios.{account} must be an object")
                continue
            if portfolio_payload.get("paperTradingOnly") is not True:
                errors.append(f"portfolios.{account} must remain paper-trading only")
            if not isinstance(portfolio_payload.get("positions"), list):
                errors.append(f"portfolios.{account}.positions must be a list")
    system = payload.get("system")
    if not isinstance(system, dict):
        errors.append("system must be an object")
    else:
        boundary = system.get("boundary")
        if not isinstance(boundary, dict) or boundary.get("paperTradingOnly") is not True or boundary.get("realBrokerOrdersAllowed") is not False:
            errors.append("system boundary must remain paper-only and forbid real broker orders")
    evolution = payload.get("evolution")
    if not isinstance(evolution, dict) or evolution.get("mode") != "gated_self_evolution":
        errors.append("evolution must contain the gated self-evolution state")
    else:
        evolution_boundary = evolution.get("boundary")
        if not isinstance(evolution_boundary, dict) or evolution_boundary.get("auto_promote_strategy") is not False or evolution_boundary.get("real_broker_orders_allowed") is not False:
            errors.append("evolution boundary must forbid automatic promotion and real broker orders")
    report_quality = payload.get("reportQuality")
    if not isinstance(report_quality, dict) or report_quality.get("passed") is not True:
        quality_errors = report_quality.get("errors", []) if isinstance(report_quality, dict) else []
        errors.append(f"report quality gate failed: {quality_errors}")
    for field in ("forecastReviews", "frameworkUpdates", "limitations"):
        value = payload.get(field)
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            errors.append(f"{field} must be a list of strings")
    return errors


def serialized_payload(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def payload_sha256(payload: Any) -> str:
    return hashlib.sha256(serialized_payload(payload).encode("utf-8")).hexdigest()


def write_json_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(serialized_payload(payload), encoding="utf-8")
    temporary.replace(path)


def site_data_matches(expected_hash: str, expected_payload_hash: str | None = None) -> bool:
    payload = load_json(SITE_DATA, {})
    return bool(
        isinstance(payload, dict)
        and payload.get("schemaVersion") == SITE_SCHEMA_VERSION
        and payload.get("contentHash") == expected_hash
        and (expected_payload_hash is None or payload_sha256(payload) == expected_payload_hash)
        and not validate_payload(payload)
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync newest briefing into the ATLAS site only when content changes.")
    parser.add_argument("--date", type=valid_iso_date, help="Generate a specific dated report instead of the newest report.")
    parser.add_argument("--dry-run", action="store_true", help="Parse and validate without writing site data or sync state.")
    parser.add_argument("--mark-deployed", metavar="SHA256", help="Mark a previously generated content hash as successfully deployed.")
    parser.add_argument("--deployment-url", default="", help="Production URL stored with --mark-deployed.")
    parser.add_argument("--force", action="store_true", help="Regenerate even when the newest hash is already deployed.")
    args = parser.parse_args()

    state = load_json(STATE_FILE, {})
    if args.mark_deployed:
        current_payload = load_json(SITE_DATA, {})
        current_payload_sha = payload_sha256(current_payload) if isinstance(current_payload, dict) else ""
        state.update({
            "last_deployed_sha": args.mark_deployed,
            "last_deployed_payload_sha": current_payload_sha,
            "last_deployed_at": datetime.now(timezone.utc).isoformat(),
            "deployment_url": args.deployment_url,
        })
        pending_payload_sha = str(state.get("pending_payload_sha") or "")
        if state.get("pending_sha") == args.mark_deployed and (not pending_payload_sha or pending_payload_sha == current_payload_sha):
            for key in ("pending_sha", "pending_payload_sha", "pending_report", "pending_date"):
                state.pop(key, None)
        write_json_atomic(STATE_FILE, state)
        print(json.dumps({"status": "marked", "sha256": args.mark_deployed, "payload_sha256": current_payload_sha, "deployment_url": args.deployment_url}, ensure_ascii=False))
        return 0

    try:
        report_path, report_date = report_for_date(args.date)
        raw = report_path.read_bytes()
    except (FileNotFoundError, OSError) as error:
        print(json.dumps({"status": "error", "error": str(error)}, ensure_ascii=False))
        return 2

    sha256 = site_input_hash(raw, report_date)
    text = raw.decode("utf-8-sig")
    report_count = 0
    for path in OUTPUTS.glob("每日全球晨间简报-*.md"):
        match = REPORT_RE.search(path.name)
        if not match:
            continue
        try:
            Date.fromisoformat(match.group(1))
        except ValueError:
            continue
        if match.group(1) <= report_date:
            report_count += 1
    payload = build_payload(text, report_date, report_count, sha256)
    payload_hash = payload_sha256(payload)
    validation_errors = validate_payload(payload)
    if validation_errors:
        print(json.dumps({"status": "error", "date": report_date, "errors": validation_errors}, ensure_ascii=False, indent=2))
        return 2
    if args.dry_run:
        print(json.dumps({
            "status": "validated",
            "sha256": sha256,
            "payload_sha256": payload_hash,
            "report": str(report_path),
            "date": report_date,
            "events": len(payload["events"]),
            "scenarios": len(payload["scenarios"]),
        }, ensure_ascii=False))
        return 0
    if not args.force and state.get("last_deployed_sha") == sha256 and state.get("last_deployed_payload_sha") == payload_hash and site_data_matches(sha256, payload_hash):
        print(json.dumps({"status": "unchanged", "sha256": sha256, "payload_sha256": payload_hash, "report": str(report_path), "date": report_date}, ensure_ascii=False))
        return 0
    if not args.force and state.get("pending_sha") == sha256 and state.get("pending_payload_sha") == payload_hash and site_data_matches(sha256, payload_hash):
        print(json.dumps({"status": "pending", "sha256": sha256, "payload_sha256": payload_hash, "report": str(report_path), "date": report_date, "generated": str(SITE_DATA)}, ensure_ascii=False))
        return 0
    write_json_atomic(SITE_DATA, payload)
    state.update({
        "pending_sha": sha256,
        "pending_payload_sha": payload_hash,
        "pending_report": str(report_path),
        "pending_date": report_date,
        "last_checked_at": datetime.now(timezone.utc).isoformat(),
    })
    write_json_atomic(STATE_FILE, state)
    print(json.dumps({
        "status": "changed",
        "sha256": sha256,
        "payload_sha256": payload_hash,
        "report": str(report_path),
        "date": report_date,
        "generated": str(SITE_DATA),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
