import asyncio
from pathlib import Path

from sagasmith_dnd_mcp.config import McpConfig
from sagasmith_dnd_mcp.server import create_server

COMMONER = """### Commoner

*Medium humanoid (any race), any alignment*

**Armor Class** 10

**Hit Points** 4 (1d8)

**Speed** 30 ft.

| STR | DEX | CON | INT | WIS | CHA |
|:---:|:---:|:---:|:---:|:---:|:---:|
| 10 (+0) | 10 (+0) | 10 (+0) | 10 (+0) | 10 (+0) | 10 (+0) |

**Senses** passive Perception 10

**Languages** any one language (usually Common)

**Challenge** 0 (10 XP)

###### Actions

***Club***. *Melee Weapon Attack:* +2 to hit, reach 5 ft., one target.
*Hit:* 2 (1d4) bludgeoning damage.
"""


REACTIVE_COMMONER = COMMONER + """

###### Reactions

***Parry***. The commoner adds 2 to its AC against one melee attack that would hit it.
"""


async def _call(server, name: str, arguments: dict):
    _, result = await server.call_tool(name, arguments)
    value = result.get("result", result) if isinstance(result, dict) else result
    if isinstance(value, dict) and "action" in value and "result" in value:
        return value["result"]
    return value


def test_imported_rule_source_creates_a_source_bound_combat_actor(tmp_path: Path) -> None:
    import_root = tmp_path / "rules"
    import_root.mkdir()
    commoner = import_root / "commoner.md"
    commoner.write_text(COMMONER, encoding="utf-8")
    config = McpConfig(
        home=tmp_path / "home",
        database_url=None,
        chroma_url=None,
        chroma_path_override=None,
        dnd_skills_dir=tmp_path / "dnd",
        modulegen_skills_dir=tmp_path / "modulegen",
        rule_import_roots=(import_root,),
        auto_seed_rules=False,
    )

    async def exercise() -> None:
        server = create_server(config)
        campaign = await _call(
            server,
            "campaign_create",
            {
                "name": "Statblock actors",
                "edition": "2014",
                "idempotency_key": "campaign",
            },
        )
        staged = await _call(
            server,
            "rule_import",
            {
                "campaign_id": campaign["id"],
                "action": "stage",
                "payload": {
                    "source_path": str(commoner),
                    "source_key": "srd/commoner",
                    "title": "Commoner",
                    "edition": "2014",
                    "publication_id": "srd2014",
                },
                "idempotency_key": "stage-commoner",
            },
        )
        job_id = staged["job"]["id"]
        await _call(
            server,
            "rule_import",
            {
                "campaign_id": campaign["id"],
                "action": "inspect",
                "payload": {"job_id": job_id},
                "idempotency_key": "inspect-commoner",
            },
        )
        ingested = await _call(
            server,
            "rule_import",
            {
                "campaign_id": campaign["id"],
                "action": "ingest",
                "payload": {"job_id": job_id},
                "idempotency_key": "ingest-commoner",
            },
        )
        arguments = {
            "mode": "statblock",
            "payload": {
                "campaign_id": campaign["id"],
                "source_id": ingested["source_id"],
                "name": "Falten",
                "character_type": "npc",
                "summary": "A tavern patron grounded in the imported module scene.",
            },
            "idempotency_key": "actor-falten",
        }
        created = await _call(server, "character_create_from", arguments)
        replay = await _call(server, "character_create_from", arguments)

        assert replay == created
        assert created["source"]["id"] == ingested["source_id"]
        assert created["source"]["chunk_ids"]
        assert created["statblock"] == {
            "challenge_rating": "0",
            "experience_points": 10,
            "warnings": [],
            "settlement": "automatic",
        }
        actor = created["character"]
        assert actor["name"] == "Falten"
        assert actor["summary"].startswith("A tavern patron")
        club = actor["derived"]["inventory"]["weapon_attacks"][0]
        assert club["item_id"] == "club"
        assert club["attack_bonus"] == 2
        assert club["damage_expression"] == "1d4"
        assert "rule-source:srd/commoner" in actor["notes"]["profile"]["dm_notes"]

    asyncio.run(exercise())


def test_statblock_reconstruction_preserves_reaction_heading_paths(tmp_path: Path) -> None:
    import_root = tmp_path / "rules"
    import_root.mkdir()
    reactive = import_root / "reactive-commoner.md"
    reactive.write_text(REACTIVE_COMMONER, encoding="utf-8")
    config = McpConfig(
        home=tmp_path / "home",
        database_url=None,
        chroma_url=None,
        chroma_path_override=None,
        dnd_skills_dir=tmp_path / "dnd",
        modulegen_skills_dir=tmp_path / "modulegen",
        rule_import_roots=(import_root,),
        auto_seed_rules=False,
    )

    async def exercise() -> None:
        server = create_server(config)
        campaign = await _call(
            server,
            "campaign_create",
            {"name": "Reaction", "edition": "2014", "idempotency_key": "campaign"},
        )
        staged = await _call(
            server,
            "rule_import",
            {
                "campaign_id": campaign["id"],
                "action": "stage",
                "payload": {
                    "source_path": str(reactive),
                    "source_key": "test/reactive-commoner",
                    "title": "Reactive Commoner",
                    "edition": "2014",
                },
                "idempotency_key": "stage",
            },
        )
        job_id = staged["job"]["id"]
        await _call(
            server,
            "rule_import",
            {
                "campaign_id": campaign["id"],
                "action": "inspect",
                "payload": {"job_id": job_id},
                "idempotency_key": "inspect",
            },
        )
        ingested = await _call(
            server,
            "rule_import",
            {
                "campaign_id": campaign["id"],
                "action": "ingest",
                "payload": {"job_id": job_id},
                "idempotency_key": "ingest",
            },
        )
        created = await _call(
            server,
            "character_create_from",
            {
                "mode": "statblock",
                "payload": {
                    "campaign_id": campaign["id"],
                    "source_id": ingested["source_id"],
                    "name": "Reactive Commoner",
                },
                "idempotency_key": "actor",
            },
        )

        parry = next(
            item
            for item in created["character"]["sheet"]["content"]["activities"]
            if item["name"] == "Parry"
        )
        assert parry["activation"]["type"] == "reaction"
        assert created["statblock"]["settlement"] == "mixed"
        assert created["statblock"]["warnings"] == [
            "Parry: descriptive reaction is not automatically settled"
        ]

    asyncio.run(exercise())
