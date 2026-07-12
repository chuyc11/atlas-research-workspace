# Trading Core Release Workflow

## Baseline

Start from `work/trading-core`.

Run:

```powershell
git status --short
git log --oneline -3
git tag --points-at HEAD
Get-Content VERSION
python -m trading_core.cli --version
```

If the user gives a pasted task file, read it first and treat it as the release spec. Confirm the current HEAD/tag/VERSION match the required baseline before implementing.

## Implementation Pattern

For a new file-backed package, mirror nearby modules:

- `<domain>_config.py`: target version, baseline version, recommended next version, modes, files, reports, boundary, dataclass config, paths.
- `input_availability.py`: required source paths, audit prerequisites, fail-close checks, source artifact map.
- `source_resolution.py`: preferred source and supporting sources.
- `date_alignment.py`: source `as_of_date` checks with `--allow-date-mismatch`.
- domain artifact modules: pure functions returning JSON-serializable dicts.
- source trace, boundary, manifest, reports, audit.
- builder: orchestrate validation, mode branching, artifact writes, report writes, and audit call.
- CLI: imports, subparsers, argument helper, main branches.
- tests: fixture helpers, module unit tests, CLI smoke, audit fail-close test.

Use `apply_patch` for manual edits. Do not rewrite unrelated modules or revert user changes.

## Artifact Generation

After implementation, run the exact CLI chain requested by the task. For v0.8.x A-share releases, this usually means:

```powershell
python -m trading_core.cli validate-... --as-of-date 2026-06-26
python -m trading_core.cli build-... --as-of-date 2026-06-26 --mode <mode>
python -m trading_core.cli audit-... --as-of-date 2026-06-26
python -m trading_core.cli build-and-audit-... --as-of-date 2026-06-26 --mode <mode>
```

Generated reports should avoid forbidden positive trading wording and state research-only / not-order-instruction boundaries.

## Release Metadata

Update only after implementation and artifact verification:

- `VERSION`
- `pyproject.toml`
- `src/trading_core/__init__.py`
- `README.md`
- `RELEASE_NOTES.md`
- relevant docs under `docs/`

Use two commits when the task asks for it:

```powershell
git commit -m "feat: ..."
git commit -m "chore: release ..."
git tag <target-version>
```

Finish by checking:

```powershell
git status --short
git tag --points-at HEAD
```

