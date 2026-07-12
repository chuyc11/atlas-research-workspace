---
name: trading-core-maintainer
description: Maintain the local trading-core project release workflow. Use when the user asks to continue, advance, audit, release, tag, or document trading-core versions; work on A-share full-market modules; generate or audit file-backed research artifacts; update CLI/docs/tests/version metadata; or preserve no-broker/no-real-order safety boundaries.
---

# Trading Core Maintainer

## Overview

Use this skill to move `work/trading-core` through small audited releases without losing the project's safety boundaries. Favor repo patterns, artifact-backed truth, targeted validation, and clean two-step implementation/release commits.

## Quick Start

1. Read the user's pasted task file before touching the repo.
2. Enter `work/trading-core` and verify baseline: `git status --short`, `git log --oneline -3`, `git tag --points-at HEAD`, `Get-Content VERSION`, and `python -m trading_core.cli --version`.
3. Search existing neighboring modules before creating new patterns: use `rg`, then read closest prior package, tests, CLI branches, docs, and generated artifacts.
4. Implement narrowly with `apply_patch`; keep no-broker/no-real-order boundaries explicit in config, boundary checks, reports, and audit.
5. Generate artifacts through CLI, audit them, run only the task-requested tests, update docs/version, commit, tag, and verify a clean worktree.

## References

- Read `references/release-workflow.md` when advancing a version, adding a package, generating artifacts, or preparing commits/tags.
- Read `references/a-share-owner-readiness.md` when the task touches v0.8.13+ owner-readiness, daily pack, quality exceptions, recovery, controlled reevaluation, readiness gates, or blocked/not-ready states.
- Read `references/testing-and-git.md` before running tests, committing, tagging, or reporting verification status.

## Project Invariants

- Treat trading-core as file-backed research infrastructure, not a broker or live-trading system.
- Do not generate real orders, broker connections, real account reads, buy/sell instruction language, old `run-daily` calls, or official forward dry-run day2 execution unless the user explicitly gives a later authorized task that changes the boundary.
- Preserve historical release truth. A passing audit may correctly represent `blocked`, `not_ready`, `skipped`, insufficient history, or warnings.
- Prefer existing package architecture: config, input availability, source resolution, date alignment, domain artifacts, source trace, boundary, manifest, reports, audit, builder, CLI, tests, docs.
- Keep generated artifacts deterministic enough for audit, but expect source trace timestamps and hashes to refresh after rebuilds.

## Communication

- Give short progress updates while exploring, before edits, before tests, and before commits.
- Final reports should include exact commands run, pass/fail status, commits, tags, and any deferred validation.
