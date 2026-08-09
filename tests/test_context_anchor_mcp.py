import asyncio
import hashlib
from pathlib import Path

import pytest
from sagasmith_dnd.character_schema import default_character_sheet

from sagasmith_dnd_mcp.config import McpConfig
from sagasmith_dnd_mcp.server import create_server
from tests.authoring_helpers import finalize_and_activate_module


async def _call(server, name: str, arguments: dict):
    called = await server.call_tool(name, arguments)
    if isinstance(called, tuple):
        _, result = called
        return result.get("result", result) if isinstance(result, dict) else result
    return called


def _config(tmp_path: Path) -> McpConfig:
    dnd = tmp_path / "dnd"
    modulegen = tmp_path / "modulegen"
    (dnd / "full").mkdir(parents=True)
    modulegen.mkdir(parents=True)
    (dnd / "full" / "SKILL.md").write_text("# D&D Full\n", encoding="utf-8")
    (modulegen / "SKILL.md").write_text("# Module Generator\n", encoding="utf-8")
    return McpConfig(
        home=tmp_path / "home",
        database_url=None,
        chroma_url=None,
        chroma_path_override=None,
        dnd_skills_dir=dnd,
        modulegen_skills_dir=modulegen,
        auto_seed_rules=False,
    )


async def _import_ironslag_context(server, campaign_id: str) -> dict:
    content = (
        "# Forge of the Fire Giants\n\n"
        "## Foundry Upper Level\n\n"
        "Zaltember is a bully and coward. If wounded, he flees to area 31. "
        "If captured or cornered, he declares that he is the son of Duke Zalto. "
        "Before conceding, his parents try to convince the captors to release him "
        "as a show of good faith. If the characters refuse, the duke or duchess "
        "gives them the conch in exchange for his safe return.\n"
    )
    staged = await _call(
        server,
        "module_draft",
        {
            "campaign_id": campaign_id,
            "action": "start",
            "payload": {
                "name": "ironslag-context.md",
                "content": content,
                "source_key": "ironslag-context",
                "title": "Ironslag Context",
            },
            "idempotency_key": "context-stage",
        },
    )
    await finalize_and_activate_module(
        _call,
        server,
        campaign_id,
        staged,
        source_key="ironslag-context",
        title="Ironslag Context",
        portable_id="dnd5e.module.ironslag-context",
        edition="2024",
    )
    hits = await _call(
        server,
        "module_search",
        {
            "campaign_id": campaign_id,
            "query": "Zaltember wounded captured conch",
            "top_k": 3,
        },
    )
    expanded = await _call(
        server,
        "module_expand",
        {"chunk_id": hits[0]["id"]},
    )
    return {
        "module_id": expanded["module"]["id"],
        "scene_id": expanded["scene"]["id"],
        "chunk_id": expanded["chunk_id"],
        "page_start": expanded["page_start"],
        "page_end": expanded["page_end"],
        "heading_path": expanded["heading_path"],
        "content_sha256": hashlib.sha256(expanded["content"].encode("utf-8")).hexdigest(),
    }


