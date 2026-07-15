# Contributing to ATLAS

ATLAS is a composed workspace with three independently governed repositories.
Before editing, run `git status --short --branch` in the root, `src`, and
`work/trading-core`. Preserve unrelated changes in every worktree.

## Local setup

```powershell
python -m pip install -r requirements.txt
python -m pip install -r requirements-dev.txt
Set-Location src
npm ci
Set-Location ..
python atlas.py doctor
```

## Required checks

For root-owned Python and global-briefing changes:

```powershell
python -m ruff check atlas.py tests work/global-briefing/scripts work/global-briefing/tests
python -m pytest -q
```

For cross-repository changes:

```powershell
python atlas.py test
```

For a release candidate:

```powershell
python atlas.py test --full
python atlas.py cycle --date YYYY-MM-DD --dry-run
```

The complete trading-core suite is intentionally a release-tier check. Pull
requests should keep the fast root suite deterministic and under a minute.

## Engineering rules

- Keep real broker execution outside ATLAS; all execution facts remain virtual.
- Treat generated reports, market snapshots, ledgers, audits, and credentials
  as runtime data, never source code.
- Use atomic writes for canonical JSON/JSONL artifacts and fail closed on
  malformed or ambiguous financial inputs.
- Add a regression test for every defect fix.
- Do not import or execute code from `external_research`; it is reference-only.
- Record each repository commit and quality result in release evidence.
