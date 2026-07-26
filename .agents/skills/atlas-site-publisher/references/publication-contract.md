# ATLAS publication contract

## Current site identity

- Existing Sites project ID: `appgprj_6a5041d5aca88191a3c6e2c9ebd03490`
- Production URL: `https://atlas-global-brief-2026.poetic-kiwi-4295.chatgpt.site`
- Source repository: the existing Sites main branch; never create a replacement project.

Confirm this identity against current Sites metadata and `work/global-briefing/data/site-sync-state.json` before deployment. Stop on disagreement.

## Frozen release inputs

The only workspace files eligible to cross into the isolated deployment worktree are:

- `src/app/briefing.generated.json` -> `app/briefing.generated.json`
- `src/app/publication.generated.json` -> `app/publication.generated.json`

The second file may already be byte-identical to Sites main and therefore absent from `git diff`; no other workspace file is allowed. The manifest must have `frozen=true`, a positive snapshot revision, and hashes matching the copied payload and retained snapshot.

## State matrix

| Sync status | Meaning | Action |
|---|---|---|
| `unchanged` | Deployed SHA already matches the dated report | Stop successfully; perform no hosting action |
| `changed` | A new frozen candidate was produced | Build and deploy from isolated worktree |
| `pending` | Exact candidate exists but lacks a successful deployed marker | Retry build/deploy with the same SHA and bytes |
| `error` | Sync or gate failed | Preserve report and pending state; stop |

After external deployment starts, do not rerun report generation, forecast recording, orders, or valuations.

## Isolation protocol

1. Acquire a fresh Sites source credential.
2. Fetch Sites main with per-command HTTP authorization into a uniquely named temporary ref. Do not persist the credential in Git configuration.
3. Create a detached temporary worktree below the resolved system temporary root.
4. Copy only the two allowlisted frozen inputs.
5. Require `git diff --name-only` to contain no path outside the allowlist.
6. Run `npm ci` only when dependencies are absent, using the existing lockfile.
7. Set `WRANGLER_LOG_PATH` on Windows; run `npm run quality` and the Sites build workflow.
8. Commit only the frozen inputs and push the detached commit to Sites main.
9. Package that exact build, save one version, deploy, and poll to terminal success.
10. Run the independent verifier and only then mark deployed.

Clean the temporary worktree, temporary ref, and package after verification. Before recursive cleanup, resolve each path and prove it remains under the intended system temporary directory.

## Verification binding

The verifier must derive and match at least:

- report date;
- content hash and payload SHA-256;
- publication snapshot revision and SHA-256;
- build and deployment identities;
- visible live content identity;
- HTTPS status and runtime-isolation policy.

The deployed marker is not authority by itself. A fresh passing verifier artifact is mandatory.
