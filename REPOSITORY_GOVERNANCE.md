# ATLAS repository governance

ATLAS uses three intentionally separate repositories:

- the workspace root owns `atlas.py`, global briefing code, policies, control-plane tests, and root CI;
- `src` owns the production website and its Sites deployment workflow;
- `work/trading-core` owns the research execution engine and its tests.

The root repository records `src` and `work/trading-core` as Git submodules.
This preserves independent ownership while making every root commit pin an
exact, fetchable revision of both components.

Generated reports, market snapshots, ledgers, run audits, repair backups, and
local credentials are not committed. They are protected by the disaster-
recovery snapshot system and remain auditable through content hashes.

Changes that span repositories must pass each repository's own quality gate
and the root ATLAS cycle before production deployment. Dirty nested worktrees
must never be reset or included in an unrelated commit.

## Reproducibility contract

The root repository is an orchestration repository, not a flattened monorepo.
A clean clone must use `--recurse-submodules` (or run `git submodule update
--init --recursive`). A release workspace is reproducible only when all three
repositories have:

- an immutable commit recorded in the release evidence;
- the exact local commit advertised by at least one remote ref (remote
  reachability alone is insufficient);
- a clean worktree at verification time; and
- successful results from the repository-specific quality gate.

`python atlas.py doctor` exposes these provenance signals without printing
remote URLs. The root-owned CI matrix covers the control plane and briefing
code; the composed-workspace job requires the `ATLAS_SUBMODULE_TOKEN` secret,
checks out the pinned private submodules, and runs the cross-repository gate.
Missing credentials fail explicitly rather than producing a partial-workspace
success. Neither result replaces each child repository's own quality workflow.

Every non-dry-run cycle writes `work/shared/atlas/workspace-lock.json` with the
full commit, branch, clean/dirty state, remote names (never URLs), and a stable
content hash for all three repositories. Cycle idempotency includes this hash,
so a code or commit change resets the repeated-run proof. A release candidate
is false unless that lock is reproducible. External disaster-recovery snapshots
also contain authenticated `git bundle` files for all three histories.
Production schema-4 archives are AES-256-GCM encrypted with a key supplied
outside the repository; the sidecar and latest index use purpose-separated
HMACs, and every predecessor is restored and checked before it is chained.
Bundles are recovery media and do not satisfy the advertised-commit release
requirement.

Publication is a second, explicit boundary. Unfrozen or failed candidates may
only be written to staging. A deployable payload must carry a schema-1 frozen
manifest that binds the report, site input, payload, prerequisite artifacts,
and all three repository commits. Production verification must observe the
payload SHA, snapshot revision/hash, candidate fingerprint, build ID, and
deployment ID from the live response; local values are never substituted for
missing production evidence.

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
