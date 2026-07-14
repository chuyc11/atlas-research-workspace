from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "paper_theme_registry.py"
SPEC = importlib.util.spec_from_file_location("paper_theme_registry_test_module", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def entry(symbol: str, theme: str = "theme_a") -> dict:
    return {
        "account": "US",
        "symbol": symbol,
        "exchange": "NASDAQ",
        "primary_theme": theme,
        "secondary_themes": [],
        "status": "verified",
        "effective_from": "2026-07-01",
        "evidence": ["instrument mandate"],
    }


class PaperThemeRegistryTests(unittest.TestCase):
    def test_duplicate_keys_and_unknown_themes_fail_validation(self) -> None:
        payload = {
            "schema_version": 1,
            "allowed_themes": ["theme_a"],
            "entries": [entry("AAA"), entry("AAA", "unknown")],
        }

        errors = MODULE.validate_registry(payload)

        self.assertTrue(any("duplicate position key" in value for value in errors))
        self.assertTrue(any("primary_theme is not allowed" in value for value in errors))

    def test_verified_registry_theme_is_used_and_conflicts_fail_closed(self) -> None:
        payload = {"schema_version": 1, "allowed_themes": ["theme_a", "theme_b"], "entries": [entry("AAA")]}

        theme, source = MODULE.resolve_order_theme(
            payload,
            account="US",
            symbol="AAA",
            exchange="NASDAQ",
            date="2026-07-14",
            required=True,
        )

        self.assertEqual((theme, source), ("theme_a", "verified_registry"))
        with self.assertRaisesRegex(ValueError, "conflicts with verified registry"):
            MODULE.resolve_order_theme(
                payload,
                account="US",
                symbol="AAA",
                exchange="NASDAQ",
                date="2026-07-14",
                explicit_theme="theme_b",
                required=True,
            )

    def test_coverage_is_account_separated_and_value_weighted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            portfolio = root / "us.json"
            portfolio.write_text(json.dumps({
                "positions": {
                    "NASDAQ:AAA": {"symbol": "AAA", "exchange": "NASDAQ", "quantity": 10, "avg_cost": 10},
                    "NASDAQ:BBB": {"symbol": "BBB", "exchange": "NASDAQ", "quantity": 10, "avg_cost": 10},
                },
                "last_prices": {
                    "NASDAQ:AAA": {"price": 20, "fx_to_base": 1},
                    "NASDAQ:BBB": {"price": 10, "fx_to_base": 1},
                },
            }), encoding="utf-8")
            config = {"accounts": {"US": {"portfolio_file": "us.json"}}}
            payload = {"schema_version": 1, "allowed_themes": ["theme_a"], "entries": [entry("AAA")]}

            result = MODULE.coverage(config, payload, root, "2026-07-13")

        us = result["accounts"][0]
        self.assertEqual(us["position_count_coverage_pct"], 50.0)
        self.assertEqual(us["position_value_coverage_pct"], 66.67)
        self.assertFalse(result["all_accounts_fully_covered"])

    def test_revision_record_is_content_addressed_audited_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            registry_path = root / "config" / "registry.json"
            registry_path.parent.mkdir(parents=True)
            registry_path.write_text(json.dumps({
                "schema_version": 1,
                "allowed_themes": ["theme_a"],
                "entries": [entry("AAA")],
            }), encoding="utf-8")
            config = {
                "theme_registry_file": "config/registry.json",
                "theme_registry_history_file": "audit/history.jsonl",
                "theme_registry_snapshot_dir": "audit/snapshots",
                "accounts": {},
            }

            first = MODULE.record_revision(config, root, "2026-07-13", "Initial audited registry baseline")
            history_bytes = (root / "audit" / "history.jsonl").read_bytes()
            second = MODULE.record_revision(config, root, "2026-07-13", "Retry of audited registry baseline")
            second_history_bytes = (root / "audit" / "history.jsonl").read_bytes()
            audit = MODULE.audit_history(config, root, "2026-07-13")

        self.assertTrue(first["recorded"])
        self.assertTrue(second["unchanged"])
        self.assertEqual(second["revision_count"], 1)
        self.assertEqual(history_bytes, second_history_bytes)
        self.assertTrue(audit["audit_passed"])
        self.assertTrue(audit["history_current"])
        self.assertTrue(str(audit["current_revision_id"]).startswith("THEME-REG-"))

    def test_unrecorded_change_and_chain_tampering_fail_audit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            registry_path = root / "registry.json"
            payload = {"schema_version": 1, "allowed_themes": ["theme_a"], "entries": [entry("AAA")]}
            registry_path.write_text(json.dumps(payload), encoding="utf-8")
            config = {
                "theme_registry_file": "registry.json",
                "theme_registry_history_file": "history.jsonl",
                "theme_registry_snapshot_dir": "snapshots",
                "accounts": {},
            }
            MODULE.record_revision(config, root, "2026-07-13", "Initial audited registry baseline")
            payload["updated"] = "2026-07-14"
            registry_path.write_text(json.dumps(payload), encoding="utf-8")

            stale = MODULE.audit_history(config, root, "2026-07-14")

            registry_path.write_text((root / "snapshots" / f"{stale['latest_recorded_registry_sha256']}.json").read_text(encoding="utf-8"), encoding="utf-8")
            history_path = root / "history.jsonl"
            row = json.loads(history_path.read_text(encoding="utf-8"))
            row["reason"] = "Silently rewritten reason"
            history_path.write_text(json.dumps(row) + "\n", encoding="utf-8")
            tampered = MODULE.audit_history(config, root, "2026-07-14")

        self.assertFalse(stale["history_current"])
        self.assertTrue(stale["chain_valid"])
        self.assertFalse(stale["audit_passed"])
        self.assertFalse(tampered["chain_valid"])
        self.assertTrue(any("chain_hash" in error for error in tampered["history_errors"]))


if __name__ == "__main__":
    unittest.main()
