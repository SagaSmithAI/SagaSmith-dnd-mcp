# Full Agent corpus regression

`scripts.regression_agent_corpus` is the single entry point for the long-running
module playthrough audit. It first regenerates the dynamic corpus inventory and
coverage matrix, then runs separate authenticated DM and player nanobot sessions
against a fresh MCP home for every runnable campaign line. It never calls an
internal service or supplies a story result.

```powershell
python -m scripts.regression_agent_corpus `
  --output-dir ..\.runs\full-agent-corpus `
  --agent-config-template C:\secure\nanobot-regression.json `
  --run-id nightly-2026-08-11 `
  --fail-fast
```

The template must define the real `sagasmith_dnd` MCP server and a usable model
provider. The runner rewrites only the per-campaign workspace, fresh MCP home,
module roots, Skills path, principal injection, and native-tool exposure. Keep
provider credentials outside the checked-in template.

Use `--campaign <dynamic-campaign-line-id>` for a focused rerun and `--resume`
to retain already completed campaign reports. `--inventory-only` is a quick
contract check; it is not gameplay evidence.

The default test suite validates discovery, orchestration, transcript parsing,
and fail-closed coverage accounting without calling a model provider. Nightly or
full CI runs the real command through the `full_agent` marker:

```powershell
$env:SAGASMITH_RUN_FULL_AGENT_CORPUS = "1"
$env:SAGASMITH_AGENT_CONFIG_TEMPLATE = "C:\secure\nanobot-regression.json"
pytest -m full_agent tests/test_regression_agent_corpus_driver.py
```

Each campaign directory retains the raw DM/player JSONL transcripts, process
stdout/stderr, complete native tool timeline, phase/exposure/context-binding
timeline, observed `tools/list_changed` refresh count, random receipts, matrix
gaps, and the legal-ending audit. A model's prose never closes a gap: the report
requires successful native calls and authoritative ending state.
