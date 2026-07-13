# SagaSmith D&D MCP

`SagaSmith-dnd-mcp` is a local MCP server that combines the SagaSmith Core D&D
runtime, the D&D skill pack, and the module-generation skill pack.

It owns its local state by default:

```
<workspace>/.sagasmith-dnd-mcp/
  data/ttrpgbase.db       SQLite campaigns, modules, rules, and FTS indexes
  data/chroma_db/         Local ChromaDB persistent store
  artifacts/modules/      Generated Markdown modules before import
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
        "toolTimeout": 60
      }
    }
  }
}
```

Leave `enabledTools` unset (or use `["*"]`) to retain MCP prompts and static
resources. Nanobot wraps the tools with names such as
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

`SAGASMITH_DND_MCP_HOME` moves all managed local state. By default it is
`<SagaSmith workspace>/.sagasmith-dnd-mcp`.

Set `SAGASMITH_DND_MCP_DENSE_ENABLED=1` after installing `[dense]` to enable
embedding-backed retrieval. The default remains SQLite FTS-only so the MCP can
start without loading an embedding model.

`SAGASMITH_DATABASE_URL` may point the Core runtime to an external database.
`CHROMA_DB_URL` may point vector retrieval to a remote Chroma service. When
neither is set, this server uses its own SQLite file and Chroma directory.
`CHROMA_DB_PATH` is also respected when a different local Chroma directory is
needed.

Use `SAGASMITH_DND_SKILLS_DIR` and `SAGASMITH_MODULEGEN_SKILLS_DIR` to point at
different checkouts of the skill packs.
