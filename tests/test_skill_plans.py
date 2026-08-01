from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from sagasmith_dnd_mcp.config import McpConfig
from sagasmith_dnd_mcp.facade_contracts import ACTION_PAYLOAD_CONTRACTS
from sagasmith_dnd_mcp.server import create_server
from sagasmith_dnd_mcp.skill_plans import (
    RESULT_OPERATION_PHASES,
    SKILL_PLAN_ASSET_ID,
    SkillPlanCatalog,
    SkillReadTracker,
)
from sagasmith_dnd_mcp.skills import SkillCatalog
from sagasmith_dnd_mcp.tool_profiles import (
    CORE_TOOLS,
    GROUP_BY_ID,
    profiles_for_tool,
)

EXPECTED_OPERATION_PHASES = {
    **{
        f"{tool_id}:{selector}": frozenset(profiles_for_tool(tool_id))
        for tool_id, selectors in ACTION_PAYLOAD_CONTRACTS.items()
        for selector in selectors
    },
    **RESULT_OPERATION_PHASES,
}


def _write_plan_skills(root: Path) -> dict:
    full = root / "full"
    references = full / "references" / "skill-groups"
    data = full / "data"
    references.mkdir(parents=True)
    data.mkdir(parents=True)
    (full / "SKILL.md").write_text(
        "# Test Full Skill\n\n## Startup\n\nUse the plan.\n",
        encoding="utf-8",
    )

    groups: dict[str, dict] = {}

    def add_group(
        group_id: str,
        *,
        policy: str,
        visibility: str,
        dependencies: list[str],
    ) -> None:
        relative = f"{group_id.replace('.', '-')}.md"
        content = f"# {group_id}\n\nRead {group_id} guidance.\n"
        (references / relative).write_text(content, encoding="utf-8")
        groups[group_id] = {
            "load_policy": policy,
            "visibility": visibility,
            "reason": f"Test guidance for {group_id}.",
            "depends_on": dependencies,
            "documents": [
                {
                    "kind": "asset",
                    "identifier": (
                        "dnd:full/references/skill-groups/" + relative
                    ),
                    "action": "read",
                    "max_chars": 1000,
                }
            ],
        }

    add_group(
        "core.runtime",
        policy="session",
        visibility="public",
        dependencies=[],
    )
    for phase in ("lobby", "play", "combat"):
        add_group(
            f"phase.{phase}",
            policy="phase",
            visibility="public",
            dependencies=["core.runtime"],
        )
    tool_group_bindings: dict[str, dict] = {}
    for tool_group_id, tool_group in GROUP_BY_ID.items():
        skill_group_id = f"tool.{tool_group_id}"
        visibility = (
            "local_admin"
            if tool_group.local_only
            else (
                "dm"
                if tool_group.roles
                else ("member" if tool_group.requires_campaign else "public")
            )
        )
        add_group(
            skill_group_id,
            policy="tool_group",
            visibility=visibility,
            dependencies=[f"phase.{tool_group.phase}"],
        )
        tool_group_bindings[tool_group_id] = {
            "required": [skill_group_id],
            "tools": sorted(tool_group.tools),
        }

    manifest = {
        "schema_version": 1,
        "budgets": {
            "core_chars": 2000,
            "phase_chars": 2000,
            "tool_group_chars": 2000,
            "automatic_chars": 4000,
        },
        "core_groups": ["core.runtime"],
        "phase_baselines": {
            "lobby": ["phase.lobby"],
            "play": ["phase.play"],
            "combat": ["phase.combat"],
        },
        "groups": groups,
        "tool_group_bindings": tool_group_bindings,
        "operation_bindings": {
            "campaign_query:resume": ["tool.play.scene"],
            "memory_change:commit": ["tool.lobby.memory_control"],
        },
    }
    (data / "skill-plan.v1.json").write_text(
        json.dumps(manifest, indent=2),
        encoding="utf-8",
    )
    return manifest


def _catalog(root: Path) -> SkillCatalog:
    modulegen = root.parent / "modulegen"
    modulegen.mkdir(exist_ok=True)
    return SkillCatalog(dnd_root=root, modulegen_root=modulegen)


