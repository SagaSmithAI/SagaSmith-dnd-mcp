# Long-term memory contract

The MCP server owns durable D&D campaign continuity. Agent workspace memory is
limited to user and table preferences; character sheets, world facts, events,
actor knowledge, module progress, and snapshots belong to the campaign database.

## Write path

Use `continuity_commit` after a resolved scene. It writes one event, zero or more
stable-keyed facts, zero or more actor-knowledge revisions, and an optional
snapshot in one database transaction. A failed item rolls back the entire unit.
Every call requires an idempotency key. Updates to existing facts or knowledge
also require the current `expected_revision_id`.

Use `memory_change` only for administrative fact maintenance:

- `add` creates a fact; generated legacy keys are supported for compatibility.
- `upsert` targets `fact_key`; revising an existing fact requires
  `expected_revision_id` and preserves omitted revision fields.
- `revise` targets `memory_id` and creates a new immutable revision.
- `supersede` keeps history while removing the fact from default retrieval.

Character-sheet `notes.memories` is a deprecated import source, not an
authoritative campaign-memory store.

## Provenance and reads

Each continuity event records a deterministic SHA-256 manifest of installed D&D
and module-generation `SKILL.md` documents. Snapshots capture those events, so a
restore retains the workflow version that produced the outcome. `skill_list` and
`skill_asset_list` expose checksums for diagnostics.

Default `memory_query` results contain active revisions only. Set
`include_inactive` for audit history. Actor knowledge remains isolated by actor
authorization and disclosure scope; objective facts must never be used to infer
what a character knows.

`continuity_context` ranks all eligible ledgers under one `budget_chars` limit and
returns retrieval counts so truncation is visible. Owner/DM callers can use
`continuity_diagnostics` for inactive revisions, orphan event references,
unsnapshotted events, checkpoint size, recap evidence, and Skill-manifest drift;
the diagnostic response contains no narrative content. Snapshot recaps always
retain a deterministic canonical delta. Optional generated presentation text must
cite player-safe event ids and cannot replace canonical restore evidence.
