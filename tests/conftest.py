"""Temporary test migration bridge from retired MCP names to the compact contract.

The bridge is loaded only by pytest.  It proves that historical semantic tests
continue to exercise the same services while production `list_tools` exposes no
legacy names.  New contract tests call the facades directly.
"""

from __future__ import annotations

import gc
import os
import shutil
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from mcp.server.fastmcp import FastMCP

from sagasmith_dnd_mcp.config import McpConfig
from sagasmith_dnd_mcp.server import create_server
from sagasmith_dnd_mcp.storage import SagaSmithStorage

_ORIGINAL_CALL_TOOL = FastMCP.call_tool
_CURRENT_SERVER: FastMCP | None = None


def _backup_sqlite(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(source) as source_connection:
        with sqlite3.connect(target) as target_connection:
            source_connection.backup(target_connection)


class _TestDatabaseTemplates:
    """Lazily build current-schema database templates for isolated test copies."""

    def __init__(self, root: Path, *, enabled: bool) -> None:
        self.root = root
        self.enabled = enabled
        self.building = False
        self.workspace = Path(__file__).resolve().parents[2]
        self._templates: dict[bool, Path] = {}

    def _config(self, home: Path, *, auto_seed_rules: bool) -> McpConfig:
        return McpConfig(
            home=home,
            database_url=None,
            chroma_url=None,
            chroma_path_override=None,
            dnd_skills_dir=self.workspace / "SagaSmith-dnd-skills",
            modulegen_skills_dir=self.workspace / "SagaSmith-module-gen-skills",
            auto_seed_rules=auto_seed_rules,
        )

    def get(self, auto_seed_rules: bool) -> Path:
        existing = self._templates.get(auto_seed_rules)
        if existing is not None:
            return existing
        if not self.enabled:
            raise RuntimeError("test database templates are disabled")

        config = self._config(
            self.root / f"bootstrap-{int(auto_seed_rules)}",
            auto_seed_rules=auto_seed_rules,
        )
        if auto_seed_rules:
            config.database_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(self.get(False), config.database_path)

        server: FastMCP | None = None
        self.building = True
        try:
            server = create_server(config)
            template = self.root / f"template-{int(auto_seed_rules)}.db"
            _backup_sqlite(config.database_path, template)
            self._templates[auto_seed_rules] = template
            return template
        finally:
            if server is not None:
                del server
            self.building = False
            gc.collect()


@pytest.fixture(scope="session")
def _test_database_templates(
    tmp_path_factory: pytest.TempPathFactory,
) -> _TestDatabaseTemplates:
    return _TestDatabaseTemplates(
        tmp_path_factory.mktemp("database-templates"),
        enabled=os.environ.get("SAGASMITH_TEST_DB_TEMPLATES", "1") != "0",
    )


def _facade(name: str, arguments: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    args = dict(arguments)
    principal = args.pop("principal_id", "system:local")
    expected = args.pop("expected_revision", None)
    branch = args.pop("branch_id", None)
    key = args.pop("idempotency_key", None)

    def packed(tool: str, **values: Any) -> tuple[str, dict[str, Any]]:
        return tool, {name: value for name, value in values.items() if value is not None}

    if name == "character_create":
        return packed(
            "character_create_from",
            mode="direct",
            payload=args,
            principal_id=principal,
            idempotency_key=key,
        )
    if name in {"character_list", "character_library_list", "character_get"}:
        view = {
            "character_list": "list",
            "character_library_list": "library",
            "character_get": "get",
        }[name]
        return packed("character_query", view=view, payload=args, principal_id=principal)
    if name == "character_update":
        character_id = args.pop("character_id")
        return packed(
            "character_metadata_update",
            character_id=character_id,
            payload=args,
            principal_id=principal,
            expected_revision=expected,
            idempotency_key=key,
        )
    if name in {"character_wallet_adjust", "party_wallet_adjust"}:
        owner = "character" if name.startswith("character") else "party"
        owner_id = args.pop("character_id", args.pop("campaign_id", None))
        return packed(
            "wallet_change",
            owner=owner,
            action="adjust",
            owner_id=owner_id,
            denomination=args.pop("denomination"),
            amount=args.pop("amount"),
            payload=args,
            principal_id=principal,
            expected_revision=expected,
            idempotency_key=key,
        )
    if name in {
        "character_inventory_add",
        "character_inventory_update",
        "character_inventory_remove",
        "character_inventory_equip",
        "character_ammunition_consume",
        "party_inventory_add",
        "party_inventory_remove",
    }:
        owner = "character" if name.startswith("character") else "party"
        owner_id = args.pop("character_id", args.pop("campaign_id", None))
        action = name.removeprefix("character_").removeprefix("party_").removeprefix("inventory_")
        action = {
            "add": "add",
            "update": "update",
            "remove": "remove",
            "equip": "equip",
            "ammunition_consume": "consume_ammunition",
        }[action]
        return packed(
            "inventory_change",
            owner=owner,
            action=action,
            owner_id=owner_id,
            payload=args,
            principal_id=principal,
            expected_revision=expected,
            idempotency_key=key,
        )
    if name == "character_inventory_transfer":
        return packed(
            "inventory_transfer",
            mode="character_to_character",
            payload=args,
            principal_id=principal,
            idempotency_key=key,
        )
    if name == "party_inventory_transfer":
        direction = args.pop("direction")
        return packed(
            "inventory_transfer",
            mode="party_to_character" if direction == "to_character" else "character_to_party",
            payload=args,
            principal_id=principal,
            idempotency_key=key,
        )
    if name == "party_wallet_transfer":
        direction = args.pop("direction")
        campaign_id = args.pop("campaign_id")
        return packed(
            "wallet_change",
            owner="party",
            action="transfer_to_character"
            if direction in {"to_character", "withdraw"}
            else "transfer_from_character",
            owner_id=campaign_id,
            denomination=args.pop("denomination"),
            amount=args.pop("amount"),
            payload=args,
            principal_id=principal,
            idempotency_key=key,
        )
    if name in {
        "character_effect_add",
        "character_effect_remove",
        "character_resource_set",
        "character_rest",
        "character_memory_add",
        "character_memory_resolve",
    }:
        character_id = args.pop("character_id")
        action = {
            "character_effect_add": "effect_add",
            "character_effect_remove": "effect_remove",
            "character_resource_set": "resource_set",
            "character_rest": "rest",
            "character_memory_add": "memory_add",
            "character_memory_resolve": "memory_resolve",
        }[name]
        return packed(
            "character_state_change",
            character_id=character_id,
            action=action,
            payload=args,
            principal_id=principal,
            expected_revision=expected,
            idempotency_key=key,
        )
    if name in {"character_cast_spell", "character_use_activity"}:
        character_id = args.pop("character_id")
        return packed(
            "character_action",
            character_id=character_id,
            action="cast_spell" if name.endswith("cast_spell") else "use_activity",
            payload=args,
            principal_id=principal,
            expected_revision=expected,
            idempotency_key=key,
        )
    if name in {"character_spell_prepare", "character_spell_prepare_list"}:
        character_id = args.pop("character_id")
        return packed(
            "character_spell_prepare",
            character_id=character_id,
            mode="set" if name.endswith("prepare") else "replace_all",
            payload=args,
            principal_id=principal,
            expected_revision=expected,
            idempotency_key=key,
        )
    if name in {"campaign_list", "campaign_get", "party_show"}:
        view = {"campaign_list": "list", "campaign_get": "get", "party_show": "party"}[name]
        return packed("campaign_query", view=view, payload=args, principal_id=principal)
    if name == "campaign_update":
        return packed(
            "campaign_change",
            campaign_id=args.pop("campaign_id"),
            payload=args,
            principal_id=principal,
            expected_revision=expected,
            idempotency_key=key,
        )
    if name in {"campaign_member_grant", "actor_grant"}:
        campaign_id = args.pop("campaign_id")
        target = principal
        return packed(
            "access_grant",
            scope="campaign" if name == "campaign_member_grant" else "actor",
            campaign_id=campaign_id,
            principal_id=target,
            payload=args,
            by_principal_id=args.pop("by_principal_id", None),
        )
    if name in {"event_add", "event_list"}:
        return packed(
            "campaign_event",
            campaign_id=args.pop("campaign_id"),
            action="add" if name == "event_add" else "list",
            payload=args,
            principal_id=principal,
            idempotency_key=key,
        )
    if name in {"memory_add", "memory_list", "memory_search"}:
        campaign_id = args.pop("campaign_id")
        if name == "memory_add":
            return packed(
                "memory_change",
                campaign_id=campaign_id,
                principal_id=principal,
                idempotency_key=key,
                **args,
            )
        return packed(
            "memory_query",
            campaign_id=campaign_id,
            view="search" if name == "memory_search" else "list",
            payload=args,
            principal_id=principal,
        )
    if name in {"actor_knowledge_add", "actor_knowledge_revise"}:
        return packed(
            "actor_knowledge_change",
            action="add" if name.endswith("add") else "revise",
            payload=args,
            principal_id=principal,
            idempotency_key=key,
        )
    if name in {"actor_knowledge_list", "actor_knowledge_search"}:
        return packed(
            "actor_knowledge_query",
            campaign_id=args.pop("campaign_id"),
            actor_id=args.pop("actor_id"),
            view="search" if name.endswith("search") else "list",
            payload=args,
            principal_id=principal,
        )
    if name in {
        "campaign_rule_profile_get",
        "campaign_rule_profile_set",
        "campaign_rule_pack_set",
        "campaign_rule_pack_remove",
        "campaign_rules_explain",
        "campaign_rule_receipts",
    }:
        campaign_id = args.pop("campaign_id")
        action = {
            "campaign_rule_profile_get": "get_profile",
            "campaign_rule_profile_set": "set_profile",
            "campaign_rule_pack_set": "set_pack",
            "campaign_rule_pack_remove": "remove_pack",
            "campaign_rules_explain": "explain",
            "campaign_rule_receipts": "receipts",
        }[name]
        return packed(
            "campaign_rules",
            campaign_id=campaign_id,
            action=action,
            payload=args,
            principal_id=principal,
            expected_revision=expected,
            branch_id=branch,
            idempotency_key=key,
        )
    if name in {"rule_pack_draft", "rule_pack_draft_from_source"}:
        return packed(
            "rule_pack_compile",
            action="from_source" if name.endswith("from_source") else "draft",
            payload=args,
        )
    if name in {"rule_pack_install", "rule_pack_remove"}:
        return packed(
            "rule_pack_change",
            action="install" if name.endswith("install") else "remove",
            pack_id=args.pop("pack_id"),
            version=args.pop("version"),
        )
    if name in {"rule_pack_list", "rule_pack_inspect", "rule_pack_test", "content_catalog_list"}:
        view = {
            "rule_pack_list": "list",
            "rule_pack_inspect": "inspect",
            "rule_pack_test": "test",
            "content_catalog_list": "content_catalog",
        }[name]
        return packed("rule_pack_query", view=view, payload=args, principal_id=principal)
    if name == "rule_import_job_create":
        artifact = args.pop("artifact")
        args["source_path"] = artifact
        return packed(
            "rule_import",
            campaign_id=args.pop("campaign_id"),
            action="stage",
            payload=args,
            principal_id=principal,
            idempotency_key=key,
        )
    if name in {
        "rule_import_job_inspect",
        "rule_import_job_ingest",
        "rule_content_candidates_extract",
        "import_job_review_candidates",
        "rule_import_job_compile",
        "rule_import_job_install",
        "rule_import_job_activate",
    }:
        campaign_id = args.pop("campaign_id")
        action = {
            "rule_import_job_inspect": "inspect",
            "rule_import_job_ingest": "ingest",
            "rule_content_candidates_extract": "extract_candidates",
            "import_job_review_candidates": "review",
            "rule_import_job_compile": "compile",
            "rule_import_job_install": "install",
            "rule_import_job_activate": "activate",
        }[name]
        return packed(
            "rule_import",
            campaign_id=campaign_id,
            action=action,
            payload=args,
            principal_id=principal,
            expected_revision=expected,
            branch_id=branch,
            idempotency_key=key,
        )
    if name == "module_import_job_create":
        campaign_id = args.pop("campaign_id")
        artifact = args.pop("artifact")
        cached = getattr(_CURRENT_SERVER, "_test_module_artifacts", {}).get(artifact)
        if cached is None:
            raise ValueError("unknown staged test module artifact")
        cached = {**cached, **args}
        return packed(
            "module_import",
            campaign_id=campaign_id,
            action="stage",
            payload=cached,
            principal_id=principal,
            idempotency_key=key,
        )
    if name in {
        "module_import_job_inspect",
        "module_import_job_validate",
        "module_import_job_import",
        "module_import_job_activate",
    }:
        campaign_id = args.pop("campaign_id")
        action = {
            "module_import_job_inspect": "inspect",
            "module_import_job_validate": "validate",
            "module_import_job_import": "ingest",
            "module_import_job_activate": "activate",
        }[name]
        return packed(
            "module_import",
            campaign_id=campaign_id,
            action=action,
            payload=args,
            principal_id=principal,
            expected_revision=expected,
            idempotency_key=key,
        )
    if name in {"branch_list", "branch_compare"}:
        return packed(
            "branch_query",
            campaign_id=args.pop("campaign_id"),
            view="compare" if name == "branch_compare" else "list",
            payload=args,
            principal_id=principal,
        )
    if name in {"branch_create", "branch_checkout"}:
        if name == "branch_checkout":
            args["branch_id"] = branch
            branch = None
        return packed(
            "branch_change",
            campaign_id=args.pop("campaign_id"),
            action="create" if name == "branch_create" else "checkout",
            payload=args,
            principal_id=principal,
            expected_revision=expected,
            expected_branch_id=args.pop("expected_branch_id", None),
            idempotency_key=key,
        )
    if name in {
        "snapshot_list",
        "snapshot_verify",
        "snapshot_lineage",
        "snapshot_regenerate_recap",
    }:
        view = {
            "snapshot_list": "list",
            "snapshot_verify": "verify",
            "snapshot_lineage": "lineage",
            "snapshot_regenerate_recap": "recap",
        }[name]
        return packed(
            "snapshot_query",
            campaign_id=args.pop("campaign_id"),
            view=view,
            payload=args,
            principal_id=principal,
        )
    if name in {"state_history", "state_undo", "state_redo"}:
        return packed(
            "state_revision",
            campaign_id=args.pop("campaign_id"),
            action={"state_history": "history", "state_undo": "undo", "state_redo": "redo"}[name],
            payload=args,
            principal_id=principal,
            idempotency_key=key,
        )
    if name in {"game_phase_get", "game_phase_set"}:
        return packed(
            "game_phase",
            campaign_id=args.pop("campaign_id"),
            action="get" if name.endswith("get") else "set",
            tool_profile=args.pop("tool_profile", None),
            principal_id=principal,
            expected_revision=expected,
            branch_id=branch,
            idempotency_key=key,
        )
    if name in {"combat_status", "combat_available_actions", "combat_reactions"}:
        return packed(
            "combat_query",
            campaign_id=args.pop("campaign_id"),
            view={
                "combat_status": "status",
                "combat_available_actions": "available_actions",
                "combat_reactions": "reactions",
            }[name],
            actor_id=args.pop("actor_id", None),
            principal_id=principal,
        )
    if name in {"combat_move", "combat_stand"}:
        return packed(
            "combat_movement",
            campaign_id=args.pop("campaign_id"),
            actor_id=args.pop("actor_id"),
            action="move" if name == "combat_move" else "stand",
            payload=args,
            principal_id=principal,
            expected_revision=expected,
            branch_id=branch,
            idempotency_key=key,
        )
    if name in {"combat_apply_damage", "combat_heal"}:
        return packed(
            "combat_hp_change",
            campaign_id=args.pop("campaign_id"),
            target_id=args.pop("target_id"),
            action="damage" if name.endswith("damage") else "heal",
            payload=args,
            principal_id=principal,
            expected_revision=expected,
            branch_id=branch,
            idempotency_key=key,
        )
    if name in {"combat_choice_open", "combat_choice_resolve"}:
        return packed(
            "combat_choice",
            campaign_id=args.pop("campaign_id"),
            actor_id=args.pop("actor_id"),
            action="open" if name.endswith("open") else "resolve",
            payload=args,
            principal_id=principal,
            expected_revision=expected,
            branch_id=branch,
            idempotency_key=key,
        )
    if name in {
        "combat_ready_spell",
        "combat_readied_spell_trigger",
        "combat_readied_spell_resolve",
        "combat_readied_action_trigger",
        "combat_readied_action_resolve",
    }:
        action = {
            "combat_ready_spell": "ready_spell",
            "combat_readied_spell_trigger": "trigger_spell",
            "combat_readied_spell_resolve": "resolve_spell",
            "combat_readied_action_trigger": "trigger_action",
            "combat_readied_action_resolve": "resolve_action",
        }[name]
        return packed(
            "combat_ready",
            campaign_id=args.pop("campaign_id"),
            action=action,
            payload=args,
            principal_id=principal,
            expected_revision=expected,
            branch_id=branch,
            idempotency_key=key,
        )
    if name in {"skill_list", "skill_read", "skill_asset_list", "skill_asset_read"}:
        kind = "skill" if name.startswith("skill_") else "asset"
        action = "read" if name.endswith("read") else "list"
        identifier = args.pop("skill_id", args.pop("asset_id", None))
        return packed(
            "skill_query",
            kind=kind,
            action=action,
            identifier=identifier,
            source=args.pop("source", None),
        )
    if name in {"module_list", "module_index", "module_read_scene", "module_current"}:
        campaign_id = args.pop("campaign_id")
        view = {
            "module_list": "list",
            "module_index": "index",
            "module_read_scene": "scene",
            "module_current": "current",
        }[name]
        return packed(
            "module_query", campaign_id=campaign_id, view=view, payload=args, principal_id=principal
        )
    return None


@pytest.fixture(autouse=True)
def _translate_retired_test_calls(
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    _test_database_templates: _TestDatabaseTemplates,
) -> None:
    original_migrate = SagaSmithStorage.migrate
    templated_paths: set[Path] = set()
    fresh_database = request.node.get_closest_marker("fresh_database") is not None

    def migrate(storage: SagaSmithStorage) -> None:
        if _test_database_templates.building:
            original_migrate(storage)
            return
        if (
            _test_database_templates.enabled
            and not fresh_database
            and storage.config.database_url is None
        ):
            target = storage.config.database_path.resolve()
            if target in templated_paths and target.exists():
                return
            if not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(
                    _test_database_templates.get(storage.config.auto_seed_rules),
                    target,
                )
                templated_paths.add(target)
                return
        original_migrate(storage)

    async def call_tool(
        self: FastMCP, name: str, arguments: dict[str, Any], *args: Any, **kwargs: Any
    ):
        # Historic tests used a write/inspect/import trilogy.  Recreate that
        # sequence through the staged public lifecycle, never by registering
        # those old tool names on the production server.
        if name == "module_write":
            cache = getattr(self, "_test_module_artifacts", {})
            cache[arguments["name"]] = {"name": arguments["name"], "content": arguments["content"]}
            setattr(self, "_test_module_artifacts", cache)
            return None, {"artifact": arguments["name"]}
        if name == "module_inspect":
            cache = getattr(self, "_test_module_artifacts", {})
            if arguments["artifact"] not in cache:
                raise ValueError("unknown staged test module artifact")
            return None, {"parser_profile": "dnd5e"}
        if name == "module_import" and "action" not in arguments:
            cache = getattr(self, "_test_module_artifacts", {})
            artifact = cache[arguments["artifact"]]
            campaign_id = arguments["campaign_id"]
            principal = arguments.get("principal_id", "system:local")
            key = arguments.get("idempotency_key")
            completed = getattr(self, "_test_module_imports", {})
            cache_key = (campaign_id, arguments["artifact"], key, principal)
            if cache_key in completed:
                return None, completed[cache_key]
            _, staged = await _ORIGINAL_CALL_TOOL(
                self,
                "module_import",
                {
                    "campaign_id": campaign_id,
                    "action": "stage",
                    "payload": artifact,
                    "principal_id": principal,
                    "idempotency_key": f"{key}:stage",
                },
            )
            job_id = staged["result"]["job"]["id"]
            for action in ("inspect", "validate", "ingest"):
                await _ORIGINAL_CALL_TOOL(
                    self,
                    "module_import",
                    {
                        "campaign_id": campaign_id,
                        "action": action,
                        "payload": {"job_id": job_id},
                        "principal_id": principal,
                        "idempotency_key": f"{key}:{action}",
                    },
                )
            _, current = await _ORIGINAL_CALL_TOOL(
                self,
                "campaign_query",
                {"view": "get", "payload": {"campaign_id": campaign_id}, "principal_id": principal},
            )
            _, activated = await _ORIGINAL_CALL_TOOL(
                self,
                "module_import",
                {
                    "campaign_id": campaign_id,
                    "action": "activate",
                    "payload": {"job_id": job_id},
                    "principal_id": principal,
                    "expected_revision": current["result"]["revision"],
                    "idempotency_key": f"{key}:activate",
                },
            )
            completed[cache_key] = activated["result"]
            setattr(self, "_test_module_imports", completed)
            return None, completed[cache_key]
        if name == "rule_document_stage":
            path = Path(arguments["source_path"])
            # Call the public stage gate to retain import-root validation; the
            # later legacy import command supplies its final metadata.
            _, staged = await _ORIGINAL_CALL_TOOL(
                self,
                "rule_import",
                {
                    "campaign_id": arguments["campaign_id"],
                    "action": "stage",
                    "payload": {
                        "source_path": str(path),
                        "source_key": "test-stage",
                        "title": "Test stage",
                        "edition": "2014",
                    },
                    "principal_id": arguments.get("principal_id", "system:local"),
                    "idempotency_key": f"test-stage:{path.name}",
                },
            )
            return None, {
                "artifact": str(path),
                "checksum": staged["result"]["job"]["artifact_checksum"],
            }
        if name == "rule_document_inspect":
            source = Path(arguments["artifact"])
            headings = sum(
                1
                for line in source.read_text(encoding="utf-8").splitlines()
                if line.startswith("#")
            )
            return None, {"sections": headings}
        if name == "rule_document_import":
            source_path = arguments["artifact"]
            stage_payload = {
                key: arguments[key]
                for key in (
                    "source_key",
                    "title",
                    "edition",
                    "locale",
                    "publication_id",
                    "version",
                    "authority",
                )
                if key in arguments
            }
            stage_payload["source_path"] = source_path
            campaign_id = arguments["campaign_id"]
            principal = arguments.get("principal_id", "system:local")
            key = arguments.get("idempotency_key")
            completed = getattr(self, "_test_rule_imports", {})
            cache_key = (campaign_id, source_path, key, principal)
            if cache_key in completed:
                return None, completed[cache_key]
            _, staged = await _ORIGINAL_CALL_TOOL(
                self,
                "rule_import",
                {
                    "campaign_id": campaign_id,
                    "action": "stage",
                    "payload": stage_payload,
                    "principal_id": principal,
                    "idempotency_key": f"{key}:stage",
                },
            )
            job_id = staged["result"]["job"]["id"]
            await _ORIGINAL_CALL_TOOL(
                self,
                "rule_import",
                {
                    "campaign_id": campaign_id,
                    "action": "inspect",
                    "payload": {"job_id": job_id},
                    "principal_id": principal,
                    "idempotency_key": f"{key}:inspect",
                },
            )
            _, indexed = await _ORIGINAL_CALL_TOOL(
                self,
                "rule_import",
                {
                    "campaign_id": campaign_id,
                    "action": "ingest",
                    "payload": {"job_id": job_id},
                    "principal_id": principal,
                    "idempotency_key": f"{key}:ingest",
                },
            )
            completed[cache_key] = indexed["result"]
            setattr(self, "_test_rule_imports", completed)
            return None, completed[cache_key]
        global _CURRENT_SERVER
        _CURRENT_SERVER = self
        translated = _facade(name, arguments)
        if translated is None:
            return await _ORIGINAL_CALL_TOOL(self, name, arguments, *args, **kwargs)
        content, result = await _ORIGINAL_CALL_TOOL(self, *translated, *args, **kwargs)
        if isinstance(result, dict) and "action" in result and "result" in result:
            result = result["result"]
        return content, result

    monkeypatch.setattr(SagaSmithStorage, "migrate", migrate)
    monkeypatch.setattr(FastMCP, "call_tool", call_tool)
