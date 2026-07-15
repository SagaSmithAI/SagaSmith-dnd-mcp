# SagaSmith D&D MCP

`SagaSmith-dnd-mcp` is a local MCP server that combines the SagaSmith Core D&D
runtime, the D&D skill pack, and the module-generation skill pack.

It owns its local state by default:

```
<workspace>/.sagasmith-dnd-mcp/
  data/ttrpgbase.db       SQLite campaigns, modules, rules, and FTS indexes
  data/chroma_db/         Local ChromaDB persistent store
  artifacts/modules/      Generated Markdown modules before import
  artifacts/rulebooks/    Content-addressed user rulebooks before indexing
```

No D&D client should write either database directly. Use MCP tools so SQLite
writes, migrations, module artifacts, and optional vector storage share one
well-defined process boundary.

## Install

From this directory:

```powershell
pip install -e ".[dev]"
```

For dense embedding and Chroma indexing, include the optional extra:

```powershell
pip install -e ".[dense,dev]"
```

## Run

```powershell
sagasmith-dnd-mcp
```

The server uses standard stdio MCP. Use the installed executable path when the
client cannot resolve `sagasmith-dnd-mcp` from `PATH`.

### Nanobot

This configuration was verified against the local Nanobot MCP client. It keeps
the MCP database and Chroma directory in the Nanobot workspace without adding
the D&D CLI or runtime to Nanobot itself:

```json
{
  "tools": {
    "mcpServers": {
      "sagasmith_dnd": {
        "command": "C:\\path\\to\\SagaSmith-dnd-mcp\\.venv\\Scripts\\sagasmith-dnd-mcp.exe",
        "args": [],
        "cwd": "C:\\path\\to\\SagaSmith-dnd-mcp",
        "env": {
          "SAGASMITH_DND_MCP_HOME": "C:\\path\\to\\nanobot-workspace\\.sagasmith-dnd-mcp"
        },
         "toolTimeout": 60,
         "injectPrincipal": true,
         "enabledTools": ["campaign_create", "campaign_get", "campaign_list", "character_get", "party_show", "continuity_context", "snapshot_create", "branch_create", "branch_checkout", "combat_start", "combat_attack_resolve", "combat_end_turn"]
      }
    }
  }
}
```

For a real agent run, use the server-owned tool profiles; exposing every tool
at once makes tool selection and context management less stable. Nanobot
wraps the tools with names such as
`mcp_sagasmith_dnd_campaign_create`. Its current client discovers static
resources but not resource templates, so use `skill_asset_list` and
`skill_asset_read` to reach dynamic references, data, and templates.

### Other MCP Clients

OpenClaw, Hermes, and other stdio-capable MCP clients use the same process
contract: `command` is `sagasmith-dnd-mcp` (or the absolute executable path),
`cwd` is this repository, and `SAGASMITH_DND_MCP_HOME` selects a client-owned
state directory. No SagaSmith CLI registration, direct SQLite access, or Agent
package dependency is required.

## MCP Surface

Tools include storage status/migration, campaign creation/listing, safe module
write/import/list/search, rule search, and validated D&D character operations.
The latter covers complete character sheets, wallet changes, inventory, equipment,
ammunition, active effects, spell preparation, resources, dice, ability checks,
and ability-score generation. D&D state is written only through these MCP tools;
clients do not need the D&D CLI or direct SQLite access.

Optional books use an explicit rule-pack lifecycle. User PDFs/Markdown/text are
staged from an allowlisted root with `rule_document_stage`, inspected with the
shared Core document parser, and indexed with `rule_document_import`.
`rule_ingest` remains a direct Markdown compatibility path. Neither makes text
executable. In the `lobby` phase, create a source-bound inactive draft with
`rule_pack_draft_from_source`, inspect its validation report,
then use `rule_pack_install`. A DM must explicitly pin an installed version with
`campaign_rule_pack_set`. `campaign_rules_explain` returns the exact branch lock,
fingerprint, mechanic ids, and citations used by settlement. Rule locks cannot
change during active combat, and snapshots restore only when every exact locked
version and checksum is still installed.
`campaign_rule_profile_set`, `campaign_rule_pack_set`, and
`campaign_rule_pack_remove` are campaign mutations: pass the latest
`expected_revision` and a stable `idempotency_key`, then use the returned
`campaign_revision` for the next write. Retries with the same key and payload
replay the original response; stale revisions fail without changing the lock.
Committed rule-aware mutations persist immutable evidence in the same mutation
group; `campaign_rule_receipts` can audit it after an optional pack is removed.

The current 2014/2024 engine is itself exposed as an immutable built-in pack,
`dnd5e.core.2014` or `dnd5e.core.2024`. Its fingerprint covers the preserved
combat, movement, reaction, damage, rest, spell, character, and MCP action-
economy boundaries. `campaign_rules_explain` reports this core pack and its
implementation/test coverage before listing optional extension mechanics.
Snapshots and branch heads also preserve that exact built-in core lock. A save
without a core lock, or one whose core fingerprint is unavailable after a runtime
upgrade, is rejected before any live campaign state is materialized; conversion
or an explicit reviewed relock must happen separately.

Skill documents are resources at `sagasmith://skill/{skill_id}`. Their bundled
references, data, and templates are listed with `skill_asset_list`, read with
`skill_asset_read`, and exposed using each returned `resource_uri`.
`dnd_dm` and `module_generator` are MCP prompts.

`sagasmith://skills/overview` is a static resource listing the installed skill
documents for clients that do not discover resource templates.

`module_write` is intentionally separate from `module_import`: generation
always leaves an editable Markdown artifact under `artifacts/modules` before it
enters a campaign index.

## Configuration

### Fresh smoke run

Use a new MCP home for each run; this intentionally does not migrate or rewrite
old campaign data:

```powershell
$env:PYTHONPATH = "$PWD\src;$PWD\..\sagasmith-core\src;$PWD\..\sagasmith-dnd\src"
python scripts\smoke_seed.py --home C:\tmp\sagasmith-dnd-smoke-01
```

The seed creates two PCs, one NPC, separate actor knowledge, a witnessed event,
an audited party wallet mutation, and a baseline snapshot.

`SAGASMITH_DND_MCP_HOME` moves all managed local state. By default it is
`<SagaSmith workspace>/.sagasmith-dnd-mcp`.

Set `SAGASMITH_DND_MCP_DENSE_ENABLED=1` after installing `[dense]` to enable
embedding-backed retrieval. The default remains SQLite FTS-only so the MCP can
start without loading an embedding model.

When `injectPrincipal` is enabled, the Nanobot transport supplies the caller
identity and the model must not provide it. Grant tools keep their target
`principal_id` separate from the authenticated caller.

`SAGASMITH_DATABASE_URL` may point the Core runtime to an external database.
`CHROMA_DB_URL` may point vector retrieval to a remote Chroma service. When
neither is set, this server uses its own SQLite file and Chroma directory.
`CHROMA_DB_PATH` is also respected when a different local Chroma directory is
needed.

Use `SAGASMITH_DND_SKILLS_DIR` and `SAGASMITH_MODULEGEN_SKILLS_DIR` to point at
different checkouts of the skill packs.

`SAGASMITH_DND_MCP_RULE_IMPORT_ROOTS` is an `os.pathsep`-separated allowlist for
user rulebook staging. Its default is `<SagaSmith workspace>/reference/DnD-Books`.
The server never imports an arbitrary model-selected path directly.
