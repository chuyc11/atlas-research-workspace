from __future__ import annotations

import importlib.util
import gzip
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from defusedxml.common import EntitiesForbidden


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "rss_collect.py"
SPEC = importlib.util.spec_from_file_location("rss_collect_security_test_module", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class FakeResponse:
    def __init__(
        self,
        payload: bytes,
        content_length: str | None = None,
        url: str = "https://example.test/feed",
        content_encoding: str | None = None,
    ) -> None:
        self.payload = payload
        self.headers = {} if content_length is None else {"Content-Length": content_length}
        if content_encoding is not None:
            self.headers["Content-Encoding"] = content_encoding
        self.url = url

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        return self.payload if size < 0 else self.payload[:size]

    def geturl(self) -> str:
        return self.url


class FakeOpener:
    def __init__(self, response) -> None:
        self.response = response

    def open(self, *_args, **_kwargs):
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


class RssCollectSecurityTests(unittest.TestCase):
    def test_fetch_rejects_oversized_response_body(self) -> None:
        response = FakeResponse(b"x" * 11)
        with patch.object(MODULE, "_resolve_host_addresses", return_value={"93.184.216.34"}), patch.object(
            MODULE.urllib.request,
            "build_opener",
            return_value=FakeOpener(response),
        ):
            with self.assertRaisesRegex(ValueError, "exceeds 10 byte limit"):
                MODULE.fetch("https://example.test/feed", timeout=1, max_response_bytes=10)

    def test_fetch_decompresses_gzip_before_feed_parsing(self) -> None:
        xml = b"<rss><channel><item><title>Compressed item</title></item></channel></rss>"
        response = FakeResponse(gzip.compress(xml), content_encoding="gzip")
        with patch.object(MODULE, "_resolve_host_addresses", return_value={"93.184.216.34"}), patch.object(
            MODULE.urllib.request,
            "build_opener",
            return_value=FakeOpener(response),
        ):
            payload = MODULE.fetch("https://example.test/feed", timeout=1)
        self.assertEqual(payload, xml)

    def test_fetch_rejects_gzip_expansion_over_limit(self) -> None:
        response = FakeResponse(gzip.compress(b"x" * 33), content_encoding="gzip")
        with patch.object(MODULE, "_resolve_host_addresses", return_value={"93.184.216.34"}), patch.object(
            MODULE.urllib.request,
            "build_opener",
            return_value=FakeOpener(response),
        ):
            with self.assertRaisesRegex(ValueError, "Decompressed response exceeds 32 byte limit"):
                MODULE.fetch("https://example.test/feed", timeout=1, max_response_bytes=32)

    def test_feed_parser_rejects_entity_expansion(self) -> None:
        malicious = b'<!DOCTYPE rss [<!ENTITY x "expanded">]><rss><channel><title>&x;</title></channel></rss>'
        with self.assertRaises(EntitiesForbidden):
            MODULE.parse_feed(malicious, {"id": "test"}, "https://example.test/feed")

    def test_fetch_rejects_private_and_non_https_destinations(self) -> None:
        with patch.object(MODULE, "_resolve_host_addresses", return_value={"127.0.0.1"}):
            with self.assertRaisesRegex(MODULE.UnsafeUrlError, "non-public"):
                MODULE.fetch("https://internal.test/feed", timeout=1)
        with self.assertRaisesRegex(MODULE.UnsafeUrlError, "HTTPS"):
            MODULE.fetch("http://example.test/feed", timeout=1)

    def test_fetch_rejects_redirect_to_unallowlisted_host(self) -> None:
        redirect = MODULE.urllib.error.HTTPError(
            "https://example.test/feed",
            302,
            "Found",
            {"Location": "https://127.0.0.1/latest"},
            None,
        )
        with patch.object(MODULE, "_resolve_host_addresses", return_value={"93.184.216.34"}), patch.object(
            MODULE.urllib.request,
            "build_opener",
            return_value=FakeOpener(redirect),
        ):
            with self.assertRaisesRegex(MODULE.UnsafeUrlError, "not allowlisted"):
                MODULE.fetch("https://example.test/feed", timeout=1)

    def test_collect_isolates_security_failure_to_one_source(self) -> None:
        malicious = b'<!DOCTYPE rss [<!ENTITY x "expanded">]><rss><channel><item><title>&x;</title></item></channel></rss>'
        valid = b"<rss><channel><item><title>Valid item</title><link>https://two.test/item</link></item></channel></rss>"
        config = {
            "sources": [
                {"name": "one", "homepage": "https://one.test", "rss": ["https://one.test/feed"]},
                {"name": "two", "homepage": "https://two.test", "rss": ["https://two.test/feed"]},
            ]
        }
        with self.subTest("batch continues"):
            from tempfile import TemporaryDirectory

            with TemporaryDirectory() as directory:
                path = Path(directory) / "sources.json"
                path.write_text(json.dumps(config), encoding="utf-8")
                with patch.object(MODULE, "SOURCES_PATH", path), patch.object(MODULE, "fetch", side_effect=[malicious, valid]):
                    result = MODULE.collect(max_sources=None, timeout=1, homepage_fallback=False)
        self.assertEqual(len(result["errors"]), 1)
        self.assertEqual(result["items"], [])
        self.assertEqual([item["source"] for item in result["undated_items"]], ["two"])
