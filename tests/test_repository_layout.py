from __future__ import annotations

import configparser
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class RepositoryLayoutTests(unittest.TestCase):
    def test_component_repositories_are_pinned_as_https_submodules(self) -> None:
        config = configparser.ConfigParser()
        config.read(ROOT / ".gitmodules", encoding="utf-8")
        expected = {
            "src": "https://github.com/chuyc11/atlas-global-briefing-site.git",
            "work/trading-core": "https://github.com/chuyc11/atlas-trading-core.git",
        }
        for path, url in expected.items():
            section = f'submodule "{path}"'
            self.assertEqual(config[section]["path"], path)
            self.assertEqual(config[section]["url"], url)

        completed = subprocess.run(
            ["git", "ls-files", "--stage", "--", *expected],
            cwd=ROOT,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=True,
        )
        entries = {
            line.split("\t", 1)[1]: line.split(maxsplit=1)[0]
            for line in completed.stdout.splitlines()
            if "\t" in line
        }
        self.assertEqual(entries, {path: "160000" for path in expected})


if __name__ == "__main__":
    unittest.main()