def test_skill_plan_manifest_covers_every_tool_group_and_tracks_reads(
    tmp_path: Path,
) -> None:
    root = tmp_path / "dnd"
    _write_plan_skills(root)
    plans = SkillPlanCatalog(
        skills=_catalog(root),
        expected_tool_groups=GROUP_BY_ID,
        expected_operation_phases=EXPECTED_OPERATION_PHASES,
    )
    tracker = SkillReadTracker()

    assert plans.summary()["available"] is True
    assert plans.summary()["tool_group_count"] == len(GROUP_BY_ID) == 21
    plan = plans.plan(
        phase="lobby",
        role="public",
        loaded_tool_groups={"lobby.bootstrap"},
        session_key="session-a",
        tracker=tracker,
    )
    assert [
        item["skill_group"] for item in plan["required_now"]
    ] == [
        "core.runtime",
        "phase.lobby",
        "tool.lobby.bootstrap",
    ]

    first = plan["required_now"][0]["documents"][0]
    document = plans.resolve_document(
        kind=first["kind"],
        identifier=first["identifier"],
        action=first["action"],
        heading=None,
    )
    assert document is not None
    tracker.mark(session_key="session-a", document=document)
    repeated = plans.plan(
        phase="lobby",
        role="public",
        loaded_tool_groups={"lobby.bootstrap"},
        session_key="session-a",
        tracker=tracker,
    )
    assert repeated["already_satisfied"] == ["core.runtime"]
    assert "core.runtime" not in {
        item["skill_group"] for item in repeated["required_now"]
    }


def test_skill_plan_checksum_change_invalidates_a_prior_read(tmp_path: Path) -> None:
    root = tmp_path / "dnd"
    _write_plan_skills(root)
    plans = SkillPlanCatalog(
        skills=_catalog(root),
        expected_tool_groups=GROUP_BY_ID,
        expected_operation_phases=EXPECTED_OPERATION_PHASES,
    )
    tracker = SkillReadTracker()
    initial = plans.plan(
        phase="lobby",
        role="public",
        loaded_tool_groups=set(),
        session_key="session-a",
        tracker=tracker,
    )
    first = initial["required_now"][0]["documents"][0]
    document = plans.resolve_document(
        kind=first["kind"],
        identifier=first["identifier"],
        action=first["action"],
        heading=None,
    )
    assert document is not None
    tracker.mark(session_key="session-a", document=document)

    target = (
        root
        / "full"
        / "references"
        / "skill-groups"
        / "core-runtime.md"
    )
    target.write_text(
        "# core.runtime\n\nChanged runtime guidance.\n",
        encoding="utf-8",
    )
    plans.reload()
    changed = plans.plan(
        phase="lobby",
        role="public",
        loaded_tool_groups=set(),
        session_key="session-a",
        tracker=tracker,
    )
    assert changed["invalidated"][0]["skill_group"] == "core.runtime"
    assert "core.runtime" not in changed["already_satisfied"]


def test_skill_plan_rejects_tool_group_contract_drift(tmp_path: Path) -> None:
    root = tmp_path / "dnd"
    manifest = _write_plan_skills(root)
    manifest["tool_group_bindings"]["lobby.bootstrap"]["tools"] = [
        "campaign_create"
    ]
    (
        root / "full" / "data" / "skill-plan.v1.json"
    ).write_text(json.dumps(manifest), encoding="utf-8")

    plans = SkillPlanCatalog(
        skills=_catalog(root),
        expected_tool_groups=GROUP_BY_ID,
        expected_operation_phases=EXPECTED_OPERATION_PHASES,
    )

    assert plans.available is False
    assert "lobby.bootstrap.tools mismatch" in str(plans.load_error)
    assert "system_list" in str(plans.load_error)


