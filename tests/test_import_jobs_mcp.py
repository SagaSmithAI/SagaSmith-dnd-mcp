import asyncio
from pathlib import Path

import pytest

from sagasmith_dnd_mcp.config import McpConfig
from sagasmith_dnd_mcp.server import create_server


def test_rule_and_module_import_jobs_are_reviewable_and_activation_safe(tmp_path: Path) -> None:
    import_root = tmp_path / "imports"
    import_root.mkdir()
    rulebook = import_root / "supplement.md"
    rulebook.write_text(
        "# Optional Spells\n\n## Spark\n\n1st-level evocation spell\nCasting Time: 1 action\n",
        encoding="utf-8",
    )
    config = McpConfig(
        home=tmp_path / "home",
        database_url=None,
        chroma_url=None,
        chroma_path_override=None,
        dnd_skills_dir=tmp_path / "dnd",
        modulegen_skills_dir=tmp_path / "modulegen",
        rule_import_roots=(import_root,),
    )

    async def call(server, name: str, arguments: dict):
        _, result = await server.call_tool(name, arguments)
        return result.get("result", result) if isinstance(result, dict) else result

    async def exercise() -> None:
        server = create_server(config)
        campaign = await call(
            server,
            "campaign_create",
            {"name": "Import lifecycle", "idempotency_key": "campaign"},
        )
        staged = await call(
            server,
            "rule_document_stage",
            {"campaign_id": campaign["id"], "source_path": str(rulebook)},
        )
        rule_job = await call(
            server,
            "rule_import_job_create",
            {
                "campaign_id": campaign["id"],
                "artifact": staged["artifact"],
                "source_key": "xgte-pilot",
                "title": "Xanathar Pilot",
                "edition": "2014",
                "publication_id": "xgte",
                "idempotency_key": "rule-job-create",
            },
        )
        rule_job_id = rule_job["job"]["id"]
        inspected = await call(
            server,
            "rule_import_job_inspect",
            {
                "campaign_id": campaign["id"],
                "job_id": rule_job_id,
                "idempotency_key": "rule-job-inspect",
            },
        )
        assert inspected["job"]["state"] == "inspected"
        indexed = await call(
            server,
            "rule_import_job_ingest",
            {
                "campaign_id": campaign["id"],
                "job_id": rule_job_id,
                "idempotency_key": "rule-job-ingest",
            },
        )
        assert indexed["source"]["edition"] == "2014"
        extracted = await call(
            server,
            "rule_content_candidates_extract",
            {
                "campaign_id": campaign["id"],
                "job_id": rule_job_id,
                "idempotency_key": "rule-job-extract",
            },
        )
        spark = next(item for item in extracted["candidates"] if item["name"] == "Spark")
        reviewed = await call(
            server,
            "import_job_review_candidates",
            {
                "campaign_id": campaign["id"],
                "job_id": rule_job_id,
                "decisions": [
                    {
                        "id": spark["id"],
                        "review_status": "accepted",
                        "artifact": {
                            "kind": "spell",
                            "application_state": "selection_ready",
                            "card": {
                                "name": "Spark",
                                "level": 1,
                                "classes": ["wizard"],
                                "definition": {},
                            },
                        },
                    }
                ],
                "idempotency_key": "rule-job-review",
            },
        )
        assert reviewed["job"]["state"] == "reviewed"
        compiled = await call(
            server,
            "rule_import_job_compile",
            {
                "campaign_id": campaign["id"],
                "job_id": rule_job_id,
                "manifest": {
                    "id": "dnd5e.xgte.import-job",
                    "version": "1.0.0",
                    "title": "Xanathar import job",
                    "namespace": "dnd5e.xgte.import-job",
                    "system_id": "dnd5e",
                    "editions": ["2014"],
                },
                "idempotency_key": "rule-job-compile",
            },
        )
        assert compiled["draft"]["status"] == "validated"
        installed = await call(
            server,
            "rule_import_job_install",
            {
                "campaign_id": campaign["id"],
                "job_id": rule_job_id,
                "idempotency_key": "rule-job-install",
            },
        )
        assert installed["job"]["state"] == "installed"
        profile = await call(
            server,
            "campaign_rule_profile_set",
            {
                "campaign_id": campaign["id"],
                "edition": "2014",
                "expected_revision": campaign["revision"],
                "idempotency_key": "profile",
            },
        )
        activated = await call(
            server,
            "rule_import_job_activate",
            {
                "campaign_id": campaign["id"],
                "job_id": rule_job_id,
                "expected_revision": profile["campaign_revision"],
                "idempotency_key": "rule-job-activate",
            },
        )
        assert activated["job"]["state"] == "activated"
        catalog = await call(
            server,
            "content_catalog_list",
            {"campaign_id": campaign["id"], "query": "Spark"},
        )
        assert catalog[0]["application_state"] == "selection_ready"
        assert catalog[0]["source_citations"][0]["source_key"] == "xgte-pilot"

        artifact = await call(
            server,
            "module_write",
            {
                "name": "import-job-module",
                "content": "# Chapter One\n\n## Arrival\n\n#### A1. Courtyard\n30 by 20 feet\n",
            },
        )
        module_job = await call(
            server,
            "module_import_job_create",
            {
                "campaign_id": campaign["id"],
                "artifact": artifact["artifact"],
                "source_key": "import-job-module",
                "idempotency_key": "module-job-create",
            },
        )
        module_job_id = module_job["job"]["id"]
        await call(
            server,
            "module_import_job_inspect",
            {
                "campaign_id": campaign["id"],
                "job_id": module_job_id,
                "idempotency_key": "module-job-inspect",
            },
        )
        validation = await call(
            server,
            "module_import_job_validate",
            {
                "campaign_id": campaign["id"],
                "job_id": module_job_id,
                "idempotency_key": "module-job-validate",
            },
        )
        assert validation["validation"]["valid"] is True
        assert validation["validation"]["diff"]["current_module_id"] is None
        imported_module = await call(
            server,
            "module_import_job_import",
            {
                "campaign_id": campaign["id"],
                "job_id": module_job_id,
                "idempotency_key": "module-job-import",
            },
        )
        assert await call(server, "module_index", {"campaign_id": campaign["id"]}) == []
        current = await call(server, "campaign_get", {"campaign_id": campaign["id"]})
        module_activated = await call(
            server,
            "module_import_job_activate",
            {
                "campaign_id": campaign["id"],
                "job_id": module_job_id,
                "expected_revision": current["revision"],
                "idempotency_key": "module-job-activate",
            },
        )
        assert module_activated["activation"]["module_id"] == imported_module["module_id"]
        index = await call(server, "module_index", {"campaign_id": campaign["id"]})
        assert "Arrival" in {item["title"] for item in index}

        await call(
            server,
            "module_write",
            {
                "name": "import-job-module",
                "content": (
                    "# Chapter One\n\n## Arrival\nRevised entrance.\n\n"
                    "## Finale\n\n#### B1. Observatory\n25 by 25 feet\n"
                ),
            },
        )
        revision_job = await call(
            server,
            "module_import_job_create",
            {
                "campaign_id": campaign["id"],
                "artifact": artifact["artifact"],
                "source_key": "import-job-module",
                "idempotency_key": "module-revision-create",
            },
        )
        await call(
            server,
            "module_import_job_inspect",
            {
                "campaign_id": campaign["id"],
                "job_id": revision_job["job"]["id"],
                "idempotency_key": "module-revision-inspect",
            },
        )
        revision_validation = await call(
            server,
            "module_import_job_validate",
            {
                "campaign_id": campaign["id"],
                "job_id": revision_job["job"]["id"],
                "idempotency_key": "module-revision-validate",
            },
        )
        assert (
            revision_validation["validation"]["diff"]["current_module_id"]
            == imported_module["module_id"]
        )
        assert revision_validation["validation"]["diff"]["added"]

    asyncio.run(exercise())


