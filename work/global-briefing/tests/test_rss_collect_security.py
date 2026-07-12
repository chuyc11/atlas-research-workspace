from __future__ import annotations

import importlib.util
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
    def __init__(self, payload: bytes, content_length: str | None = None) -> None:
        self.payload = payload
        self.headers = {} if content_length is None else {"Content-Length": content_length}

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        return self.payload if size < 0 else self.payload[:size]


class RssCollectSecurityTests(unittest.TestCase):
    def test_fetch_rejects_oversized_response_body(self) -> None:
        response = FakeResponse(b"x" * 11)
        with patch.object(MODULE.urllib.request, "urlopen", return_value=response):
            with self.assertRaisesRegex(ValueError, "exceeds 10 byte limit"):
                MODULE.fetch("https://example.test/feed", timeout=1, max_response_bytes=10)

    def test_feed_parser_rejects_entity_expansion(self) -> None:
        malicious = b'<!DOCTYPE rss [<!ENTITY x "expanded">]><rss><channel><title>&x;</title></channel></rss>'
        with self.assertRaises(EntitiesForbidden):
            MODULE.parse_feed(malicious, {"id": "test"}, "https://example.test/feed")
