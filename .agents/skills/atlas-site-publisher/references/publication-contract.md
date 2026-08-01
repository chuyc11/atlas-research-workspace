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
9. Package that exact build, calculate its SHA-256 and byte size, save one version, deploy, and poll to terminal success.
10. Persist the exact-schema credential-free publisher metadata described below.
11. Run the independent verifier against the fixed production origin and only then mark deployed with both artifacts; the verifier, not the publisher metadata, is authoritative.

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

## Publisher deployment metadata

`--mark-deployed` requires fresh JSON publisher metadata with exactly these fields and no extensions:

- `schema_version=1`, `status=succeeded`, timezone-aware `created_at`;
- the existing `project_id` and production `deployment_url`;
- `source_base_commit` fetched from Sites main and the different `published_commit` pushed after the isolated build;
- `artifact_sha256` and positive `artifact_size_bytes` for the exact deployed package;
- provider-returned `sites_version_id` and `sites_deployment_id`;
- `publication_manifest_sha256` and pending `content_hash`.

The metadata must be built from actual Git/build/Sites results without secrets. It is caller-authored and therefore cannot independently prove provider facts. A placeholder, unknown field, non-production URL, or missing provider ID keeps the candidate pending. The script archives its exact validated bytes and records `publisher_metadata_unverified`; a fresh fixed-origin live verification remains the authority for marking deployment.
