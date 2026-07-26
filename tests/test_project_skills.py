from __future__ import annotations

import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILLS_ROOT = ROOT / ".agents" / "skills"
ROUTER_PATH = ROOT / "work" / "global-briefing" / "config" / "skills.json"

PROJECT_SKILLS = {
    "atlas-daily-operator",
    "atlas-integrity-maintainer",
    "atlas-site-publisher",
    "global-briefing-runbook",
}


class ProjectSkillContractTests(unittest.TestCase):
    def test_orchestration_skills_are_discoverable_and_have_ui_metadata(self) -> None:
        router = json.loads(ROUTER_PATH.read_text(encoding="utf-8"))
        routed = {
            entry["name"] for entry in router.get("project_orchestration_skills", [])
        }
        self.assertEqual(routed, PROJECT_SKILLS)

        priorities = [
            entry["priority"] for entry in router["project_orchestration_skills"]
        ]
        self.assertEqual(priorities, sorted(set(priorities)))

        for name in sorted(PROJECT_SKILLS):
            with self.subTest(skill=name):
                skill_text = (SKILLS_ROOT / name / "SKILL.md").read_text(encoding="utf-8")
                metadata = re.match(r"\A---\s*\n(.*?)\n---", skill_text, re.DOTALL)
                self.assertIsNotNone(metadata)
                self.assertRegex(metadata.group(1), rf"(?m)^name:\s*{re.escape(name)}\s*$")
                self.assertRegex(metadata.group(1), r"(?m)^description:\s*\S.+$")
                self.assertNotIn("TODO", skill_text)

                interface = (SKILLS_ROOT / name / "agents" / "openai.yaml").read_text(
                    encoding="utf-8"
                )
                self.assertIn(f"${name}", interface)

    def test_phase_a_preflight_precedes_prediction_recording(self) -> None:
        runbook = (SKILLS_ROOT / "global-briefing-runbook" / "SKILL.md").read_text(
            encoding="utf-8"
        )
        self.assertLess(
            runbook.index("briefing_store.py validate-records"),
            runbook.index("briefing_store.py record"),
        )
        self.assertLess(
            runbook.index("briefing_store.py record"),
            runbook.index("research_quality.py"),
        )

    def test_daily_cycle_defers_publication_until_closed_loop_finishes(self) -> None:
        operator = (SKILLS_ROOT / "atlas-daily-operator" / "SKILL.md").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            "atlas.py cycle --date RUN_DATE --skip-site --skip-publication", operator
        )

    def test_site_publisher_copies_both_authoritative_generated_inputs(self) -> None:
        contract = (
            SKILLS_ROOT
            / "atlas-site-publisher"
            / "references"
            / "publication-contract.md"
        ).read_text(encoding="utf-8")
        self.assertIn("src/app/briefing.generated.json", contract)
        self.assertIn("src/app/publication.generated.json", contract)
        self.assertIn("fresh passing verifier artifact", contract)


if __name__ == "__main__":
    unittest.main()
