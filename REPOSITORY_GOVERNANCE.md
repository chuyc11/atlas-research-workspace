# ATLAS repository governance

ATLAS uses three intentionally separate repositories:

- the workspace root owns `atlas.py`, global briefing code, policies, control-plane tests, and root CI;
- `src` owns the production website and its Sites deployment workflow;
- `work/trading-core` owns the research execution engine and its tests.

Generated reports, market snapshots, ledgers, run audits, repair backups, and
local credentials are not committed. They are protected by the disaster-
recovery snapshot system and remain auditable through content hashes.

Changes that span repositories must pass each repository's own quality gate
and the root ATLAS cycle before production deployment. Dirty nested worktrees
must never be reset or included in an unrelated commit.

## Reproducibility contract

The root repository is an orchestration repository, not a self-contained
monorepo. A release workspace is reproducible only when all three repositories
have:

- an immutable commit recorded in the release evidence;
- at least one fetchable Git remote;
- a clean worktree at verification time; and
- successful results from the repository-specific quality gate.

`python atlas.py doctor` exposes these provenance signals without printing
remote URLs. A green root CI run covers root-owned control-plane and briefing
code only; it must never be presented as evidence that the website or
trading-core repositories passed.

Every non-dry-run cycle writes `work/shared/atlas/workspace-lock.json` with the
full commit, branch, clean/dirty state, remote names (never URLs), and a stable
content hash for all three repositories. A release candidate is false unless
that lock is reproducible. External disaster-recovery snapshots also contain
verified `git bundle` files for all three histories, but bundles are recovery
media and do not satisfy the fetchable-remote release requirement.

## Test tiers

- `python -m pytest -q`: fast, root-owned unit and control-plane tests.
- `python atlas.py test`: cross-repository integration and website build gate.
- `python atlas.py test --full`: release-candidate gate including the complete
  trading-core regression suite.
- `python atlas.py cycle --full-tests`: records full-suite evidence; it becomes
  a release candidate only on an identical idempotent rerun with reproducible
  repository provenance.

Third-party code under `work/trading-core/external_research` is reference-only
and is excluded from ATLAS test discovery and release artifacts.
