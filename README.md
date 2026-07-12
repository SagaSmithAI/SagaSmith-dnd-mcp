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

The server uses stdio. Configure a client such as Nanobot with:

```json
{
  "command": "sagasmith-dnd-mcp",
  "args": []
}
```

## MCP Surface

Tools include storage status/migration, campaign creation/listing, safe module
write/import/list/search, rule search, and skill listing/reading. Skill packs
are also resources at `sagasmith://skill/{skill_id}`, while `dnd_dm` and
`module_generator` are MCP prompts.

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
