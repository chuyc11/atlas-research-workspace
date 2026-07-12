# Testing and Git Policy

## Test Scope

Always follow the task's explicit test policy. Do not run full pytest when the task says to defer it.

For v0.8.17, run targeted pytest only:

```powershell
$tests = @(Get-ChildItem tests -Filter 'test_a_share_owner_readiness_controlled_gate_reevaluation*.py') + @(Get-ChildItem tests -Filter 'test_a_share_controlled_gate_reevaluation*.py')
python -m pytest @($tests.FullName)
```

PowerShell does not expand pytest globs the same way POSIX shells do. If a literal glob causes `file or directory not found`, expand with `Get-ChildItem` and rerun the same targeted set.

Report v0.8.17 testing as:

- `full_pytest_run=false`
- `targeted_pytest_passed=true`
- `targeted_pytest_count=<actual>`
- `full_pytest_deferred_until=v0.9.0 or next big-version closeout`

## Useful Checks

Use these as appropriate:

```powershell
python -m compileall <new-package> src\trading_core\cli.py
python -m trading_core.cli --version
python -m trading_core.cli build-and-audit-... --as-of-date 2026-06-26 --mode <mode>
```

## Git Hygiene

- Check `git status --short` before edits, before commits, and at the end.
- Do not revert unrelated user changes.
- Keep implementation commit separate from release metadata commit when requested.
- Commit generated artifacts that are part of the release spec.
- Tag the release after the release commit.
- Verify `git tag --points-at HEAD` and clean worktree before final response.

