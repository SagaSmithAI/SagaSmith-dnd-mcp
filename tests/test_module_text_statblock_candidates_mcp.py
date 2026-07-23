import asyncio
from pathlib import Path

from sagasmith_dnd_mcp.config import McpConfig
from sagasmith_dnd_mcp.server import create_server

GOBLIN_MODULE = (
    "# Appendix B: Monsters\n\n"
    "## MONSTER DESCRIPTIONS\n\n"
    "##### GOBLIN\n\n"
    "Small humanoid (goblinoid), neutral evil Armor Class 15 "
    "(leather armor, shield) Hit Points 7 (2d6) Speed 30 ft.\n\n"
    "##### STR\n\n8 (-1)\n\n"
    "##### DEX\n\n14 (+2)\n\n"
    "##### CON\n\n10 (+0)\n\n"
    "##### INT\n\n10 (+0)\n\n"
    "##### WIS\n\n8 (-1)\n\n"
    "##### CHA\n\n"
    "8 (-1) Skills Stealth +6 Senses darkvision 60 ft., passive Perception 9 "
    "Languages Common, Goblin Challenge 1/4 (50 XP) Nimble Escape. "
    "The goblin can take the Disengage or Hide action as a bonus action.\n\n"
    "##### ACTIONS\n\n"
    "Scimitar. Melee Weapon Attack: +4 to hit, reach 5 ft., one target. "
    "Hit: 5 (ld6 + 2) slashing damage. Shortbow. Ranged Weapon Attack: "
    "+4 to hit, range 80 ft./320 ft., one target. Hit: 5 (1d6 + 2) "
    "piercing damage.\n"
)


async def _call(server, name: str, arguments: dict):
    called = await server.call_tool(name, arguments)
    if isinstance(called, tuple):
        _, result = called
        return result.get("result", result) if isinstance(result, dict) else result
    return called


def test_text_module_statblock_candidate_can_create_a_source_bound_actor(
    tmp_path: Path,
) -> None:
    config = McpConfig(
        home=tmp_path / "home",
        database_url=None,
        chroma_url=None,
        chroma_path_override=None,
        dnd_skills_dir=tmp_path / "dnd",
        modulegen_skills_dir=tmp_path / "modulegen",
        auto_seed_rules=True,
    )

    async def exercise() -> None:
        server = create_server(config)
        campaign = await _call(
            server,
            "campaign_create",
            {"name": "Text statblock", "edition": "2014", "idempotency_key": "campaign"},
        )
        staged = await _call(
            server,
            "module_import",
            {
                "campaign_id": campaign["id"],
                "action": "stage",
                "payload": {
                    "name": "goblins.md",
                    "content": GOBLIN_MODULE,
                    "source_key": "goblins",
                    "title": "Goblins",
                },
                "idempotency_key": "stage",
            },
        )
        job_id = staged["job"]["id"]
        for action in ("inspect", "validate", "ingest"):
            ingested = await _call(
                server,
                "module_import",
                {
                    "campaign_id": campaign["id"],
                    "action": action,
                    "payload": {"job_id": job_id},
                    "idempotency_key": action,
                },
            )
        campaign = await _call(
            server,
            "campaign_query",
            {"view": "get", "payload": {"campaign_id": campaign["id"]}},
        )
        await _call(
            server,
            "module_import",
            {
                "campaign_id": campaign["id"],
                "action": "activate",
                "payload": {"job_id": job_id},
                "expected_revision": campaign["revision"],
                "idempotency_key": "activate",
            },
        )
        candidates = await _call(
            server,
            "module_query",
            {
                "campaign_id": campaign["id"],
                "view": "candidates",
                "payload": {"module_id": ingested["module_id"]},
            },
        )

        assert len(candidates) == 1
        candidate = candidates[0]
        source_chunks = [
            await _call(server, "module_expand", {"chunk_id": chunk_id})
            for chunk_id in candidate["source_chunk_ids"]
        ]
        assert candidate["execution_state"] == "review_ready", candidate.get(
            "review_error"
        )
        assert [item["chunk_id"] for item in source_chunks] == candidate[
            "source_chunk_ids"
        ]
        assert candidate["validation"]["name"] == "GOBLIN"
        reviewed = await _call(
            server,
            "module_content_review",
            {
                "campaign_id": campaign["id"],
                "module_id": ingested["module_id"],
                "scene_id": candidate["scene_id"],
                "content_key": "goblin",
                "normalized_content": candidate["normalized_content"],
                "source_chunk_ids": candidate["source_chunk_ids"],
                "observation": "Reviewed normalized text against all source chunks.",
                "idempotency_key": "review-goblin",
            },
        )
        assert reviewed["review"]["evidence"]["confidence"] == "reviewed_text"
        created = await _call(
            server,
            "character_create_from",
            {
                "mode": "module_statblock",
                "payload": {
                    "campaign_id": campaign["id"],
                    "review_id": reviewed["review"]["id"],
                    "name": "Cragmaw Goblin",
                    "character_type": "monster",
                },
                "idempotency_key": "create-goblin",
            },
        )
        assert created["character"]["name"] == "Cragmaw Goblin"
        assert created["character"]["derived"]["armor_class"] == 15
        assert created["character"]["derived"]["hit_points"]["max"] == 7
        attacks = {
            item["item_id"]: item
            for item in created["character"]["derived"]["inventory"]["weapon_attacks"]
        }
        assert set(attacks) == {"scimitar", "shortbow"}
        assert attacks["shortbow"]["range_ft"] == {"normal": 80, "long": 320}
        assert {
            item["source_key"] for item in created["character"]["sheet"]["inventory"]["items"]
        } == {f"module-review:{reviewed['review']['id']}"}

    asyncio.run(exercise())
