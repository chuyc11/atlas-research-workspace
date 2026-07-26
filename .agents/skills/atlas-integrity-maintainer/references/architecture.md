# ATLAS authority map

| Layer | Primary location | Authority | Must not be trusted as authority |
|---|---|---|---|
| Control plane | `atlas.py` | cycle ordering, locks, canonical ledger, run audit | self-declared stage booleans |
| Research/paper | `work/global-briefing` | source evidence, predictions, locked paper snapshots | site-side ledger reconstruction |
| Trading core | `work/trading-core` | calendar, accounting, replay, release evidence | copied version modules and tracked historical result JSON |
| Public site | `src` | rendering a frozen payload and deployment marker | mutable payload or self-referential HTML markers |
| External trust | configured trust/backup roots, CI attestation, transparency witness | monotonic heads and provenance | a second writable directory with the same ACL boundary |

## Cross-layer bindings

- A run audit must bind the exact canonical ledger content hash and predecessor.
- A publication candidate must bind the report date, payload SHA-256, source commits, gate artifacts, and snapshot revision.
- A backup must bind authenticated latest metadata, manifest, archive digest, restore result, and predecessor.
- A deployment result must bind the frozen publication identity, built artifact, live visible content, and deployment ID.

## Source-of-truth rule

Read portfolio and account state through the locked paper-trading snapshot API. Read trading sessions from a versioned exchange calendar. Read release success from actual subprocess/test/attestation evidence. Never recompute these independently in the site layer.
