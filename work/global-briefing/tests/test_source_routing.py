from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def load(name: str):
    spec = importlib.util.spec_from_file_location(f"{name}_routing_test", SCRIPTS / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


CHINA = load("china_market")
RSS = load("rss_collect")


class SourceRoutingTests(unittest.TestCase):
    def test_tencent_is_primary_for_bounded_watchlist(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            watchlist = Path(temp) / "watchlist.json"
            watchlist.write_text(json.dumps({
                "data_sources": {"primary": "Tencent quote"},
                "indices": [{"symbol": "000001.SH", "name": "SSE", "market": "A_SHARE_INDEX"}],
            }), encoding="utf-8")
            primary = {"provider": "Tencent quote", "primary_provider": "Tencent quote", "items": [{"symbol": "000001.SH", "price": 3000}], "errors": []}
            with (
                patch.object(CHINA, "fetch_with_tencent", return_value=primary) as tencent,
                patch.object(CHINA, "fetch_with_akshare") as akshare,
                patch.object(CHINA, "apply_eastmoney_fallback", side_effect=lambda result, *_args, **_kwargs: result),
                patch.object(CHINA, "apply_yahoo_fallback", side_effect=lambda result, *_args, **_kwargs: result),
            ):
                result = CHINA.snapshot(45, False, 8, 8, True, 8, 2, 8, 2, True, watchlist)

            tencent.assert_called_once()
            akshare.assert_not_called()
            self.assertEqual(result["primary_provider"], "Tencent quote")
            self.assertEqual(result["errors"], [])

    def test_first_party_homepage_discovery_is_not_recorded_as_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            sources = Path(temp) / "sources.json"
            sources.write_text(json.dumps({"sources": [{
                "name": "Example",
                "homepage": "https://example.com/",
                "rss": [],
                "discovery_mode": "homepage",
                "tier": 2,
            }]}), encoding="utf-8")
            html = b'<a href="/2026-07-12/material-story">A sufficiently descriptive material story title</a>'
            with patch.object(RSS, "SOURCES_PATH", sources), patch.object(RSS, "fetch", return_value=html):
                result = RSS.collect(None, 1, True, lookback_hours=None)

            self.assertEqual(result["errors"], [])
            self.assertNotIn("fallbacks", result)
            self.assertEqual(result["items"][0]["source_method"], "homepage_discovery")

    def test_syndicated_discovery_keeps_provider_and_evidence_policy(self) -> None:
        source = {"name": "Publisher", "rss": ["https://example.com/rss"], "discovery_provider": "Aggregator", "evidence_policy": "Open original."}
        xml = b"<rss><channel><item><title>Headline</title><link>https://example.com/a</link></item></channel></rss>"

        item = RSS.parse_feed(xml, source, source["rss"][0])[0]

        self.assertEqual(item["source_method"], "syndicated_rss")
        self.assertEqual(item["discovery_provider"], "Aggregator")
        self.assertEqual(item["evidence_policy"], "Open original.")


if __name__ == "__main__":
    unittest.main()
