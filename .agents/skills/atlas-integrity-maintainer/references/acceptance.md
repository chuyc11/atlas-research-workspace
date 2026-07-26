# ATLAS acceptance layers

## Layer 1: targeted regressions

- Root: canonical semantic mutation, deletion/truncation, rollback, lock race, fake backup, fake deploy evidence.
- Global briefing: cross-market sessions, FX lot cost, attribution, finite numbers, provider units/routing, locked snapshots.
- Trading core: calendar horizon, T+1 lots, full-cost cash checks, purge/embargo, real release evidence, wheel install.
- Site: frozen manifest hash, production identity, malicious payload/XSS, visible-content verification.

## Layer 2: repository gates

Run root pytest/ruff/security checks, global-briefing tests, `npm run quality`, and the full trading-core suite. Preserve warnings that indicate calendar degradation or evidence fallback.

## Layer 3: clean environments

Build a trading-core wheel, install it into a new virtual environment, load all packaged configs, read/write Parquet, and run the installed console script. Run `npm ci` before site quality/build checks.

## Layer 4: release evidence

Require PR/default-branch CI, exact commit identities, nonempty test collection, full regression, artifact digest, and cryptographic provenance. Historical checked-in results are context only.

## Layer 5: production

Require `frozen=true`, matching payload/snapshot hashes, revision greater than zero, successful deployment, and a live verifier that derives identity from visible content or the deployed artifact. Record deployment only after this verification.

## Stop conditions

Do not claim completion if any P0 negative test passes open, calendar release horizon is below 90 days, a clean install fails, the current publication manifest is invalid, CI has not run on the target commit, or live verification fails.
