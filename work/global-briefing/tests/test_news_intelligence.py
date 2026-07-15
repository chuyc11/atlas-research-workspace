from __future__ import annotations

import importlib.util
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


RSS = load_module("rss_freshness_test_module", SCRIPTS / "rss_collect.py")
INTELLIGENCE = load_module("news_intelligence_test_module", SCRIPTS / "news_intelligence.py")


class NewsIntelligenceTests(unittest.TestCase):
    def test_freshness_filter_removes_old_items_and_quarantines_undated_discovery(self) -> None:
        output = {
            "items": [
                {"title": "fresh", "published": "Fri, 10 Jul 2026 12:00:00 GMT"},
                {"title": "stale", "published": "Mon, 01 Jun 2026 12:00:00 GMT"},
                {"title": "undated", "published": ""},
            ]
        }
        RSS.apply_freshness_filter(
            output,
            lookback_hours=72,
            reference_time=datetime(2026, 7, 11, 12, tzinfo=timezone.utc),
        )

        self.assertEqual({item["title"] for item in output["items"]}, {"fresh"})
        self.assertEqual({item["title"] for item in output["undated_items"]}, {"undated"})
        self.assertEqual(output["freshness"]["stale_filtered"], 1)
        self.assertEqual(output["freshness"]["undated_quarantined"], 1)

    def test_related_multi_region_sources_form_a_deep_research_dossier(self) -> None:
        discovery = {
            "items": [
                {
                    "source": "UN",
                    "tier": 1,
                    "country_or_region": "International",
                    "categories": ["energy", "security"],
                    "title": "Hormuz shipping attacks trigger new global energy warning",
                    "link": "https://un.example/hormuz-warning",
                    "published_at": "2026-07-11T01:00:00+00:00",
                    "age_hours": 2,
                },
                {
                    "source": "AP",
                    "tier": 2,
                    "country_or_region": "Global/US",
                    "categories": ["energy", "military"],
                    "title": "US demands Iran stop Hormuz shipping attacks",
                    "link": "https://ap.example/iran-hormuz",
                    "published_at": "2026-07-11T01:30:00+00:00",
                    "age_hours": 1.5,
                },
                {
                    "source": "Regional",
                    "tier": 3,
                    "country_or_region": "Middle East",
                    "categories": ["energy", "shipping"],
                    "title": "Iran responds as Hormuz shipping attacks disrupt energy route",
                    "link": "https://regional.example/hormuz-iran",
                    "published_at": "2026-07-11T02:00:00+00:00",
                    "age_hours": 1,
                },
                {
                    "source": "Tech",
                    "tier": 2,
                    "country_or_region": "US",
                    "categories": ["technology"],
                    "title": "New artificial intelligence model launches for developers",
                    "link": "https://tech.example/ai-model",
                    "published_at": "2026-07-11T02:00:00+00:00",
                    "age_hours": 1,
                },
            ],
            "source_health": [],
            "freshness": {"lookback_hours": 72},
        }
        settings = {
            "news_research_policy": {
                "mode": "deep",
                "core_story_count": 5,
                "secondary_story_count": 5,
                "minimum_sources_per_core_story": 3,
                "minimum_independent_domains_per_core_story": 2,
                "minimum_regions_per_geopolitical_story": 2,
                "require_primary_source_for_high_impact_story": True,
                "required_story_layers": ["confirmed_facts", "causal_chain", "counterevidence"],
            }
        }

        result = INTELLIGENCE.build_intelligence(discovery, settings, {"verification_sources": []})
        hormuz = next(item for item in result["all_dossiers"] if "Hormuz" in item["headline"])

        self.assertEqual(hormuz["source_count"], 3)
        self.assertEqual(hormuz["status"], "ready_for_synthesis")
        self.assertEqual(hormuz["verification_gaps"], [])
        self.assertIn("causal_chain", hormuz["analysis_requirements"])
        self.assertGreater(hormuz["importance_score"], 60)
        self.assertGreaterEqual(result["event_cluster_count"], 2)

    def test_sports_cluster_cannot_be_promoted_by_ambiguous_strike_term(self) -> None:
        discovery = {
            "items": [
                {
                    "source": f"Outlet {index}",
                    "tier": 2,
                    "country_or_region": region,
                    "categories": ["military", "economy"],
                    "title": title,
                    "link": f"https://source{index}.example/sports/world-cup-{index}",
                    "published_at": "2026-07-12T01:00:00+00:00",
                    "age_hours": index,
                }
                for index, (region, title) in enumerate(
                    [
                        ("Europe", "World Cup midfielder dies at 25"),
                        ("Africa", "World Cup player remembered before match"),
                        ("US", "Late strike sends team into World Cup semifinal"),
                        ("Asia", "World Cup quarterfinal match highlights"),
                    ],
                    1,
                )
            ]
        }
        settings = {
            "news_research_policy": {
                "core_story_count": 5,
                "secondary_story_count": 5,
                "minimum_sources_per_core_story": 3,
                "minimum_independent_domains_per_core_story": 2,
                "minimum_regions_per_geopolitical_story": 2,
                "require_primary_source_for_high_impact_story": True,
            }
        }

        result = INTELLIGENCE.build_intelligence(discovery, settings, {"verification_sources": []})
        dossier = result["all_dossiers"][0]

        self.assertTrue(dossier["score_components"]["low_priority"])
        self.assertFalse(dossier["core_eligible"])
        self.assertEqual(result["core_research_queue"], [])


if __name__ == "__main__":
    unittest.main()
