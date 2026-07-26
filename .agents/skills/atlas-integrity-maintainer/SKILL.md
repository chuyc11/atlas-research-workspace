---
name: atlas-integrity-maintainer
description: Audit, repair, and verify integrity across the ATLAS control plane, global-briefing, trading-core, and site. Use for deep project reviews, rollback or evidence defects, financial-correctness fixes, calendar expiry, clean-install/CI reproducibility, publication-identity failures, or safe remediation. Use atlas-daily-operator for routine daily orchestration and atlas-site-publisher for external Sites deployment.
---

# ATLAS Integrity Maintainer

Treat green tests and `overall_passed=true` fields as claims that require independent evidence. Preserve paper-only boundaries and fail closed when evidence is missing, stale, unsigned, date-misaligned, or not bound to the exact artifact.

## Start here

1. Locate the workspace root by finding `atlas.py`, `work/global-briefing`, `work/trading-core`, and `src`.
2. Read `references/architecture.md` before changing authority boundaries or publication flow.
3. Run `python .agents/skills/atlas-integrity-maintainer/scripts/atlas_preflight.py --root <root>`.
4. Inspect all three Git worktrees independently. Preserve unrelated user changes.
5. Classify the request as audit-only, repair, publication retry, or release/deploy. Do not infer deployment authorization from an audit request.

## Audit workflow

Verify behavior with negative experiments in temporary directories. Never mutate canonical workspace evidence for an attack simulation.

Check these invariants in order:

1. Canonical ledger events are append-only by full semantic hash, not only source locator or ID.
2. Ledger/history/backup/publication heads cannot be rolled back with an older valid signature.
3. Backup gates authenticate metadata and restore the exact encrypted archive.
4. Deployment evidence is obtained from a fresh verifier and is bound to visible content and artifact hashes.
5. Public portfolio, FX cost basis, attribution, prices, and dates come from locked core snapshots.
6. Trading calendars cover every active market and have at least 30 forward days; release readiness requires 90 days.
7. Clean wheel/npm installs contain all runtime dependencies and package data.
8. PR CI runs the full suite; selected smoke tests do not stand in for release evidence.

For a formal audit, write a Markdown report with stable finding IDs, severity, exact file/line evidence, reproduction, impact, fix, and acceptance test. Separate confirmed defects, historical defects already fixed, and unverified claims.

When the user requests three-agent review, send every completed report version to three reviewers with different emphases: integrity/security, financial/data semantics, and CI/release reproducibility. Any `CHANGES_REQUIRED` creates a new version and restarts all three reviews. Finalize only after three explicit `PASS` results.

## Repair workflow

1. Add a failing regression test that reproduces the semantic defect.
2. Fix the lowest shared authority layer. Do not patch only the public renderer when the core snapshot is wrong.
3. Remove or rename self-asserted flags that have no independent producer.
4. Keep evidence producer and verifier separate. Bind evidence to date, commit, content hash, and predecessor.
5. Run the smallest relevant tests, then repository quality gates, then clean-install tests.
6. Re-run the preflight. A publication failure caused by strict evidence is a correct fail-closed result, not permission to forge a manifest.

Use `references/acceptance.md` for exact acceptance layers and stop conditions.

## Publication and deployment

Do not equate pushed feature branches with active code. Require PR/default-branch CI before calling a fix merged. Require a frozen manifest with matching payload/snapshot hashes before building a release. Verify the live URL after deployment, then record deployment only from the fresh verifier output.

Prefer GitHub Artifact Attestations for build provenance. They do not provide a transparency log, so use an independent append-only witness such as Sigstore Rekor for anti-rollback anchors. Keep official exchange schedules authoritative; community calendar packages may only cross-check them.

## Safety boundaries

- Never enable real broker orders, margin, shorting, or broker connections.
- Never print secret values or persist subprocess stderr that can include credential-bearing remote URLs.
- Never delete or rewrite canonical evidence to make a gate green.
- Never mark publication deployed from a user-supplied JSON file without rerunning the verifier.
- Stop and report the blocker when a required key, remote witness, provider receipt, or deployment authority is unavailable.
