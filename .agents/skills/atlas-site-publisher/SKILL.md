---
name: atlas-site-publisher
description: Stage, resume, build, deploy, and verify the existing ATLAS briefing website from a frozen publication candidate. Use when site sync returns changed or pending, a deployment retry is required, or a completed daily briefing must be published without replaying research, predictions, paper orders, marks, or valuations. Enforce an isolated temporary Git worktree, exact frozen-artifact allowlist, fresh Sites credentials, live verification, and idempotent deployed-state marking.
---

# ATLAS Site Publisher

Publish only an already validated ATLAS candidate. The workspace `src` checkout is a producer, never the deployment checkout.

## Read First

1. Read `work/global-briefing/config/settings.json` and resolve `RUN_DATE` from its timezone.
2. Read `work/global-briefing/data/site-sync-state.json`, the dated publication snapshot, `src/app/briefing.generated.json`, and `src/app/publication.generated.json`.
3. Read [references/publication-contract.md](references/publication-contract.md) for the status matrix, current site identity, isolation protocol, and final verifier commands.
4. Load the installed Sites building and hosting skills before using Sites tooling.

## Sync Once

Run from the workspace root:

```powershell
python work\global-briefing\scripts\sync_briefing_site.py --date RUN_DATE
```

Parse its single JSON result.

- `unchanged`: finish successfully without build, commit, push, version creation, or deployment.
- `changed`: deploy the newly frozen candidate.
- `pending`: reuse the existing pending SHA and frozen bytes; do not regenerate Phase A.
- `error` or unknown: stop and preserve pending state.

Never rerun the sync command to paper over a build or hosting failure.

## Isolate the Build

Obtain a fresh source-write credential without printing or persisting it. Fetch the existing Sites main branch into a temporary automation ref and create a detached worktree under the system temporary directory.

Copy only the frozen release inputs listed in the publication contract. Verify:

- the payload `contentHash` equals the pending SHA;
- the manifest is frozen and its payload/snapshot identities match;
- `git diff --name-only` is a subset of the exact allowlist and includes every changed frozen input;
- no other dirty workspace file enters the isolated worktree.

Install from the existing lockfile only when dependencies are absent. Set `WRANGLER_LOG_PATH` on Windows, run the full project quality/build suite, commit the frozen inputs, and push that exact commit to the existing Sites main branch using per-command authorization.

## Deploy and Verify

Package the exact successful build, save one Sites version, deploy it to the existing project, and poll to a terminal state. Do not create a new Sites project. Persist a credential-free `DEPLOYMENT_RECEIPT` JSON outside the isolated worktree with schema version 1, status `succeeded`, UTC `created_at`, the existing project ID, production URL, fetched Sites-main base commit, pushed commit, exact package SHA-256 and byte size, Sites version ID, Sites deployment ID, frozen publication-manifest SHA-256, and pending content hash. Values must come from the build and hosting tool results; never invent them. This caller-produced receipt is audit metadata, not cryptographic provider proof; only the fresh fixed-origin live verifier authorizes the deployed marker.

After a successful deployment, run the independent live verifier. Mark deployed only with its fresh passing artifact:

```powershell
python work\global-briefing\scripts\verify_production_site.py --url PRODUCTION_URL --output VERIFICATION_ARTIFACT
python work\global-briefing\scripts\sync_briefing_site.py --mark-deployed PENDING_SHA --deployment-url PRODUCTION_URL --verification-artifact VERIFICATION_ARTIFACT --deployment-receipt DEPLOYMENT_RECEIPT
```

If build, tests, push, version save, deployment, polling, or live verification fails, do not mark deployed. Keep the pending candidate for an idempotent retry.

## Safety

- Never copy the whole dirty `src` tree.
- Never expose a credential in output, Git config, a remote URL, or durable logs.
- Never mark deployed from user-supplied or stale verification JSON.
- Never mark deployed without exact-schema credential-free publisher metadata; do not describe it as provider-authenticated proof.
- Treat the fresh verifier against the fixed production origin as deployment authority.
- Never let site publication change research, prediction, or paper-trading artifacts.
- Verify the resolved temporary path is under the system temporary root before cleanup.
- Stop and disclose any disagreement between control-plane and site publication gates; never choose the more permissive result merely to deploy.

Report `unchanged`, `deployed`, or `pending/failed`, the dated report path, and the production URL only when live verification passed.
