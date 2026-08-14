# Long-term memory contract

The MCP server owns durable D&D campaign continuity. Agent workspace memory is
limited to user and table preferences; character sheets, world facts, events,
actor knowledge, module progress, and snapshots belong to the campaign database.

## Write path

Use `memory_change(action="commit")` after a resolved scene. It writes one event, zero or more
stable-keyed facts, zero or more actor-knowledge revisions, and an optional
snapshot in one database transaction. A failed item rolls back the entire unit.
Every call requires an idempotency key. Updates to existing facts or knowledge
also require the current `expected_revision_id`.

Use `memory_change` only for administrative fact maintenance:

- `add` creates a fact and assigns a canonical stable key when one is omitted.
- `upsert` targets `fact_key`; revising an existing fact requires
  `expected_revision_id` and preserves omitted revision fields.
- `revise` targets `memory_id` and creates a new immutable revision.
- `supersede` keeps history while removing the fact from default retrieval.

Character-sheet notes never contain actor knowledge or campaign facts. Public
`character_state_change` deliberately has no `memory_add` or `memory_resolve`
action; use stable-keyed `actor_knowledge_change(add/revise)` for subjective
knowledge and `memory_change` for objective facts or an atomic continuity event.

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
`memory_query(view="diagnostics")` for inactive revisions, orphan event references,
unsnapshotted events, checkpoint size, recap evidence, and Skill-manifest drift;
the diagnostic response contains no narrative content. Snapshot recaps always
retain a deterministic canonical delta. Optional generated presentation text must
cite player-safe event ids and cannot replace canonical restore evidence.

## NPC conversation settlement

An active NPC conversation is a recoverable runtime journal, not authoritative
campaign memory. Every understood speech event produces a stable, reviewable
ActorKnowledge candidate that records only that the speaker said the text; it
never promotes the statement itself to objective truth. NPC workers may also
propose actor-owned relationship, goal, commitment, and knowledge candidates.

`npc_conversation(action="get")` exposes the sanitized candidates with stable
`candidate_id` values and their activation, publication, and event provenance.
Closing accepts only `accepted_candidate_ids`; array indexes are not a protocol.
The accepted candidates, one complete DM conversation event, and their source
links commit atomically. Closing is rejected while an activation, publication,
or mechanic resolution remains unfinished.

The complete transcript is retained for DM audit and has bounded first-class
event retrieval text. NPC turn context expands only transcript segments that the
target NPC perceived, using the recorded audience decision; content the NPC did
not understand is never recovered through the DM transcript. ActorKnowledge
remains the durable personalized recall ledger.

Active journals are bounded to 200 public events and 4 MiB. Close and abort
replace the mutable journal with a compact terminal receipt containing a
compressed idempotent result. Terminal receipts expire after 30 days; the
authoritative event and accepted memory remain in the campaign database.
