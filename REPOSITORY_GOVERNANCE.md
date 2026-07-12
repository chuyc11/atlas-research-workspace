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