def test_skill_plan_rejects_guidance_below_tool_group_access_boundary(
    tmp_path: Path,
) -> None:
    root = tmp_path / "dnd"
    manifest = _write_plan_skills(root)
    manifest["groups"]["tool.lobby.rules"]["visibility"] = "public"
    (root / "full" / "data" / "skill-plan.v1.json").write_text(
        json.dumps(manifest),
        encoding="utf-8",
    )

    plans = SkillPlanCatalog(
        skills=_catalog(root),
        expected_tool_groups=GROUP_BY_ID,
        expected_operation_phases=EXPECTED_OPERATION_PHASES,
    )

    assert plans.available is False
    assert "lobby.rules.required" in str(plans.load_error)
    assert "'dm' guidance" in str(plans.load_error)


def test_skill_plan_rejects_unknown_operation_selector(tmp_path: Path) -> None:
    root = tmp_path / "dnd"
    manifest = _write_plan_skills(root)
    manifest["operation_bindings"]["character_create_from:preset"] = [
        "tool.lobby.characters"
    ]
    (root / "full" / "data" / "skill-plan.v1.json").write_text(
        json.dumps(manifest),
        encoding="utf-8",
    )

    plans = SkillPlanCatalog(
        skills=_catalog(root),
        expected_tool_groups=GROUP_BY_ID,
        expected_operation_phases=EXPECTED_OPERATION_PHASES,
    )

    assert plans.available is False
    assert "character_create_from:preset" in str(plans.load_error)
    assert "public selector" in str(plans.load_error)


def test_skill_plan_rejects_cross_phase_operation_guidance(
    tmp_path: Path,
) -> None:
    root = tmp_path / "dnd"
    manifest = _write_plan_skills(root)
    manifest["operation_bindings"]["chase:start"] = ["phase.combat"]
    (root / "full" / "data" / "skill-plan.v1.json").write_text(
        json.dumps(manifest),
        encoding="utf-8",
    )

    plans = SkillPlanCatalog(
        skills=_catalog(root),
        expected_tool_groups=GROUP_BY_ID,
        expected_operation_phases=EXPECTED_OPERATION_PHASES,
    )

    assert plans.available is False
    assert "chase:start" in str(plans.load_error)
    assert "incompatible phase guidance" in str(plans.load_error)


def test_skill_plan_rejects_cross_phase_tool_group_guidance(
    tmp_path: Path,
) -> None:
    root = tmp_path / "dnd"
    manifest = _write_plan_skills(root)
    manifest["tool_group_bindings"]["lobby.bootstrap"]["required"] = [
        "phase.combat"
    ]
    (root / "full" / "data" / "skill-plan.v1.json").write_text(
        json.dumps(manifest),
        encoding="utf-8",
    )

    plans = SkillPlanCatalog(
        skills=_catalog(root),
        expected_tool_groups=GROUP_BY_ID,
        expected_operation_phases=EXPECTED_OPERATION_PHASES,
    )

    assert plans.available is False
    assert "lobby.bootstrap.required" in str(plans.load_error)
    assert "incompatible phase guidance" in str(plans.load_error)


def test_skill_read_tracker_is_bounded_and_rejects_unknown_focus(
    tmp_path: Path,
) -> None:
    root = tmp_path / "dnd"
    _write_plan_skills(root)
    plans = SkillPlanCatalog(
        skills=_catalog(root),
        expected_tool_groups=GROUP_BY_ID,
        expected_operation_phases=EXPECTED_OPERATION_PHASES,
    )
    tracker = SkillReadTracker(max_sessions=1, max_documents_per_session=1)
    initial = plans.plan(
        phase="lobby",
        role="public",
        loaded_tool_groups=set(),
        session_key="session-a",
        tracker=tracker,
    )
    documents = [
        plans.resolve_document(
            kind=item["kind"],
            identifier=item["identifier"],
            action=item["action"],
            heading=item.get("heading"),
        )
        for group in initial["required_now"]
        for item in group["documents"]
    ]
    resolved = [item for item in documents if item is not None]
    assert len(resolved) >= 2
    tracker.mark(session_key="session-a", document=resolved[0])
    tracker.mark(session_key="session-a", document=resolved[1])
    assert tracker.status(session_key="session-a", document=resolved[0]) == "unread"
    tracker.mark(session_key="session-b", document=resolved[0])
    assert tracker.status(session_key="session-a", document=resolved[1]) == "unread"

    with pytest.raises(ValueError, match="unknown focused tool groups"):
        plans.plan(
            phase="lobby",
            role="public",
            loaded_tool_groups=set(),
            focus_tool_groups={"missing.group"},
            session_key="session-b",
            tracker=tracker,
        )