def test_module_import_facade_stages_only_allowlisted_documents(tmp_path: Path) -> None:
    import_root = tmp_path / "modules"
    import_root.mkdir()
    source = import_root / "adventure.md"
    source.write_text(
        "# Chapter One\n\n## Arrival\n\n#### A1. Courtyard\n30 by 20 feet\n",
        encoding="utf-8",
    )
    outside = tmp_path / "outside.md"
    outside.write_text("# Outside\n", encoding="utf-8")
    config = McpConfig(
        home=tmp_path / "home",
        database_url=None,
        chroma_url=None,
        chroma_path_override=None,
        dnd_skills_dir=tmp_path / "dnd",
        modulegen_skills_dir=tmp_path / "modulegen",
        module_import_roots=(import_root,),
    )

    async def call(server, name: str, arguments: dict):
        _, result = await server.call_tool(name, arguments)
        return result.get("result", result) if isinstance(result, dict) else result

    async def exercise() -> None:
        server = create_server(config)
        campaign = await call(
            server,
            "campaign_create",
            {"name": "Managed module", "idempotency_key": "campaign"},
        )
        with pytest.raises(Exception, match="outside configured import roots"):
            await call(
                server,
                "module_import",
                {
                    "campaign_id": campaign["id"],
                    "action": "stage",
                    "payload": {"source_path": str(outside)},
                    "idempotency_key": "outside",
                },
            )
        staged = await call(
            server,
            "module_import",
            {
                "campaign_id": campaign["id"],
                "action": "stage",
                "payload": {
                    "source_path": str(source),
                    "source_key": "managed-adventure",
                    "title": "Managed Adventure",
                },
                "idempotency_key": "stage",
            },
        )
        assert staged["staged"] is True
        assert staged["artifact"].endswith("-adventure.md")
        job_id = staged["job"]["id"]

        inspected = await call(
            server,
            "module_import",
            {
                "campaign_id": campaign["id"],
                "action": "inspect",
                "payload": {"job_id": job_id},
                "idempotency_key": "inspect",
            },
        )
        assert inspected["preview"]["valid"] is True
        validated = await call(
            server,
            "module_import",
            {
                "campaign_id": campaign["id"],
                "action": "validate",
                "payload": {"job_id": job_id},
                "idempotency_key": "validate",
            },
        )
        assert validated["validation"]["valid"] is True
        ingested = await call(
            server,
            "module_import",
            {
                "campaign_id": campaign["id"],
                "action": "ingest",
                "payload": {"job_id": job_id},
                "idempotency_key": "ingest",
            },
        )
        current = await call(server, "campaign_get", {"campaign_id": campaign["id"]})
        activated = await call(
            server,
            "module_import",
            {
                "campaign_id": campaign["id"],
                "action": "activate",
                "payload": {"job_id": job_id},
                "expected_revision": current["revision"],
                "idempotency_key": "activate",
            },
        )
        assert activated["activation"]["module_id"] == ingested["module_id"]

    asyncio.run(exercise())
