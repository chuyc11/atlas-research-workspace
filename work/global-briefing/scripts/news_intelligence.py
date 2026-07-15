#!/usr/bin/env python3
"""Cluster fresh news discovery into evidence dossiers and a deep-research queue."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


SCRIPT_PATH = Path(__file__).resolve()
ROOT = SCRIPT_PATH.parents[3]
SETTINGS_PATH = ROOT / "work" / "global-briefing" / "config" / "settings.json"
SOURCES_PATH = ROOT / "work" / "global-briefing" / "config" / "sources.json"
DATA_DIR = ROOT / "work" / "global-briefing" / "data"

TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9'-]{2,}|[\u4e00-\u9fff]{2,}")
STOPWORDS = {
    "the", "and", "for", "who", "why", "how", "has", "had", "was", "were", "are", "but", "not", "its",
    "his", "her", "our", "out", "off", "far", "won", "than", "about", "after", "again", "against", "amid", "among", "been", "before", "being", "could",
    "from", "have", "into", "latest", "more", "news", "over", "says", "that", "their", "there",
    "these", "this", "through", "under", "what", "when", "where", "which", "while", "with", "world",
    "would", "your", "live", "update", "updates", "report", "reports", "watch", "video", "analysis",
    "new", "first", "last", "year", "years", "day", "days", "plan", "plans", "explains",
    "inside", "across", "million", "billion", "today", "tonight", "people", "puts", "good",
}
IMPACT_TERMS = {
    "war", "attack", "strike", "missile", "nuclear", "sanction", "tariff", "election", "ceasefire",
    "oil", "gas", "shipping", "hormuz", "inflation", "rates", "central", "bank", "recession", "trade",
    "china", "russia", "ukraine", "iran", "israel", "nato", "ai", "chip", "semiconductor", "climate",
    "heatwave", "earthquake", "wildfire", "flood", "storm", "blackout", "outbreak", "ebola", "pandemic",
    "regulation", "regulator", "lawsuit", "court", "privacy", "default", "supply",
}
MARKET_CATEGORIES = {"economy", "energy", "technology", "markets", "macro", "finance", "supply_chain", "military"}
SUBSTANTIVE_CATEGORIES = MARKET_CATEGORIES | {
    "politics", "security", "health", "public_health", "climate", "environment", "humanitarian", "nuclear",
    "middle_east", "china", "asia", "africa", "europe",
}
LOW_PRIORITY_TERMS = {
    "prince", "princess", "king", "queen", "royal", "football", "midfielder", "striker", "player", "coach",
    "singer", "actor", "celebrity", "review", "game", "gaming", "dating", "concert", "sport", "sports",
}
LOW_PRIORITY_PHRASES = {
    "world cup", "semi-final", "semifinal", "quarterfinal", "match highlights", "/sport/", "/sports/",
}
GENERIC_TITLES = {
    "media accreditation", "climate and environment", "politics and diplomacy", "press releases", "latest news",
    "top stories", "news and events", "world news", "business news", "peace and security", "tech now", "tech life",
}
TOPIC_TERMS = {
    "technology": {"ai", "apple", "openai", "chip", "semiconductor", "technology", "cyber", "deepfake", "privacy", "model"},
    "climate": {"wildfire", "climate", "heatwave", "flood", "storm", "drought", "temperature", "weather"},
    "health": {"health", "outbreak", "ebola", "pandemic", "disease", "hospital", "cancer", "virus"},
    "energy": {"oil", "gas", "energy", "electricity", "blackout", "shipping", "hormuz", "tanker", "fuel"},
    "military": {"war", "attack", "strike", "missile", "military", "defence", "defense", "nato", "drone", "ceasefire"},
    "economy": {"economy", "inflation", "rates", "bank", "tariff", "trade", "recession", "debt", "currency"},
    "politics": {"election", "president", "minister", "government", "parliament", "leader", "diplomacy", "sanction"},
    "china": {"china", "chinese", "beijing", "hong", "shanghai", "shenzhen", "pboc"},
}


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def tokens(title: str) -> set[str]:
    return {
        token.lower().strip("'-")
        for token in TOKEN_RE.findall(title)
        if len(token.strip("'-")) >= 3 and token.lower().strip("'-") not in STOPWORDS
    }


def is_generic_discovery(item: dict[str, Any]) -> bool:
    title = " ".join(str(item.get("title") or "").lower().split())
    if title in GENERIC_TITLES:
        return True
    if item.get("source_method") == "homepage_fallback" and len(tokens(title)) < 3:
        return True
    return False


def is_low_priority_cluster(items: list[dict[str, Any]]) -> bool:
    """Detect lifestyle/sports clusters before ambiguous words such as 'strike' can inflate impact."""
    raw_text = " ".join(
        f"{item.get('title') or ''} {item.get('link') or ''}"
        for item in items
    ).lower()
    title_tokens = tokens(" ".join(str(item.get("title") or "") for item in items))
    return bool(title_tokens & LOW_PRIORITY_TERMS) or any(phrase in raw_text for phrase in LOW_PRIORITY_PHRASES)


def domain(item: dict[str, Any]) -> str:
    return urlparse(str(item.get("link") or "")).netloc.lower().removeprefix("www.")


def source_key(item: dict[str, Any]) -> str:
    return domain(item) or str(item.get("source") or "unknown").lower()


def similarity(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    overlap = len(left & right)
    union = len(left | right)
    jaccard = overlap / union if union else 0.0
    containment = overlap / min(len(left), len(right))
    return max(jaccard, containment * 0.72)


def cluster_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    prepared = []
    for item in items:
        if is_generic_discovery(item):
            continue
        title = " ".join(str(item.get("title") or "").split())
        item_tokens = tokens(title)
        if len(item_tokens) < 2:
            continue
        prepared.append((item, item_tokens))
    prepared.sort(key=lambda pair: (pair[0].get("age_hours") is None, pair[0].get("age_hours") or 0))

    clusters: list[dict[str, Any]] = []
    for item, item_tokens in prepared:
        best_index = -1
        best_score = 0.0
        for index, cluster in enumerate(clusters):
            score = max(
                (similarity(item_tokens, tokens(str(existing.get("title") or ""))) for existing in cluster["items"]),
                default=0.0,
            )
            if score > best_score:
                best_index, best_score = index, score
        if best_index >= 0 and best_score >= 0.34:
            cluster = clusters[best_index]
            cluster["items"].append(item)
            cluster["tokens"].update(item_tokens)
        else:
            clusters.append({"items": [item], "tokens": set(item_tokens)})
    merged: list[dict[str, Any]] = []
    for cluster in clusters:
        best_index = -1
        best_score = 0.0
        best_overlap = 0
        for index, existing in enumerate(merged):
            pairs = [
                (
                    similarity(tokens(str(left.get("title") or "")), tokens(str(right.get("title") or ""))),
                    len(tokens(str(left.get("title") or "")) & tokens(str(right.get("title") or ""))),
                )
                for left in cluster["items"]
                for right in existing["items"]
            ]
            score, overlap = max(pairs, default=(0.0, 0))
            if overlap >= 3 and score > best_score:
                best_index, best_score, best_overlap = index, score, overlap
        if best_index >= 0 and best_overlap >= 3 and best_score >= 0.22:
            merged[best_index]["items"].extend(cluster["items"])
            merged[best_index]["tokens"].update(cluster["tokens"])
        else:
            merged.append(cluster)
    return merged


def representative_title(items: list[dict[str, Any]]) -> str:
    ranked = sorted(
        items,
        key=lambda item: (
            int(item.get("tier") or 9),
            item.get("age_hours") is None,
            item.get("age_hours") or 0,
            -len(str(item.get("title") or "")),
        ),
    )
    return str(ranked[0].get("title") or "Untitled event")


def cluster_score(cluster: dict[str, Any]) -> tuple[float, dict[str, float]]:
    items = cluster["items"]
    source_keys = {source_key(item) for item in items}
    regions = {str(item.get("country_or_region") or "unknown") for item in items}
    tiers = [int(item.get("tier") or 3) for item in items]
    ages = [float(item["age_hours"]) for item in items if item.get("age_hours") is not None]
    categories = {str(value) for item in items for value in item.get("categories", [])}
    cluster_tokens = cluster["tokens"]
    strategic_term_count = len(cluster_tokens & IMPACT_TERMS)

    quality = 25 if 1 in tiers else 18 if 2 in tiers else 8
    confirmation = min(22, max(0, len(source_keys) - 1) * 8)
    freshness = 15 if ages and min(ages) <= 12 else 11 if ages and min(ages) <= 24 else 6 if ages else 2
    global_impact = min(15, len(cluster_tokens & IMPACT_TERMS) * 3)
    market_relevance = 13 if categories & MARKET_CATEGORIES else 5
    diversity = min(10, max(0, len(regions) - 1) * 5)
    novelty = min(10, math.log2(len(cluster_tokens) + 1) * 2)
    low_priority = is_low_priority_cluster(items)
    editorial_relevance = 12 if cluster_tokens & IMPACT_TERMS else 7 if categories & SUBSTANTIVE_CATEGORIES else 0
    if low_priority:
        editorial_relevance -= 35
    components = {
        "source_quality": quality,
        "cross_source_confirmation": confirmation,
        "freshness": freshness,
        "global_impact": global_impact,
        "market_relevance": market_relevance,
        "regional_diversity": diversity,
        "novelty": round(novelty, 2),
        "editorial_relevance": editorial_relevance,
        "strategic_term_count": strategic_term_count,
        "low_priority": low_priority,
    }
    return round(max(0.0, min(100.0, sum(components.values()))), 2), components


def dominant_topic(items: list[dict[str, Any]]) -> str:
    title_tokens = tokens(" ".join(str(item.get("title") or "") for item in items))
    priority = ["military", "energy", "technology", "climate", "health", "economy", "politics", "china"]
    scored_topics = sorted(
        ((len(title_tokens & TOPIC_TERMS[topic]), -priority.index(topic), topic) for topic in priority),
        reverse=True,
    )
    if scored_topics and scored_topics[0][0] > 0:
        return scored_topics[0][2]
    categories = Counter(str(value) for item in items for value in item.get("categories", []))
    topic_order = [
        "military", "security", "energy", "economy", "macro", "technology", "china", "climate", "environment",
        "health", "public_health", "politics", "humanitarian", "society",
    ]
    for topic in topic_order:
        if categories.get(topic):
            return topic
    return categories.most_common(1)[0][0] if categories else "general"


def story_family(items: list[dict[str, Any]], topic: str) -> str:
    title_tokens = tokens(" ".join(str(item.get("title") or "") for item in items))
    families = [
        ("iran_hormuz", {"iran", "hormuz"}),
        ("russia_ukraine", {"russia", "russian", "ukraine", "ukrainian"}),
        ("apple_openai", {"apple", "openai"}),
        ("extreme_weather", {"wildfire", "heatwave", "typhoon", "flood", "storm"}),
        ("china_policy", {"china", "chinese", "beijing", "pboc"}),
    ]
    for family, terms in families:
        if title_tokens & terms:
            return family
    return topic


def select_diverse(dossiers: list[dict[str, Any]], count: int, per_topic_limit: int = 2) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    topic_counts: Counter[str] = Counter()
    family_counts: Counter[str] = Counter()
    for dossier in dossiers:
        if not dossier.get("core_eligible"):
            continue
        topic = str(dossier.get("dominant_topic") or "general")
        family = str(dossier.get("story_family") or topic)
        if topic_counts[topic] >= per_topic_limit or family_counts[family] >= 2:
            continue
        selected.append(dossier)
        topic_counts[topic] += 1
        family_counts[family] += 1
        if len(selected) >= count:
            break
    if len(selected) < count:
        for dossier in dossiers:
            if dossier in selected or not dossier.get("core_eligible"):
                continue
            selected.append(dossier)
            if len(selected) >= count:
                break
    return selected


def verification_gaps(items: list[dict[str, Any]], policy: dict[str, Any]) -> list[str]:
    gaps: list[str] = []
    tiers = {int(item.get("tier") or 3) for item in items}
    domains = {source_key(item) for item in items}
    regions = {str(item.get("country_or_region") or "unknown") for item in items}
    if policy.get("require_primary_source_for_high_impact_story", True) and 1 not in tiers:
        gaps.append("primary_source_missing")
    if len(domains) < int(policy.get("minimum_independent_domains_per_core_story", 2)):
        gaps.append("independent_confirmation_missing")
    if len(regions) < int(policy.get("minimum_regions_per_geopolitical_story", 2)):
        gaps.append("cross_region_perspective_missing")
    if all(item.get("published_at") is None for item in items):
        gaps.append("publication_time_unknown")
    return gaps


def search_queries(title: str, cluster_tokens: set[str], gaps: list[str], verification_sources: list[dict[str, Any]]) -> list[str]:
    key_terms = " ".join(sorted(cluster_tokens & IMPACT_TERMS)[:5]) or " ".join(list(sorted(cluster_tokens))[:5])
    queries = [f'"{title}" latest verification', f"{key_terms} official statement data latest"]
    if "primary_source_missing" in gaps:
        relevant = []
        for source in verification_sources:
            source_categories = {str(value) for value in source.get("categories", [])}
            if source_categories & ({"energy", "shipping", "security"} if {"oil", "hormuz", "shipping"} & cluster_tokens else MARKET_CATEGORIES):
                relevant.append(str(source.get("search_hint") or ""))
        queries.extend(value for value in relevant[:2] if value)
    if "cross_region_perspective_missing" in gaps:
        queries.append(f"{key_terms} regional response local media")
    return list(dict.fromkeys(queries))[:5]


def build_intelligence(
    discovery: dict[str, Any],
    settings: dict[str, Any],
    sources: dict[str, Any],
) -> dict[str, Any]:
    policy = settings.get("news_research_policy", {})
    verification_sources = sources.get("verification_sources", [])
    dossiers: list[dict[str, Any]] = []
    for cluster in cluster_items(discovery.get("items", [])):
        items = cluster["items"]
        score, score_components = cluster_score(cluster)
        title = representative_title(items)
        topic = dominant_topic(items)
        gaps = verification_gaps(items, policy)
        independent_sources = sorted({source_key(item) for item in items})
        regions = sorted({str(item.get("country_or_region") or "unknown") for item in items})
        digest = hashlib.sha256("|".join(sorted(str(item.get("link") or item.get("title")) for item in items)).encode()).hexdigest()[:12]
        dossiers.append(
            {
                "event_id": f"NEWS-{digest}",
                "headline": title,
                "importance_score": score,
                "score_components": score_components,
                "dominant_topic": topic,
                "story_family": story_family(items, topic),
                "core_eligible": score >= 55 and score_components.get("strategic_term_count", 0) > 0 and score_components.get("editorial_relevance", 0) > 0,
                "status": "ready_for_synthesis" if not gaps and len(items) >= int(policy.get("minimum_sources_per_core_story", 3)) else "needs_deep_verification",
                "source_count": len(independent_sources),
                "independent_sources": independent_sources,
                "regions": regions,
                "tier1_count": sum(1 for item in items if int(item.get("tier") or 3) == 1),
                "verification_gaps": gaps,
                "research_queries": search_queries(title, cluster["tokens"], gaps, verification_sources),
                "analysis_requirements": policy.get("required_story_layers", []),
                "evidence": [
                    {
                        "source": item.get("source"),
                        "tier": item.get("tier"),
                        "region": item.get("country_or_region"),
                        "title": item.get("title"),
                        "url": item.get("link"),
                        "published_at": item.get("published_at"),
                        "age_hours": item.get("age_hours"),
                    }
                    for item in sorted(items, key=lambda value: (int(value.get("tier") or 9), value.get("age_hours") is None, value.get("age_hours") or 0))[:12]
                ],
            }
        )
    dossiers.sort(key=lambda value: (-float(value["importance_score"]), -int(value["source_count"]), value["headline"]))
    core_count = int(policy.get("core_story_count", 5))
    secondary_count = int(policy.get("secondary_story_count", 5))
    core_queue = select_diverse(dossiers, core_count)
    secondary_pool = [item for item in dossiers if item not in core_queue and item.get("core_eligible")]
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": policy.get("mode", "deep"),
        "discovery_item_count": len(discovery.get("items", [])),
        "event_cluster_count": len(dossiers),
        "core_research_queue": core_queue,
        "secondary_research_queue": secondary_pool[:secondary_count],
        "all_dossiers": dossiers,
        "source_health": discovery.get("source_health", []),
        "freshness": discovery.get("freshness", {}),
        "required_story_layers": policy.get("required_story_layers", []),
        "warning": "Discovery clustering is a research queue, not confirmation. Core claims still require opening and verifying the linked primary and independent sources.",
    }


def build_intelligence_file(discovery_path: Path, output_path: Path) -> dict[str, Any]:
    result = build_intelligence(
        load_json(discovery_path, {}),
        load_json(SETTINGS_PATH, {}),
        load_json(SOURCES_PATH, {}),
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build clustered deep-research dossiers from RSS discovery output.")
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args(argv)
    output = args.output or DATA_DIR / f"news-intelligence-{datetime.now().strftime('%Y-%m-%d')}.json"
    result = build_intelligence_file(args.input, output)
    print(f"clusters={result['event_cluster_count']} core={len(result['core_research_queue'])}")
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