def test_truncated_planned_section_does_not_satisfy_read_receipt(
    tmp_path: Path,
) -> None:
    dnd = tmp_path / "dnd"
    manifest = _write_plan_skills(dnd)
    target = (
        dnd
        / "full"
        / "references"
        / "skill-groups"
        / "core-runtime.md"
    )
    target.write_text(
        "# core.runtime\n\n" + ("bounded guidance " * 40) + "\n",
        encoding="utf-8",
    )
    manifest["groups"]["core.runtime"]["documents"][0].update(
        {"action": "section", "heading": "core.runtime", "max_chars": 1000}
    )
    (dnd / "full" / "data" / "skill-plan.v1.json").write_text(
        json.dumps(manifest),
        encoding="utf-8",
    )
    config = McpConfig(
        home=tmp_path / "home",
        database_url=None,
        chroma_url=None,
        chroma_path_override=None,
        dnd_skills_dir=dnd,
        modulegen_skills_dir=tmp_path / "modulegen",
        auto_seed_rules=False,
    )

    async def exercise() -> None:
        server = create_server(config)
        _, truncated = await server.call_tool(
            "skill_query",
            {
                "kind": "asset",
                "action": "section",
                "identifier": (
                    "dnd:full/references/skill-groups/core-runtime.md"
                ),
                "heading": "core.runtime",
                "max_chars": 256,
            },
        )
        assert truncated["result"]["truncated"] is True
        assert "skill_read_receipt" not in truncated
        _, plan = await server.call_tool(
            "skill_query",
            {"kind": "skill", "action": "plan"},
        )
        assert "core.runtime" in {
            item["skill_group"] for item in plan["result"]["required_now"]
        }

        _, complete = await server.call_tool(
            "skill_query",
            {
                "kind": "asset",
                "action": "section",
                "identifier": (
                    "dnd:full/references/skill-groups/core-runtime.md"
                ),
                "heading": "core.runtime",
                "max_chars": 1000,
            },
        )
        assert complete["result"]["truncated"] is False
        assert complete["skill_read_receipt"]["action"] == "section"

    asyncio.run(exercise())


def test_full_runtime_exposure_load_fails_closed_without_a_valid_plan(
    tmp_path: Path,
) -> None:
    dnd = tmp_path / "dnd"
    full = dnd / "full"
    full.mkdir(parents=True)
    (full / "SKILL.md").write_text("# Full runtime\n", encoding="utf-8")
    config = McpConfig(
        home=tmp_path / "home",
        database_url=None,
        chroma_url=None,
        chroma_path_override=None,
        dnd_skills_dir=dnd,
        modulegen_skills_dir=tmp_path / "modulegen",
        auto_seed_rules=False,
    )

    async def exercise() -> None:
        server = create_server(config)
        _, opened = await server.call_tool("exposure_open", {})
        assert opened["skill_plan"]["available"] is False
        assert opened["skill_plan"]["required"] is True
        with pytest.raises(Exception, match="Skills plan is unavailable"):
            await server.call_tool(
                "exposure_load",
                {
                    "exposure_id": opened["exposure_id"],
                    "group_id": "lobby.bootstrap",
                },
            )

    asyncio.run(exercise())