def test_public_context_anchor_pins_exact_dm_evidence_without_a_narrative_dsl(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        server = create_server(_config(tmp_path))
        campaign = await _call(
            server,
            "campaign_create",
            {
                "name": "Agent DM context",
                "idempotency_key": "campaign",
            },
        )
        source_ref = await _import_ironslag_context(
            server,
            campaign["id"],
        )
        source_excerpt = "Zaltember is a bully and coward. If wounded, he flees to area 31."
        anchor = await _call(
            server,
            "memory_change",
            {
                "campaign_id": campaign["id"],
                "action": "upsert",
                "payload": {
                    "fact_key": "context:actor:zaltember:ironslag",
                    "kind": "context_anchor",
                    "subject": "Zaltember module context",
                    "subject_ref": "actor:zaltember",
                    "predicate": "",
                    "content": "Exact source context for Agent-as-DM adjudication.",
                    "metadata": {
                        "schema_version": 1,
                        "purpose": "Zaltember behavior and conch negotiation",
                        "related_refs": [
                            "scene:ironslag-area18",
                            "quest:obtain-fire-giant-conch",
                            "item:fire-giant-conch",
                        ],
                        "source_bindings": [
                            {
                                "source_ref": source_ref,
                                "source_excerpt": source_excerpt,
                            }
                        ],
                    },
                    "importance": 5,
                    "disclosure_scope": "dm",
                },
                "idempotency_key": "anchor",
            },
        )
        replay = await _call(
            server,
            "memory_change",
            {
                "campaign_id": campaign["id"],
                "action": "upsert",
                "payload": {
                    "fact_key": "context:actor:zaltember:ironslag",
                    "kind": "context_anchor",
                    "subject": "Zaltember module context",
                    "subject_ref": "actor:zaltember",
                    "predicate": "",
                    "content": "Exact source context for Agent-as-DM adjudication.",
                    "metadata": {
                        "schema_version": 1,
                        "purpose": "Zaltember behavior and conch negotiation",
                        "related_refs": [
                            "scene:ironslag-area18",
                            "quest:obtain-fire-giant-conch",
                            "item:fire-giant-conch",
                        ],
                        "source_bindings": [
                            {
                                "source_ref": source_ref,
                                "source_excerpt": source_excerpt,
                            }
                        ],
                    },
                    "importance": 5,
                    "disclosure_scope": "dm",
                },
                "idempotency_key": "anchor",
            },
        )
        context = await _call(
            server,
            "continuity_context",
            {
                "campaign_id": campaign["id"],
                "query": "words that do not occur in the module",
                "related_refs": ["actor:zaltember"],
                "budget_chars": 1_000,
            },
        )

        assert anchor["kind"] == "context_anchor"
        assert replay == anchor
        assert anchor["metadata"]["related_refs"][0] == "actor:zaltember"
        assert context["facts"] == []
        assert context["module_evidence"][0]["context_role"] == ("non_executable_module_evidence")
        assert context["module_evidence"][0]["source_ref"] == source_ref
        assert context["module_evidence"][0]["source_excerpt"] == source_excerpt
        assert context["retrieval"]["pinned_module_evidence_count"] == 1
        assert context["retrieval"]["strategy"] == ("lexical_structured_pinned_module_evidence_v3")
        assert context["context_receipt"]["campaign_id"] == campaign["id"]
        assert "principal_id" not in context["context_receipt"]
        assert len(context["context_receipt"]["principal_fingerprint"]) == 64
        assert context["context_receipt"]["module_source_ref_digests"]

        interpretation_bundle = await _call(
            server,
            "continuity_context",
            {
                "campaign_id": campaign["id"],
                "purpose": "source_interpretation",
                "query": "When does Zaltember flee?",
                "related_refs": ["actor:zaltember"],
            },
        )
        assert interpretation_bundle["purpose"] == "source_interpretation"
        assert interpretation_bundle["context"]["source_evidence"][0]["context_role"] == "evidence"
        source_basis = interpretation_bundle["context"]["source_evidence"][0]["basis_ref"]
        interpretation = {
            "schema_version": 1,
            "bundle_id": interpretation_bundle["bundle_id"],
            "purpose": "source_interpretation",
            "question": "When does Zaltember flee?",
            "interpretation": "Being wounded is narrative context for a DM decision.",
            "claims": [
                {
                    "statement": "The excerpt says he flees if wounded.",
                    "basis_refs": [source_basis],
                    "posture": "supported",
                }
            ],
            "ambiguities": ["The excerpt does not define a mechanical HP threshold."],
            "requires_dm_review": True,
        }
        validated_interpretation = await _call(
            server,
            "bounded_evaluation",
            {
                "campaign_id": campaign["id"],
                "action": "validate",
                "proposal": interpretation,
                "bundle_receipt": interpretation_bundle["bundle_receipt"],
            },
        )
        assert validated_interpretation["proposal"] == interpretation

        with pytest.raises(Exception, match="question does not match"):
            await _call(
                server,
                "bounded_evaluation",
                {
                    "campaign_id": campaign["id"],
                    "action": "validate",
                    "proposal": {
                        **interpretation,
                        "question": "What unrelated event happens next?",
                    },
                    "bundle_receipt": interpretation_bundle["bundle_receipt"],
                },
            )

        ruling_bundle = await _call(
            server,
            "continuity_context",
            {
                "campaign_id": campaign["id"],
                "purpose": "bounded_ruling",
                "query": "Does the current wound make Zaltember flee now?",
                "related_refs": ["actor:zaltember"],
            },
        )
        ruling_source_basis = ruling_bundle["context"]["source_evidence"][0]["basis_ref"]
        ruling = {
            "schema_version": 1,
            "bundle_id": ruling_bundle["bundle_id"],
            "purpose": "bounded_ruling",
            "ruling": "The DM must decide from current actor state; no threshold is invented.",
            "claims": [
                {
                    "statement": "The source supplies no numeric threshold.",
                    "basis_refs": [ruling_source_basis],
                    "posture": "inference",
                }
            ],
            "engine_requests": [],
            "unresolved": ["Whether the live injury is enough to trigger flight."],
            "decision_summary": "The result remains a proposal until the DM commits it.",
        }
        validated_ruling = await _call(
            server,
            "bounded_evaluation",
            {
                "campaign_id": campaign["id"],
                "action": "validate",
                "proposal": ruling,
                "bundle_receipt": ruling_bundle["bundle_receipt"],
            },
        )
        assert validated_ruling["proposal"] == ruling

        source_bound_event = {
            "summary": "Zaltember flees toward area 31.",
            "event_type": "npc_fled",
            "audience_scope": "party",
            "payload": {
                "source_ref": source_ref,
                "source_excerpt": source_excerpt,
            },
        }
        current_campaign = await _call(
            server,
            "campaign_query",
            {"view": "get", "payload": {"campaign_id": campaign["id"]}},
        )
        with pytest.raises(Exception, match="context_receipt is required"):
            await _call(
                server,
                "memory_change",
                {
                    "campaign_id": campaign["id"],
                    "action": "commit",
                    "payload": {"event": source_bound_event},
                    "expected_revision": current_campaign["revision"],
                    "idempotency_key": "source-bound-without-context",
                },
            )
        committed = await _call(
            server,
            "memory_change",
            {
                "campaign_id": campaign["id"],
                "action": "commit",
                "payload": {
                    "event": source_bound_event,
                    "context_receipt": context["context_receipt"],
                },
                "expected_revision": current_campaign["revision"],
                "idempotency_key": "source-bound-with-context",
            },
        )
        assert committed["event"]["event_type"] == "npc_fled"

        await _call(
            server,
            "access_grant",
            {
                "scope": "campaign",
                "campaign_id": campaign["id"],
                "principal_id": "player:one",
                "payload": {"role": "player"},
            },
        )
        player_context = await _call(
            server,
            "continuity_context",
            {
                "campaign_id": campaign["id"],
                "audience": "player",
                "related_refs": ["actor:zaltember"],
                "principal_id": "player:one",
            },
        )
        assert player_context["module_evidence"] == []

        with pytest.raises(Exception, match="unsupported fields"):
            await _call(
                server,
                "memory_change",
                {
                    "campaign_id": campaign["id"],
                    "action": "upsert",
                    "payload": {
                        "fact_key": "context:actor:zaltember:trigger",
                        "kind": "context_anchor",
                        "subject_ref": "actor:zaltember",
                        "content": "Must not become executable.",
                        "metadata": {
                            **anchor["metadata"],
                            "trigger": {"event": "actor_wounded"},
                        },
                        "disclosure_scope": "dm",
                    },
                    "idempotency_key": "invalid-trigger",
                },
            )

    asyncio.run(exercise())


def test_named_npc_state_changes_request_generic_agent_narrative_followup(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        server = create_server(_config(tmp_path))
        campaign = await _call(
            server,
            "campaign_create",
            {"name": "Narrative follow-up", "idempotency_key": "campaign"},
        )
        source_ref = await _import_ironslag_context(server, campaign["id"])
        sheet = default_character_sheet()
        sheet["combat"]["hp"] = {"value": 5, "max": 5, "temp": 0}
        npc = await _call(
            server,
            "character_create_from",
            {
                "mode": "direct",
                "payload": {
                    "campaign_id": campaign["id"],
                    "name": "Zaltember",
                    "character_type": "npc",
                    "sheet": sheet,
                },
                "principal_id": "system:local",
                "idempotency_key": "zaltember",
            },
        )
        await _call(
            server,
            "memory_change",
            {
                "campaign_id": campaign["id"],
                "action": "upsert",
                "payload": {
                    "fact_key": f"context:actor:{npc['id']}:ironslag",
                    "kind": "context_anchor",
                    "subject": "Zaltember module context",
                    "subject_ref": f"actor:{npc['id']}",
                    "content": "Exact source context for Agent-as-DM adjudication.",
                    "metadata": {
                        "schema_version": 1,
                        "purpose": "Zaltember behavior after consequential state changes",
                        "related_refs": [f"actor:{npc['id']}", "scene:ironslag-area18"],
                        "source_bindings": [
                            {
                                "source_ref": source_ref,
                                "source_excerpt": (
                                    "Zaltember is a bully and coward. If wounded, "
                                    "he flees to area 31."
                                ),
                            }
                        ],
                    },
                    "disclosure_scope": "dm",
                },
                "idempotency_key": "npc-anchor",
            },
        )
        arguments = {
            "character_id": npc["id"],
            "action": "damage",
            "payload": {
                "parts": [{"amount": 1, "damage_type": "bludgeoning"}],
            },
            "expected_revision": npc["revision"],
            "idempotency_key": "wound-zaltember",
        }
        damaged = await _call(server, "character_state_change", arguments)
        replay = await _call(server, "character_state_change", arguments)

        assert replay == damaged
        assert damaged["result"]["after_hp"] == 4
        assert damaged["narrative_followup"] == {
            "status": "agent_review_required",
            "default_resolver": "agent",
            "blocking": False,
            "actor_ids": [npc["id"]],
            "reasons": ["named_npc_hp_changed"],
            "related_refs": [f"actor:{npc['id']}"],
            "recommended_operation": "continuity_context:npc_turn",
        }

        inventory_changed = await _call(
            server,
            "inventory_change",
            {
                "owner": "character",
                "action": "add",
                "owner_id": npc["id"],
                "payload": {
                    "item": {
                        "id": "iron-token",
                        "name": "Iron token",
                        "kind": "equipment",
                        "quantity": 1,
                    }
                },
                "expected_revision": damaged["character"]["revision"],
                "idempotency_key": "give-zaltember-token",
            },
        )
        assert inventory_changed["narrative_followup"]["reasons"] == ["named_npc_inventory_changed"]

    asyncio.run(exercise())


def test_public_context_anchor_rejects_a_nonverbatim_source_excerpt(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        server = create_server(_config(tmp_path))
        campaign = await _call(
            server,
            "campaign_create",
            {
                "name": "Strict Agent context",
                "idempotency_key": "campaign",
            },
        )
        source_ref = await _import_ironslag_context(
            server,
            campaign["id"],
        )
        with pytest.raises(Exception, match="not present"):
            await _call(
                server,
                "memory_change",
                {
                    "campaign_id": campaign["id"],
                    "action": "upsert",
                    "payload": {
                        "fact_key": "context:bad-source",
                        "kind": "context_anchor",
                        "subject_ref": "actor:zaltember",
                        "content": "Paraphrase must not become authority.",
                        "metadata": {
                            "schema_version": 1,
                            "purpose": "Invalid paraphrased context",
                            "related_refs": [],
                            "source_bindings": [
                                {
                                    "source_ref": source_ref,
                                    "source_excerpt": ("Zaltember teleports directly to area 31."),
                                }
                            ],
                        },
                        "disclosure_scope": "dm",
                    },
                    "idempotency_key": "bad-anchor",
                },
            )

    asyncio.run(exercise())
