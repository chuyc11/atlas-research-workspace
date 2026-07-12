#!/usr/bin/env python3
"""Best-effort RSS/Atom collector for the global briefing source matrix."""

from __future__ import annotations

import argparse
import html
import re
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from defusedxml import ElementTree as ET
from html.parser import HTMLParser
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any


SCRIPT_PATH = Path(__file__).resolve()
ROOT = SCRIPT_PATH.parents[3]
SOURCES_PATH = ROOT / "work" / "global-briefing" / "config" / "sources.json"
DATA_DIR = ROOT / "work" / "global-briefing" / "data"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0 Safari/537.36 CodexGlobalBriefing/1.0"
)
REQUEST_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, text/html;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.7,zh;q=0.6",
}
MAX_RESPONSE_BYTES = 5 * 1024 * 1024


def fetch(url: str, timeout: int, max_response_bytes: int = MAX_RESPONSE_BYTES) -> bytes:
    request = urllib.request.Request(url, headers=REQUEST_HEADERS)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        content_length = response.headers.get("Content-Length")
        if content_length:
            try:
                declared_size = int(content_length)
            except ValueError:
                declared_size = 0
            if declared_size > max_response_bytes:
                raise ValueError(f"Response exceeds {max_response_bytes} byte limit: {url}")
        payload = response.read(max_response_bytes + 1)
        if len(payload) > max_response_bytes:
            raise ValueError(f"Response exceeds {max_response_bytes} byte limit: {url}")
        return payload


def text_of(element: ET.Element | None) -> str:
    if element is None or element.text is None:
        return ""
    return " ".join(element.text.split())


def parse_feed(xml_bytes: bytes, source: dict[str, Any], feed_url: str) -> list[dict[str, Any]]:
    root = ET.fromstring(xml_bytes)
    items: list[dict[str, Any]] = []

    channel_items = root.findall(".//item")
    if channel_items:
        for item in channel_items:
            items.append(
                {
                    "source": source["name"],
                    "tier": source.get("tier"),
                    "country_or_region": source.get("country_or_region"),
                    "language": source.get("language"),
                    "categories": source.get("categories", []),
                    "title": text_of(item.find("title")),
                    "link": text_of(item.find("link")),
                    "published": text_of(item.find("pubDate")) or text_of(item.find("date")),
                    "summary": text_of(item.find("description")),
                    "feed_url": feed_url,
                }
            )
        return items

    ns = {"atom": "http://www.w3.org/2005/Atom"}
    for entry in root.findall(".//atom:entry", ns):
        link = ""
        link_el = entry.find("atom:link", ns)
        if link_el is not None:
            link = link_el.attrib.get("href", "")
        items.append(
            {
                "source": source["name"],
                "tier": source.get("tier"),
                "country_or_region": source.get("country_or_region"),
                "language": source.get("language"),
                "categories": source.get("categories", []),
                "title": text_of(entry.find("atom:title", ns)),
                "link": link,
                "published": text_of(entry.find("atom:updated", ns)) or text_of(entry.find("atom:published", ns)),
                "summary": text_of(entry.find("atom:summary", ns)),
                "feed_url": feed_url,
            }
        )
    return items


class LinkCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[dict[str, str]] = []
        self._current_href: str | None = None
        self._text_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        href = dict(attrs).get("href")
        if not href:
            return
        self._current_href = href
        self._text_parts = []

    def handle_data(self, data: str) -> None:
        if self._current_href:
            self._text_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() != "a" or not self._current_href:
            return
        text = " ".join(" ".join(self._text_parts).split())
        if text:
            self.links.append({"href": self._current_href, "text": html.unescape(text)})
        self._current_href = None
        self._text_parts = []


def looks_like_story(title: str, url: str) -> bool:
    title = " ".join(title.split())
    if len(title) < 18:
        return False
    lowered = title.lower()
    if lowered in {
        "home", "news", "world", "business", "subscribe", "sign in", "login", "media accreditation",
        "climate and environment", "politics and diplomacy", "press releases", "latest news", "top stories",
    }:
        return False
    if any(token in lowered for token in ("cookie", "privacy", "terms of use", "advertise", "newsletter")):
        return False
    if url.startswith(("mailto:", "javascript:", "#")):
        return False
    return True


def scrape_homepage(source: dict[str, Any], timeout: int, max_items: int = 30) -> list[dict[str, Any]]:
    homepage = source.get("homepage")
    if not homepage:
        return []
    raw = fetch(homepage, timeout)
    parser = LinkCollector()
    parser.feed(raw.decode("utf-8", errors="replace"))
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for link in parser.links:
        title = " ".join(link["text"].split())
        url = urllib.parse.urljoin(homepage, link["href"])
        url = url.split("#", 1)[0]
        if not looks_like_story(title, url) or url in seen:
            continue
        seen.add(url)
        url_date = ""
        date_match = re.search(r"/(20\d{2})-(\d{2})-(\d{2})/", url)
        if date_match:
            url_date = f"{date_match.group(1)}-{date_match.group(2)}-{date_match.group(3)}T00:00:00Z"
        items.append(
            {
                "source": source["name"],
                "tier": source.get("tier"),
                "country_or_region": source.get("country_or_region"),
                "language": source.get("language"),
                "categories": source.get("categories", []),
                "title": title,
                "link": url,
                "published": url_date,
                "summary": "",
                "feed_url": homepage,
                "source_method": "homepage_fallback",
            }
        )
        if len(items) >= max_items:
            break
    return items