def test_server_skill_plan_follows_exposure_phase_and_read_checksums(
    tmp_path: Path,
) -> None:
    dnd = tmp_path / "dnd"
    _write_plan_skills(dnd)
    config = McpConfig(
        home=tmp_path / "home",
        database_url=None,
        chroma_url=None,
        chroma_path_override=None,
        dnd_skills_dir=dnd,
        modulegen_skills_dir=tmp_path / "modulegen",
        auto_seed_rules=False,
    )

    async def exercise() -> None:
        server = create_server(config)
        _, planned = await server.call_tool(
            "skill_query",
            {"kind": "skill", "action": "plan"},
        )
        initial = planned["result"]
        assert initial["available"] is True
        assert initial["phase"] == "lobby"
        first = initial["required_now"][0]["documents"][0]

        _, read = await server.call_tool(
            "skill_query",
            {
                "kind": first["kind"],
                "action": first["action"],
                "identifier": first["identifier"],
            },
        )
        assert read["skill_read_receipt"]["checksum"] == first["checksum"]
        _, replanned = await server.call_tool(
            "skill_query",
            {"kind": "skill", "action": "plan"},
        )
        assert "core.runtime" in replanned["result"]["already_satisfied"]

        _, opened = await server.call_tool("exposure_open", {})
        assert opened["skill_plan"]["phase"] == "lobby"
        _, loaded = await server.call_tool(
            "exposure_load",
            {
                "exposure_id": opened["exposure_id"],
                "group_id": "lobby.bootstrap",
            },
        )
        assert "tool.lobby.bootstrap" in {
            item["skill_group"]
            for item in loaded["skill_plan_delta"]["required_now"]
        }

        _, campaign = await server.call_tool(
            "campaign_create",
            {
                "name": "Skill-plan campaign",
                "idempotency_key": "skill-plan-campaign",
            },
        )
        _, phase = await server.call_tool(
            "game_phase",
            {
                "campaign_id": campaign["id"],
                "action": "set",
                "tool_profile": "play",
                "expected_revision": campaign["revision"],
                "idempotency_key": "skill-plan-play",
            },
        )
        assert phase["result"]["tool_profile"] == "play"
        assert phase["skill_plan"]["phase"] == "play"
        _, play_opened = await server.call_tool(
            "exposure_open",
            {"campaign_id": campaign["id"]},
        )
        assert play_opened["skill_plan"]["phase"] == "play"
        _, play_loaded = await server.call_tool(
            "exposure_load",
            {
                "exposure_id": play_opened["exposure_id"],
                "group_id": "play.resolution",
            },
        )
        assert "tool.play.resolution" in {
            item["skill_group"]
            for item in play_loaded["skill_plan_delta"]["required_now"]
        }
        _, resumed = await server.call_tool(
            "campaign_query",
            {
                "view": "resume",
                "payload": {"campaign_id": campaign["id"]},
            },
        )
        assert resumed["result"]["skill_plan"]["operation"] == (
            "campaign_query:resume"
        )
        assert resumed["result"]["host_context_binding"] == resumed["result"][
            "continuity"
        ]["host_context_binding"]
        assert "tool.play.scene" in {
            item["skill_group"]
            for item in resumed["result"]["skill_plan"]["required_now"]
        }
        _, unloaded = await server.call_tool(
            "exposure_unload",
            {
                "exposure_id": play_opened["exposure_id"],
                "group_id": "play.resolution",
            },
        )
        assert unloaded["skill_plan_delta"]["removed_tool_groups"] == [
            "play.resolution"
        ]

    asyncio.run(exercise())


def test_real_skill_plan_manifest_is_valid_and_within_budgets() -> None:
    skill_root = Path(__file__).resolve().parents[2] / "SagaSmith-dnd-skills"
    if not skill_root.is_dir():
        return
    skills = SkillCatalog(
        dnd_root=skill_root,
        modulegen_root=skill_root.parent / "SagaSmith-module-gen-skills",
    )
    plans = SkillPlanCatalog(
        skills=skills,
        expected_tool_groups=GROUP_BY_ID,
        expected_operation_phases=EXPECTED_OPERATION_PHASES,
    )

    assert SKILL_PLAN_ASSET_ID.endswith("skill-plan.v1.json")
    assert plans.available is True, plans.load_error
    assert plans.summary()["group_count"] == 35
    assert plans.summary()["tool_group_count"] == 21
    assert plans.summary()["operation_binding_count"] == 22
    assert len(skills.read("dnd.full.skills.dnd-dm")) < 12_000
    assert len(
        skills.read_asset(
            "dnd:full/skills/dnd-dm/references/RUNTIME_DEEP_REFERENCE.md"
        )
    ) > 50_000
    player_plan = plans.plan(
        phase="lobby",
        role="player",
        loaded_tool_groups={"lobby.modules"},
        session_key="player-session",
        tracker=SkillReadTracker(),
    )
    assert player_plan["unavailable_skill_groups"] == ["modules.import"]
    assert not any(
        item["operation"].startswith("module_review:")
        for item in player_plan["conditional"]
    )
    combat_plan = plans.plan(
        phase="combat",
        role="dm",
        loaded_tool_groups={"combat.actions"},
        operation="combat_choice:on_hit_ruling",
        session_key="dm-combat-session",
        tracker=SkillReadTracker(),
    )
    assert combat_plan["phase"] == "combat"
    assert {"phase.combat", "combat.actions"} <= {
        item["skill_group"] for item in combat_plan["required_now"]
    }
    npc_plan = plans.plan(
        phase="play",
        role="dm",
        loaded_tool_groups={"play.scene", "play.scene_control"},
        operation="continuity_context:npc_turn",
        session_key="dm-npc-session",
        tracker=SkillReadTracker(),
    )
    assert "npc.portrayal" in {
        item["skill_group"] for item in npc_plan["required_now"]
    }
    actor_plan = plans.plan(
        phase="combat",
        role="dm",
        loaded_tool_groups={"combat.observe"},
        operation="continuity_context:actor_turn",
        session_key="dm-actor-session",
        tracker=SkillReadTracker(),
    )
    assert {"core.context_isolation", "evaluation.actor"} <= {
        item["skill_group"] for item in actor_plan["required_now"]
    }
    audience_plan = plans.plan(
        phase="play",
        role="player",
        loaded_tool_groups={"play.scene"},
        operation="continuity_context:audience_render",
        session_key="player-audience-session",
        tracker=SkillReadTracker(),
    )
    assert "evaluation.audience" in {
        item["skill_group"] for item in audience_plan["required_now"]
    }


def test_stdio_cold_start_uses_real_phase_skill_plan(tmp_path: Path) -> None:
    skill_root = Path(__file__).resolve().parents[2] / "SagaSmith-dnd-skills"
    if not skill_root.is_dir():
        pytest.skip("adjacent Full D&D Skills repository is unavailable")

    async def exercise() -> None:
        env = dict(os.environ)
        env.update(
            {
                "SAGASMITH_DND_MCP_HOME": str(tmp_path / "home"),
                "SAGASMITH_DND_MCP_AUTO_SEED": "0",
                "SAGASMITH_DND_SKILLS_DIR": str(skill_root),
                "SAGASMITH_DND_MCP_BOUND_PRINCIPAL_ID": "codex:cold-start",
            }
        )
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "sagasmith_dnd_mcp.server"],
            cwd=Path(__file__).parents[1],
            env=env,
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                initialized = await session.initialize()
                assert initialized.capabilities.tools.listChanged is True
                tools = {item.name for item in (await session.list_tools()).tools}
                assert tools == set(CORE_TOOLS)

                capabilities = await session.call_tool(
                    "server_capabilities",
                    {},
                )
                capability_payload = json.loads(capabilities.content[0].text)
                summary = capability_payload["zero_knowledge_bootstrap"][
                    "phase_skill_plan"
                ]
                assert summary["available"] is True
                assert summary["group_count"] == 35
                assert summary["tool_group_count"] == 21
                assert summary["operation_binding_count"] == 22

                planned = await session.call_tool(
                    "skill_query",
                    {"kind": "skill", "action": "plan"},
                )
                plan = json.loads(planned.content[0].text)["result"]
                assert plan["phase"] == "lobby"
                assert plan["role"] == "public"
                first = plan["required_now"][0]["documents"][0]
                read_result = await session.call_tool(
                    "skill_query",
                    {
                        "kind": first["kind"],
                        "action": first["action"],
                        "identifier": first["identifier"],
                    },
                )
                read_payload = json.loads(read_result.content[0].text)
                assert read_payload["skill_read_receipt"]["checksum"] == (
                    first["checksum"]
                )

    asyncio.run(exercise())