def parse_published(value: str) -> datetime | None:
    clean = str(value or "").strip()
    if not clean:
        return None
    try:
        parsed = parsedate_to_datetime(clean)
    except (TypeError, ValueError, OverflowError):
        try:
            parsed = datetime.fromisoformat(clean.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def apply_freshness_filter(
    output: dict[str, Any],
    lookback_hours: int | None,
    reference_time: datetime,
) -> None:
    cutoff = reference_time - timedelta(hours=lookback_hours) if lookback_hours else None
    retained: list[dict[str, Any]] = []
    stale_count = 0
    undated_count = 0
    for item in output.get("items", []):
        published_at = parse_published(str(item.get("published") or ""))
        if published_at is None:
            item["freshness"] = "unknown"
            item["published_at"] = None
            item["age_hours"] = None
            undated_count += 1
            retained.append(item)
            continue
        age_hours = max(0.0, (reference_time - published_at).total_seconds() / 3600)
        item["published_at"] = published_at.isoformat()
        item["age_hours"] = round(age_hours, 2)
        item["freshness"] = "fresh" if cutoff is None or published_at >= cutoff else "stale"
        if cutoff is not None and published_at < cutoff:
            stale_count += 1
            continue
        retained.append(item)
    output["items"] = retained
    output["freshness"] = {
        "lookback_hours": lookback_hours,
        "reference_time": reference_time.isoformat(),
        "retained": len(retained),
        "stale_filtered": stale_count,
        "undated_retained": undated_count,
    }


def build_source_health(output: dict[str, Any], sources: list[dict[str, Any]]) -> None:
    item_counts: dict[str, int] = {}
    error_counts: dict[str, int] = {}
    for item in output.get("items", []):
        name = str(item.get("source") or "unknown")
        item_counts[name] = item_counts.get(name, 0) + 1
    for error in output.get("errors", []):
        name = str(error.get("source") or "unknown")
        error_counts[name] = error_counts.get(name, 0) + 1
    output["source_health"] = [
        {
            "source": source.get("name"),
            "tier": source.get("tier"),
            "items": item_counts.get(str(source.get("name")), 0),
            "errors": error_counts.get(str(source.get("name")), 0),
            "status": "healthy"
            if item_counts.get(str(source.get("name")), 0) > 0
            else "degraded"
            if error_counts.get(str(source.get("name")), 0) > 0
            else "discovery_only",
        }
        for source in sources
    ]


def collect(
    max_sources: int | None,
    timeout: int,
    homepage_fallback: bool,
    lookback_hours: int | None = 72,
    reference_time: datetime | None = None,
) -> dict[str, Any]:
    config = json.loads(SOURCES_PATH.read_text(encoding="utf-8"))
    output: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "items": [],
        "errors": [],
    }
    sources = config.get("sources", [])
    if max_sources is not None:
        sources = sources[:max_sources]
    seen: set[str] = set()

    for source in sources:
        source_item_count = 0
        source_feed_errors = 0
        for feed_url in source.get("rss", []):
            try:
                items = parse_feed(fetch(feed_url, timeout), source, feed_url)
            except (urllib.error.URLError, TimeoutError, ET.ParseError, OSError) as exc:
                output["errors"].append({"source": source.get("name"), "feed_url": feed_url, "error": str(exc)})
                source_feed_errors += 1
                continue
            for item in items:
                key = item.get("link") or f"{item.get('source')}|{item.get('title')}"
                if key in seen:
                    continue
                seen.add(key)
                output["items"].append(item)
                source_item_count += 1
        if homepage_fallback and source.get("rss") and source_item_count == 0 and source_feed_errors:
            try:
                fallback_items = scrape_homepage(source, timeout)
            except (urllib.error.URLError, TimeoutError, OSError, UnicodeError) as exc:
                output["errors"].append(
                    {"source": source.get("name"), "feed_url": source.get("homepage"), "error": f"homepage fallback failed: {exc}"}
                )
                continue
            for item in fallback_items:
                key = item.get("link") or f"{item.get('source')}|{item.get('title')}"
                if key in seen:
                    continue
                seen.add(key)
                output["items"].append(item)
            if fallback_items:
                output.setdefault("fallbacks", []).append(
                    {"source": source.get("name"), "method": "homepage_fallback", "items": len(fallback_items)}
                )
    apply_freshness_filter(output, lookback_hours, reference_time or datetime.now(timezone.utc))
    build_source_health(output, sources)
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Collect best-effort RSS items from the global briefing source matrix.")
    parser.add_argument("--max-sources", type=int, default=None)
    parser.add_argument("--timeout", type=int, default=12)
    parser.add_argument("--no-homepage-fallback", action="store_true")
    parser.add_argument("--lookback-hours", type=int, default=72, help="Discard dated items older than this window; use 0 to disable.")
    parser.add_argument("--no-intelligence", action="store_true", help="Skip clustered deep-research queue generation.")
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args(argv)

    result = collect(
        max_sources=args.max_sources,
        timeout=args.timeout,
        homepage_fallback=not args.no_homepage_fallback,
        lookback_hours=args.lookback_hours or None,
    )
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    output_path = args.output or DATA_DIR / f"rss-items-{datetime.now().strftime('%Y-%m-%d')}.json"
    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"items={len(result['items'])} errors={len(result['errors'])}")
    print(output_path)
    if not args.no_intelligence:
        from news_intelligence import build_intelligence_file

        intelligence_path = DATA_DIR / f"news-intelligence-{datetime.now().strftime('%Y-%m-%d')}.json"
        build_intelligence_file(output_path, intelligence_path)
        print(intelligence_path)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
