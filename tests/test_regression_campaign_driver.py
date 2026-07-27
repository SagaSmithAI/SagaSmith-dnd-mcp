from __future__ import annotations

import argparse
import asyncio
import hashlib
import io
import json
import sys
from contextlib import AbstractAsyncContextManager
from pathlib import Path

import pytest

import scripts.regression_campaign as campaign_driver
from scripts.regression_campaign import (
    _arguments,
    _character_summary,
    _configure_utf8_streams,
    _discover_rule_chunks,
    _discover_rule_sources,
    _expanded_source_ref,
    _load_json_object,
    _load_review_override,
    _prepare_rule_statblock,
    _prepare_rule_statblock_with_recovery,
    _prepare_statblock,
    _restore_statblock_preparation_context,
    _review_override_page,
    _rule_statblock_operation_token,
    _statblock_creation_key,
    _statblock_replacement_fields,
    _validate_noncombat_scene,
)


def test_rule_statblock_idempotency_is_bound_to_source_and_actor_batch() -> None:
    base = {
        "run_id": "campaign-run",
        "actor_name": "Encounter creature",
        "actor_type": "monster",
        "actor_count": 2,
        "replace_actor_id": None,
        "chunk_ids": [],
        "source_query": "",
        "source_page": None,
        "reviewed_content": None,
        "review_observation": None,
        "variant": None,
    }

    kobold = _rule_statblock_operation_token(
        source_identity={"source_id": "srd-kobold"},
        **base,
    )
    commoner = _rule_statblock_operation_token(
        source_identity={"source_id": "srd-commoner"},
        **base,
    )

    assert kobold == _rule_statblock_operation_token(
        source_identity={"source_id": "srd-kobold"},
        **base,
    )
    assert kobold != commoner
    assert kobold != _rule_statblock_operation_token(
        source_identity={"source_id": "srd-kobold"},
        **{**base, "actor_name": "Linan Swift", "actor_count": 1},
    )
    assert kobold != _rule_statblock_operation_token(
        source_identity={"source_id": "srd-kobold"},
        **{**base, "source_statblock_name": "Kobold"},
    )
    assert kobold != _rule_statblock_operation_token(
        source_identity={"source_id": "srd-kobold"},
        **{**base, "source_job_id": "retained-job-2"},
    )


def test_rule_statblock_idempotency_is_bound_to_card_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = {
        "run_id": "campaign-run",
        "source_identity": {"source_id": "srd-dragon"},
        "actor_name": "Lennithon",
        "actor_type": "monster",
        "actor_count": 1,
        "replace_actor_id": "dragon-id",
        "chunk_ids": [],
        "source_query": "adult blue dragon",
        "source_page": 91,
        "reviewed_content": None,
        "review_observation": None,
        "variant": None,
    }
    current = _rule_statblock_operation_token(**base)

    monkeypatch.setattr(
        campaign_driver,
        "RULE_STATBLOCK_CARD_PROFILE",
        "agent-ruling-v3",
    )

    assert current != _rule_statblock_operation_token(**base)


def test_blocked_candidate_override_requires_nonempty_visual_evidence(tmp_path: Path) -> None:
    path = tmp_path / "wolf.md"
    path.write_text("# WOLF\n\n**Armor Class** 13\n", encoding="utf-8")

    content, observation, resolved = _load_review_override(
        path,
        "Rendered source PDF page 63 at 200 DPI and checked all six ability scores.",
    )

    assert content.startswith("# WOLF")
    assert observation.startswith("Rendered source PDF page 63")
    assert resolved == path.resolve()
    with pytest.raises(ValueError, match="visual evidence"):
        _load_review_override(path, "")


def test_multi_page_candidate_override_requires_an_in_range_visual_page() -> None:
    candidate = {"page_start": 195, "page_end": 196}

    assert _review_override_page(candidate, 195) == 195
    with pytest.raises(ValueError, match="requires explicit --source-page"):
        _review_override_page(candidate, None)
    with pytest.raises(ValueError, match="outside"):
        _review_override_page(candidate, 197)
    with pytest.raises(ValueError, match="does not match"):
        _review_override_page({"page_start": 195, "page_end": 195}, 196)


def test_statblock_variant_file_requires_a_json_object(tmp_path: Path) -> None:
    path = tmp_path / "sildar-variant.json"
    path.write_text(
        json.dumps(
            {
                "source_ref": "module-chunk:area-6",
                "current_hit_points": 1,
                "armor_class": 10,
                "remove_actions": ["Longsword", "Heavy Crossbow"],
            }
        ),
        encoding="utf-8",
    )

    variant, resolved = _load_json_object(path, "statblock variant")

    assert variant["current_hit_points"] == 1
    assert variant["remove_actions"] == ["Longsword", "Heavy Crossbow"]
    assert resolved == path.resolve()
    path.write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError, match="must contain a JSON object"):
        _load_json_object(path, "statblock variant")


def test_prepare_statblock_accepts_an_npc_actor_type(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "regression_campaign.py",
            "--home",
            str(tmp_path),
            "--campaign-id",
            "campaign",
            "--output",
            str(tmp_path / "report.json"),
            "--actor-type",
            "npc",
        ],
    )

    assert _arguments().actor_type == "npc"


def test_prepare_statblock_accepts_agent_semantic_fill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fill_path = tmp_path / "guard-drake-fill.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "regression_campaign.py",
            "--home",
            str(tmp_path),
            "--campaign-id",
            "campaign",
            "--output",
            str(tmp_path / "report.json"),
            "--candidate-id",
            "guard-drake-candidate",
            "--agent-statblock-fill",
            str(fill_path),
        ],
    )

    assert _arguments().agent_statblock_fill == fill_path


def test_prepare_statblock_accepts_deferred_main_timeline_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "regression_campaign.py",
            "--home",
            str(tmp_path),
            "--campaign-id",
            "campaign",
            "--output",
            str(tmp_path / "report.json"),
            "--defer-checkpoint",
        ],
    )

    assert _arguments().defer_checkpoint is True


def test_prepare_statblock_rejects_deferred_isolated_branch() -> None:
    args = argparse.Namespace(
        review_id="review-1",
        candidate_id=None,
        defer_checkpoint=True,
        isolate_branch=True,
    )

    with pytest.raises(ValueError, match="cannot defer.*isolated branch"):
        asyncio.run(_prepare_statblock(args))


class _RuleStatblockSession(AbstractAsyncContextManager):
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None

    async def initialize(self) -> None:
        return None


class _RuleStatblockTransport(AbstractAsyncContextManager):
    async def __aenter__(self):
        return object(), object()

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None


class _RuleStatblockClient:
    def __init__(self, branch_id: str = "branch-1") -> None:
        self.revision = 10
        self.branch_id = branch_id
        self.calls: list[tuple[str, str, dict]] = []
        self.loaded: list[tuple[str, ...]] = []

    async def open(self) -> None:
        self.calls.append(("client", "open", {}))

    async def load(self, *group_ids: str) -> None:
        self.loaded.append(group_ids)

    async def core(self, tool_id: str, arguments: dict):
        self.calls.append(("core", tool_id, arguments))
        if tool_id == "game_phase" and arguments["action"] == "get":
            return {"tool_profile": "play"}
        if tool_id == "campaign_query":
            return {"id": "campaign-1", "revision": self.revision}
        if tool_id == "game_phase" and arguments["action"] == "set":
            assert arguments["expected_revision"] == self.revision
            self.revision += 1
            return {
                "tool_profile": arguments["tool_profile"],
                "campaign_revision": self.revision,
            }
        raise AssertionError((tool_id, arguments))

    async def domain(self, tool_id: str, arguments: dict):
        self.calls.append(("domain", tool_id, arguments))
        if tool_id == "branch_query":
            return [
                {
                    "id": self.branch_id,
                    "is_current": True,
                    "head_snapshot_id": "snapshot-0",
                }
            ]
        if tool_id == "rule_import":
            action = arguments["action"]
            if action == "stage":
                return {"job": {"id": "job-1"}, "artifact": "rulebook.pdf"}
            if action == "inspect":
                return {"inspection": {"warnings": []}}
            if action == "ingest":
                return {"source": {"id": "source-1"}}
            if action == "review_statblock":
                return {
                    "review": {
                        "id": "rule-statblock-review:kenku",
                        "source_id": "source-1",
                        "page_number": arguments["payload"]["page_number"],
                    }
                }
            raise AssertionError(arguments)
        if tool_id == "import_query":
            assert arguments == {
                "campaign_id": "campaign-1",
                "view": "list",
                "kind": "rulebook",
            }
            return []
        if tool_id == "rule_pack_query":
            if arguments["view"] == "sources":
                assert arguments["payload"] == {
                    "system_id": "dnd5e",
                    "edition": "2014",
                }
                return [
                    {
                        "id": "source-1",
                        "system_id": "dnd5e",
                        "edition": "2014",
                        "title": "Commoner",
                    }
                ]
            assert arguments["view"] == "source_chunks"
            return [
                {
                    "id": "kenku-chunk",
                    "content": "KENKU\nMedium humanoid (kenku), chaotic neutral",
                    "page_start": 195,
                    "page_end": 195,
                }
            ]
        if tool_id == "character_query":
            assert arguments == {
                "view": "get",
                "payload": {"character_id": "actor-1"},
            }
            return {
                "id": "actor-1",
                "revision": 4,
                "summary": "Prior narrative summary.",
            }
        if tool_id == "character_create_from":
            self.revision += 1
            result = {
                "character": {
                    "id": "actor-1",
                    "name": "Stirge",
                    "character_type": "monster",
                    "revision": 1,
                    "sheet": {
                        "inventory": {
                            "items": [
                                {
                                    "id": "blood-drain",
                                    "source_key": "rule-source:source-1",
                                }
                            ]
                        }
                    },
                    "derived": {
                        "hit_points": {"value": 2, "max": 2, "temp": 0},
                        "armor_class": 14,
                        "inventory": {"weapon_attacks": [{"id": "blood-drain"}]},
                    },
                },
                "statblock": {"challenge_rating": "1/8"},
                "source": {"id": "source-1"},
            }
            if arguments["payload"].get("variant") is not None:
                result["variant_evidence"] = {
                    "kind": "module-chunk",
                    "id": "opening-chunk",
                }
            return result
        if tool_id == "snapshot_create":
            assert arguments["expected_revision"] == self.revision
            self.revision += 1
            return {"id": "snapshot-1", "slot": 1}
        if tool_id == "snapshot_query":
            return {"valid": True}
        raise AssertionError((tool_id, arguments))


def _rule_statblock_args(tmp_path: Path, *, defer_checkpoint: bool) -> argparse.Namespace:
    return argparse.Namespace(
        campaign_id="campaign-1",
        source_path=None,
        source_id="source-1",
        actor_count=1,
        run_id="waterdeep-stirge",
        actor_name="Stirge",
        chunk_id="chunk-1",
        home=tmp_path,
        module_root=None,
        defer_checkpoint=defer_checkpoint,
        statblock_variant=None,
        actor_type="monster",
        replace_actor_id=None,
        source_query="",
        source_page=None,
        review_override=None,
        review_observation="",
    )


def _patch_rule_statblock_transport(
    monkeypatch: pytest.MonkeyPatch, client: _RuleStatblockClient
) -> None:
    monkeypatch.setattr(
        campaign_driver,
        "stdio_client",
        lambda _parameters: _RuleStatblockTransport(),
    )
    monkeypatch.setattr(
        campaign_driver,
        "ClientSession",
        lambda _read, _write: _RuleStatblockSession(),
    )
    monkeypatch.setattr(
        campaign_driver,
        "CampaignMcp",
        lambda _session, _campaign_id: client,
    )


def test_prepare_rule_statblock_can_defer_scene_batch_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _RuleStatblockClient()
    _patch_rule_statblock_transport(monkeypatch, client)

    report = asyncio.run(
        _prepare_rule_statblock(
            _rule_statblock_args(tmp_path, defer_checkpoint=True)
        )
    )

    assert report["snapshot"] is None
    assert report["snapshot_verification"] is None
    assert not any(
        scope == "domain" and tool_id == "snapshot_create"
        for scope, tool_id, _arguments in client.calls
    )
    assert ("play.scene", "play.scene_control", "play.characters") in client.loaded
    phase_sets = [
        arguments["tool_profile"]
        for scope, tool_id, arguments in client.calls
        if scope == "core" and tool_id == "game_phase" and arguments["action"] == "set"
    ]
    assert phase_sets == ["lobby", "play"]


def test_prepare_rule_statblock_scopes_actor_idempotency_to_current_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    keys: list[str] = []
    for branch_id in ("branch-1", "branch-2"):
        client = _RuleStatblockClient(branch_id)
        _patch_rule_statblock_transport(monkeypatch, client)
        asyncio.run(
            _prepare_rule_statblock(
                _rule_statblock_args(tmp_path, defer_checkpoint=True)
            )
        )
        keys.append(
            next(
                arguments["idempotency_key"]
                for scope, tool_id, arguments in client.calls
                if scope == "domain" and tool_id == "character_create_from"
            )
        )

    assert keys[0] != keys[1]
    assert "branch-branch-1" in keys[0]
    assert "branch-branch-2" in keys[1]


def test_prepare_rule_statblock_checkpoints_after_returning_to_play(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _RuleStatblockClient()
    _patch_rule_statblock_transport(monkeypatch, client)

    report = asyncio.run(
        _prepare_rule_statblock(
            _rule_statblock_args(tmp_path, defer_checkpoint=False)
        )
    )

    assert report["snapshot"]["id"] == "snapshot-1"
    assert report["snapshot_verification"]["valid"] is True
    call_names = [(scope, tool_id) for scope, tool_id, _arguments in client.calls]
    return_to_play = max(
        index
        for index, (scope, tool_id) in enumerate(call_names)
        if scope == "core" and tool_id == "game_phase"
    )
    checkpoint = call_names.index(("domain", "snapshot_create"))
    assert return_to_play < checkpoint


def test_prepare_rule_statblock_applies_source_cited_variant_and_actor_type(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _RuleStatblockClient()
    _patch_rule_statblock_transport(monkeypatch, client)
    variant_path = tmp_path / "troll-variant.json"
    variant_path.write_text(
        json.dumps(
            {
                "source_ref": "module-chunk:opening-chunk",
                "current_hit_points": 44,
            }
        ),
        encoding="utf-8",
    )
    args = _rule_statblock_args(tmp_path, defer_checkpoint=True)
    args.actor_type = "npc"
    args.statblock_variant = variant_path

    report = asyncio.run(_prepare_rule_statblock(args))

    create_call = next(
        arguments
        for scope, tool_id, arguments in client.calls
        if scope == "domain" and tool_id == "character_create_from"
    )
    assert create_call["payload"]["character_type"] == "npc"
    assert create_call["payload"]["variant"]["current_hit_points"] == 44
    assert report["variant"]["source_ref"] == "module-chunk:opening-chunk"
    assert report["variant_evidence"]["id"] == "opening-chunk"
    assert report["variant_path"] == str(variant_path.resolve())


def test_prepare_rule_statblock_can_rebuild_an_existing_actor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _RuleStatblockClient()
    _patch_rule_statblock_transport(monkeypatch, client)
    args = _rule_statblock_args(tmp_path, defer_checkpoint=True)
    args.replace_actor_id = "actor-1"

    asyncio.run(_prepare_rule_statblock(args))

    create_call = next(
        arguments
        for scope, tool_id, arguments in client.calls
        if scope == "domain" and tool_id == "character_create_from"
    )
    assert create_call["payload"]["replace_character_id"] == "actor-1"
    assert create_call["payload"]["expected_revision"] == 4
    assert create_call["payload"]["summary"] == "Prior narrative summary."


def test_statblock_replacement_fields_are_shared_by_module_and_rule_preparation() -> None:
    client = _RuleStatblockClient()

    assert asyncio.run(_statblock_replacement_fields(client, None)) == {}
    assert asyncio.run(_statblock_replacement_fields(client, "actor-1")) == {
        "replace_character_id": "actor-1",
        "expected_revision": 4,
        "summary": "Prior narrative summary.",
    }


def test_prepare_rule_statblock_discovers_chunks_by_source_page_and_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _RuleStatblockClient()
    _patch_rule_statblock_transport(monkeypatch, client)
    args = _rule_statblock_args(tmp_path, defer_checkpoint=True)
    args.chunk_id = []
    args.source_query = "Kenku"
    args.source_page = 195
    args.source_statblock_name = "Kenku"

    report = asyncio.run(_prepare_rule_statblock(args))

    query_call = next(
        arguments
        for scope, tool_id, arguments in client.calls
        if scope == "domain" and tool_id == "rule_pack_query"
    )
    assert query_call["payload"] == {
        "source_id": "source-1",
        "query": "Kenku",
        "page": 195,
        "limit": 200,
    }
    create_call = next(
        arguments
        for scope, tool_id, arguments in client.calls
        if scope == "domain" and tool_id == "character_create_from"
    )
    assert create_call["payload"]["chunk_ids"] == ["kenku-chunk"]
    assert create_call["payload"]["source_statblock_name"] == "Kenku"
    assert report["selected_source_chunks"][0]["page_start"] == 195


def test_discover_rule_chunks_returns_boundaries_without_creating_an_actor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _RuleStatblockClient()
    _patch_rule_statblock_transport(monkeypatch, client)
    args = _rule_statblock_args(tmp_path, defer_checkpoint=True)
    args.source_query = "Kenku"
    args.source_page = 195

    report = asyncio.run(_discover_rule_chunks(args))

    assert report["query"] == {
        "source_id": "source-1",
        "query": "Kenku",
        "page": 195,
        "limit": 200,
    }
    assert report["chunks"] == [
        {
            "id": "kenku-chunk",
            "content": "KENKU\nMedium humanoid (kenku), chaotic neutral",
            "page_start": 195,
            "page_end": 195,
        }
    ]
    assert not any(
        scope == "domain" and tool_id == "character_create_from"
        for scope, tool_id, _arguments in client.calls
    )
    phase_sets = [
        arguments["tool_profile"]
        for scope, tool_id, arguments in client.calls
        if scope == "core" and tool_id == "game_phase" and arguments["action"] == "set"
    ]
    assert phase_sets == ["lobby", "play"]


def test_discover_rule_sources_uses_public_lobby_query(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _RuleStatblockClient()
    _patch_rule_statblock_transport(monkeypatch, client)
    args = _rule_statblock_args(tmp_path, defer_checkpoint=False)

    report = asyncio.run(_discover_rule_sources(args))

    assert report["action"] == "discover-rule-sources"
    assert report["initial_phase"] == "play"
    assert report["sources"] == [
        {
            "id": "source-1",
            "system_id": "dnd5e",
            "edition": "2014",
            "title": "Commoner",
        }
    ]
    assert report["import_jobs"] == []
    assert any(
        scope == "domain"
        and tool_id == "rule_pack_query"
        and arguments["view"] == "sources"
        for scope, tool_id, arguments in client.calls
    )


def test_prepare_rule_statblock_uses_checksum_bound_visual_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _RuleStatblockClient()
    _patch_rule_statblock_transport(monkeypatch, client)
    source = tmp_path / "monster-manual.pdf"
    source.write_bytes(b"test fixture")
    override = tmp_path / "kenku.md"
    override.write_text("# Kenku\n\nReviewed statblock.", encoding="utf-8")
    args = _rule_statblock_args(tmp_path, defer_checkpoint=True)
    args.source_id = None
    args.source_path = source
    args.chunk_id = []
    args.source_page = 195
    args.review_override = override
    args.review_observation = "Visually checked every Kenku field on rendered PDF page 195."

    report = asyncio.run(_prepare_rule_statblock(args))

    review_call = next(
        arguments
        for scope, tool_id, arguments in client.calls
        if scope == "domain"
        and tool_id == "rule_import"
        and arguments["action"] == "review_statblock"
    )
    assert review_call["payload"]["page_number"] == 195
    assert review_call["payload"]["normalized_content"].startswith("# Kenku")
    create_call = next(
        arguments
        for scope, tool_id, arguments in client.calls
        if scope == "domain" and tool_id == "character_create_from"
    )
    assert create_call["mode"] == "reviewed_rule_statblock"
    assert create_call["payload"]["review_id"] == "rule-statblock-review:kenku"
    assert report["rule_review"]["source_id"] == "source-1"
    assert report["review_override_path"] == str(override.resolve())


def test_prepare_rule_statblock_uses_contiguous_agent_text_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class AgentTextClient(_RuleStatblockClient):
        async def domain(self, tool_id: str, arguments: dict):
            if tool_id == "import_query":
                self.calls.append(("domain", tool_id, arguments))
                return [
                    {
                        "id": job_id,
                        "kind": "rulebook",
                        "source_id": "source-1",
                        "artifact": "rules.pdf",
                        "artifact_checksum": "same-checksum",
                    }
                    for job_id in ("job-source-2", "job-source-1")
                ]
            if (
                tool_id == "rule_pack_query"
                and arguments["view"] == "source_chunks"
            ):
                self.calls.append(("domain", tool_id, arguments))
                return [
                    {
                        "id": f"evidence-{ordinal}",
                        "ordinal": ordinal,
                        "heading_path": ["COMMONER"],
                        "content": f"evidence {ordinal}",
                        "page_start": 1,
                        "page_end": 1,
                    }
                    for ordinal in range(3)
                ]
            return await super().domain(tool_id, arguments)

    client = AgentTextClient()
    _patch_rule_statblock_transport(monkeypatch, client)
    review = tmp_path / "commoner.md"
    review.write_text(
        """### Commoner

*Medium humanoid (any race), any alignment*

**Armor Class** 10
**Hit Points** 4 (1d8)
**Speed** 30 ft.

| STR | DEX | CON | INT | WIS | CHA |
|:---:|:---:|:---:|:---:|:---:|:---:|
| 10 (+0) | 10 (+0) | 10 (+0) | 10 (+0) | 10 (+0) | 10 (+0) |

**Challenge** 0 (10 XP)
""",
        encoding="utf-8",
    )
    args = _rule_statblock_args(tmp_path, defer_checkpoint=True)
    args.actor_name = "Commoner"
    args.chunk_id = ["evidence-0", "evidence-1", "evidence-2"]
    args.source_page = 1
    args.agent_rule_statblock_review = review
    args.review_observation = (
        "Agent normalized only exact contiguous indexed rule text on page 1."
    )

    report = asyncio.run(_prepare_rule_statblock(args))

    review_call = next(
        arguments
        for scope, tool_id, arguments in client.calls
        if scope == "domain"
        and tool_id == "rule_import"
        and arguments["action"] == "review_statblock"
    )
    assert review_call["payload"]["review_mode"] == "agent_text"
    assert review_call["payload"]["job_id"] == "job-source-1"
    assert review_call["payload"]["evidence_chunk_ids"] == [
        "evidence-0",
        "evidence-1",
        "evidence-2",
    ]
    assert report["review_mode"] == "agent_text"
    assert report["equivalent_source_import_jobs"] == [
        "job-source-1",
        "job-source-2",
    ]
    assert [item["ordinal"] for item in report["review_evidence_chunks"]] == [
        0,
        1,
        2,
    ]
    assert report["review_override_path"] == str(review.resolve())


def test_prepare_rule_statblock_rejects_ambiguous_agent_text_import_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class AmbiguousImportClient(_RuleStatblockClient):
        async def domain(self, tool_id: str, arguments: dict):
            if tool_id == "import_query":
                self.calls.append(("domain", tool_id, arguments))
                return [
                    {
                        "id": "job-1",
                        "source_id": "source-1",
                        "artifact": "first.pdf",
                        "artifact_checksum": "first-checksum",
                    },
                    {
                        "id": "job-2",
                        "source_id": "source-1",
                        "artifact": "second.pdf",
                        "artifact_checksum": "second-checksum",
                    },
                ]
            return await super().domain(tool_id, arguments)

    client = AmbiguousImportClient()
    _patch_rule_statblock_transport(monkeypatch, client)
    review = tmp_path / "commoner.md"
    review.write_text("### Commoner\n", encoding="utf-8")
    args = _rule_statblock_args(tmp_path, defer_checkpoint=True)
    args.source_page = 1
    args.agent_rule_statblock_review = review
    args.review_observation = "Exact indexed text evidence was normalized by the Agent."

    with pytest.raises(RuntimeError, match="unambiguous retained artifact identity"):
        asyncio.run(_prepare_rule_statblock(args))

    assert not any(
        scope == "domain"
        and tool_id == "rule_import"
        and arguments["action"] == "review_statblock"
        for scope, tool_id, arguments in client.calls
    )

    resolved_client = AmbiguousImportClient()
    _patch_rule_statblock_transport(monkeypatch, resolved_client)
    resolved_args = _rule_statblock_args(tmp_path, defer_checkpoint=True)
    resolved_args.chunk_id = ["kenku-chunk"]
    resolved_args.source_page = 195
    resolved_args.agent_rule_statblock_review = review
    resolved_args.review_observation = (
        "Exact indexed text evidence was normalized by the Agent."
    )
    resolved_args.source_job_id = "job-2"

    report = asyncio.run(_prepare_rule_statblock(resolved_args))

    review_call = next(
        arguments
        for scope, tool_id, arguments in resolved_client.calls
        if scope == "domain"
        and tool_id == "rule_import"
        and arguments["action"] == "review_statblock"
    )
    assert review_call["payload"]["job_id"] == "job-2"
    assert report["source_import_job"]["id"] == "job-2"


def test_prepare_rule_statblock_rejects_noncontiguous_agent_text_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class GappedEvidenceClient(_RuleStatblockClient):
        async def domain(self, tool_id: str, arguments: dict):
            if tool_id == "import_query":
                self.calls.append(("domain", tool_id, arguments))
                return [{"id": "job-1", "source_id": "source-1"}]
            if (
                tool_id == "rule_pack_query"
                and arguments["view"] == "source_chunks"
            ):
                self.calls.append(("domain", tool_id, arguments))
                return [
                    {
                        "id": f"evidence-{ordinal}",
                        "ordinal": ordinal,
                        "heading_path": ["COMMONER"],
                        "content": "evidence",
                        "page_start": 1,
                        "page_end": 1,
                    }
                    for ordinal in (0, 2)
                ]
            return await super().domain(tool_id, arguments)

    client = GappedEvidenceClient()
    _patch_rule_statblock_transport(monkeypatch, client)
    review = tmp_path / "commoner.md"
    review.write_text("### Commoner\n", encoding="utf-8")
    args = _rule_statblock_args(tmp_path, defer_checkpoint=True)
    args.chunk_id = ["evidence-0", "evidence-2"]
    args.source_page = 1
    args.agent_rule_statblock_review = review
    args.review_observation = "Exact indexed text evidence was normalized by the Agent."

    with pytest.raises(RuntimeError, match="ordered contiguous page segment"):
        asyncio.run(_prepare_rule_statblock(args))

    assert not any(
        scope == "domain"
        and tool_id == "rule_import"
        and arguments["action"] == "review_statblock"
        for scope, tool_id, arguments in client.calls
    )


def test_prepare_rule_statblock_recovers_layout_ocr_without_image_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class OcrFallbackClient(_RuleStatblockClient):
        def __init__(self) -> None:
            super().__init__()
            self.failed_creation = False

        async def domain(self, tool_id: str, arguments: dict):
            if (
                tool_id == "character_create_from"
                and arguments["mode"] == "statblock"
                and not self.failed_creation
            ):
                self.calls.append(("domain", tool_id, arguments))
                self.failed_creation = True
                raise RuntimeError(
                    "character_create_from: statblock is missing size, type, and alignment"
                )
            if (
                tool_id == "rule_import"
                and arguments["action"] == "recover_statblock"
            ):
                self.calls.append(("domain", tool_id, arguments))
                return {
                    "review": {
                        "id": "rule-statblock-review:adult-blue-dragon",
                        "source_id": "source-1",
                        "page_number": 92,
                    }
                }
            return await super().domain(tool_id, arguments)

    client = OcrFallbackClient()
    _patch_rule_statblock_transport(monkeypatch, client)
    source = tmp_path / "monster-manual.pdf"
    source.write_bytes(b"test fixture")
    args = _rule_statblock_args(tmp_path, defer_checkpoint=True)
    args.source_id = None
    args.source_path = source
    args.chunk_id = []
    args.source_query = "Adult Blue Dragon"
    args.actor_name = "Lennithon"
    args.source_statblock_name = "Adult Blue Dragon"

    report = asyncio.run(_prepare_rule_statblock(args))

    recovery_call = next(
        arguments
        for scope, tool_id, arguments in client.calls
        if scope == "domain"
        and tool_id == "rule_import"
        and arguments["action"] == "recover_statblock"
    )
    assert recovery_call["payload"] == {
        "job_id": "job-1",
        "name": "Adult Blue Dragon",
    }
    create_calls = [
        arguments
        for scope, tool_id, arguments in client.calls
        if scope == "domain" and tool_id == "character_create_from"
    ]
    assert [call["mode"] for call in create_calls] == [
        "statblock",
        "reviewed_rule_statblock",
    ]
    assert (
        create_calls[1]["payload"]["review_id"]
        == "rule-statblock-review:adult-blue-dragon"
    )
    assert report["rule_review"]["page_number"] == 92
    assert report["source_statblock_name"] == "Adult Blue Dragon"


def test_prepare_rule_statblock_recovers_an_existing_indexed_source_by_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class IndexedOcrFallbackClient(_RuleStatblockClient):
        def __init__(self) -> None:
            super().__init__()
            self.failed_creation = False

        async def domain(self, tool_id: str, arguments: dict):
            if tool_id == "import_query":
                self.calls.append(("domain", tool_id, arguments))
                return [
                    {
                        "id": "existing-mm-job",
                        "kind": "rulebook",
                        "source_id": "source-1",
                        "artifact": "monster-manual.pdf",
                    }
                ]
            if (
                tool_id == "character_create_from"
                and arguments["mode"] == "statblock"
                and not self.failed_creation
            ):
                self.calls.append(("domain", tool_id, arguments))
                self.failed_creation = True
                raise RuntimeError(
                    "statblock source chunks contain no creature core headed 'Ettercap'"
                )
            if (
                tool_id == "rule_import"
                and arguments["action"] == "recover_statblock"
            ):
                self.calls.append(("domain", tool_id, arguments))
                return {
                    "review": {
                        "id": "rule-statblock-review:ettercap",
                        "source_id": "source-1",
                        "page_number": 132,
                    },
                    "provider": "rapidocr",
                    "corroboration_mode": "embedded_text",
                }
            return await super().domain(tool_id, arguments)

    client = IndexedOcrFallbackClient()
    _patch_rule_statblock_transport(monkeypatch, client)
    args = _rule_statblock_args(tmp_path, defer_checkpoint=True)
    args.chunk_id = ["ettercap-core", "ettercap-actions"]
    args.source_page = 132
    args.source_statblock_name = "Ettercap"

    report = asyncio.run(_prepare_rule_statblock(args))

    recovery_call = next(
        arguments
        for scope, tool_id, arguments in client.calls
        if scope == "domain"
        and tool_id == "rule_import"
        and arguments["action"] == "recover_statblock"
    )
    assert recovery_call["payload"] == {
        "job_id": "existing-mm-job",
        "name": "Ettercap",
        "page_number": 132,
    }
    create_calls = [
        arguments
        for scope, tool_id, arguments in client.calls
        if scope == "domain" and tool_id == "character_create_from"
    ]
    assert [call["mode"] for call in create_calls] == [
        "statblock",
        "reviewed_rule_statblock",
    ]
    assert create_calls[1]["payload"]["job_id"] == "existing-mm-job"
    assert report["source_import_job"]["id"] == "existing-mm-job"
    assert report["ocr_recovery"]["provider"] == "rapidocr"


def test_failed_rule_statblock_preparation_uses_shared_phase_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _RuleStatblockClient()
    _patch_rule_statblock_transport(monkeypatch, client)
    original = {"phase": "play", "branch_id": "branch-1"}
    restored: list[dict] = []

    async def context(_client, _campaign_id):
        return original

    async def fail(_args):
        raise RuntimeError("malformed source selection")

    async def restore(_client, **arguments):
        restored.append(arguments)
        return {"phase_changes": []}

    monkeypatch.setattr(campaign_driver, "_statblock_preparation_context", context)
    monkeypatch.setattr(campaign_driver, "_prepare_rule_statblock", fail)
    monkeypatch.setattr(campaign_driver, "_restore_statblock_preparation_context", restore)
    args = _rule_statblock_args(tmp_path, defer_checkpoint=True)

    with pytest.raises(RuntimeError, match="malformed source selection"):
        asyncio.run(_prepare_rule_statblock_with_recovery(args))

    assert restored == [
        {
            "campaign_id": "campaign-1",
            "original": original,
            "token": "waterdeep-stirge",
        }
    ]


def test_failed_statblock_preparation_restores_original_play_phase() -> None:
    class Client:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict]] = []
            self.loaded: list[tuple[str, ...]] = []

        async def open(self) -> None:
            self.calls.append(("open", {}))

        async def load(self, *group_ids: str) -> None:
            self.loaded.append(group_ids)

        async def core(self, tool_id: str, arguments: dict):
            self.calls.append((tool_id, arguments))
            if tool_id == "game_phase" and arguments["action"] == "get":
                return {"tool_profile": "lobby"}
            if tool_id == "campaign_query":
                return {"id": "campaign-1", "revision": 12}
            if tool_id == "game_phase" and arguments["action"] == "set":
                assert arguments["tool_profile"] == "play"
                assert arguments["branch_id"] == "branch-1"
                assert arguments["expected_revision"] == 12
                return {"tool_profile": "play", "campaign_revision": 13}
            raise AssertionError((tool_id, arguments))

        async def domain(self, tool_id: str, arguments: dict):
            self.calls.append((tool_id, arguments))
            if tool_id == "branch_query":
                return [{"id": "branch-1", "is_current": True}]
            raise AssertionError((tool_id, arguments))

    client = Client()
    result = asyncio.run(
        _restore_statblock_preparation_context(
            client,
            campaign_id="campaign-1",
            original={"phase": "play", "branch_id": "branch-1"},
            token="prepare-token",
        )
    )

    assert result["checkout"] is None
    assert result["phase_changes"] == [
        {"tool_profile": "play", "campaign_revision": 13}
    ]
    phase_set = next(
        arguments
        for tool_id, arguments in client.calls
        if tool_id == "game_phase" and arguments["action"] == "set"
    )
    assert phase_set["idempotency_key"].startswith(
        "prepare-token-failure-restore-play-"
    )


def test_statblock_creation_key_scopes_repeated_source_actors_by_identity() -> None:
    common = {
        "run_id": "full-campaign",
        "review_id": "bugbear-review",
        "actor_type": "monster",
        "variant": None,
    }

    first = _statblock_creation_key(actor_name="Mosk", **common)
    repeated = _statblock_creation_key(actor_name="Mosk", **common)
    second = _statblock_creation_key(actor_name="Area 9 Bugbear 2", **common)

    assert first == repeated
    assert first != second
    assert first.startswith("full-campaign-create-statblock-")


def test_character_summary_keeps_provenance_for_a_disarmed_module_npc() -> None:
    summary = _character_summary(
        {
            "id": "sildar",
            "name": "Sildar Hallwinter",
            "character_type": "npc",
            "revision": 1,
            "sheet": {"inventory": {"items": []}, "content": {}},
            "derived": {
                "hit_points": {"value": 1, "max": 27, "temp": 0},
                "armor_class": 10,
                "inventory": {"weapon_attacks": []},
            },
            "notes": {
                "profile": {
                    "dm_notes": "Reviewed module statblock: module-review:sildar."
                }
            },
        }
    )

    assert summary["source_bound"] is True
    assert summary["statblock_source_preserved"] is True
    assert summary["narrative_source_preserved"] is False
    assert len(summary["notes_sha256"]) == 64


def test_character_summary_audits_in_place_narrative_materialization() -> None:
    summary = _character_summary(
        {
            "id": "actor-1",
            "name": "Caldan Voss",
            "summary": "A source-authored actor-troupe member.",
            "character_type": "npc",
            "revision": 2,
            "sheet": {
                "adventure_state": {"status_tags": []},
                "inventory": {"items": []},
                "content": {},
            },
            "derived": {
                "hit_points": {"value": 4, "max": 4, "temp": 0},
                "armor_class": 10,
                "inventory": {"weapon_attacks": [{"item_id": "club"}]},
            },
            "notes": {
                "profile": {
                    "dm_notes": (
                        'sagasmith:narrative-npc-source:{"source_identity":'
                        '"troop of actors"}\n'
                        "Statblock import: rule-source:commoner."
                    )
                }
            },
        }
    )

    assert summary["summary"] == "A source-authored actor-troupe member."
    assert summary["narrative_source_preserved"] is True
    assert summary["statblock_source_preserved"] is True
    assert summary["attack_count"] == 1


def test_character_summary_counts_known_and_prepared_spells_without_conflation() -> None:
    summary = _character_summary(
        {
            "id": "bard",
            "name": "Bard",
            "character_type": "pc",
            "revision": 1,
            "sheet": {
                "inventory": {"items": []},
                "content": {
                    "spells": [
                        {"id": "known-1", "access": {"known": True}},
                        {"id": "known-2", "access": {"known": True}},
                        {"id": "prepared-1", "access": {"prepared": True}},
                        {"id": "spellbook-only", "access": {"spellbook": True}},
                    ]
                },
            },
            "derived": {
                "hit_points": {"value": 8, "max": 8, "temp": 0},
                "armor_class": 12,
                "inventory": {"weapon_attacks": []},
                "spellcasting": {
                    "prepared_spell_ids": ["prepared-1"],
                },
            },
            "notes": {"profile": {}},
        }
    )

    assert summary["spell_count"] == 3
    assert summary["known_spell_count"] == 2
    assert summary["prepared_spell_count"] == 1
    assert summary["spell_card_count"] == 4


def test_expanded_source_ref_keeps_exact_module_scene_and_content_identity() -> None:
    content = "An adventure for four to six characters."
    expanded = {
        "chunk_id": "chunk-1",
        "content": content,
        "heading_path": ["Introduction", "Character Advancement"],
        "page_start": 7,
        "page_end": 8,
        "module": {"id": "module-1", "title": "Campaign"},
        "chapter": {"id": "chapter-1", "title": "Introduction"},
        "scene": {
            "id": "scene-1",
            "title": "Character Advancement",
            "stable_key": "introduction/character-advancement",
        },
    }

    assert _expanded_source_ref(expanded) == {
        "module_id": "module-1",
        "module_title": "Campaign",
        "chapter_id": "chapter-1",
        "chapter_title": "Introduction",
        "scene_id": "scene-1",
        "scene_title": "Character Advancement",
        "scene_stable_key": "introduction/character-advancement",
        "chunk_id": "chunk-1",
        "heading_path": ["Introduction", "Character Advancement"],
        "page_start": 7,
        "page_end": 8,
        "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
    }


def test_campaign_report_streams_are_reconfigured_for_source_text() -> None:
    stream = io.TextIOWrapper(io.BytesIO(), encoding="cp936")

    _configure_utf8_streams(stream)
    stream.write("£")
    stream.flush()

    assert stream.encoding == "utf-8"


def test_noncombat_scene_inputs_are_validated_before_branch_setup() -> None:
    scene = {
        "content": "A DC 10 Wisdom (Survival) check reveals the Goblin Trail.",
        "locations": [{"key": "goblin-ambush"}],
    }

    _validate_noncombat_scene(
        scene,
        source_excerpt="A DC 10 Wisdom (Survival) check reveals the Goblin Trail.",
        location_key="goblin-ambush",
    )
    with pytest.raises(RuntimeError, match="location is not present"):
        _validate_noncombat_scene(
            scene,
            source_excerpt="A DC 10 Wisdom (Survival) check reveals the Goblin Trail.",
            location_key="goblin-trail",
        )


def test_full_campaign_corpus_accounts_for_every_asset_and_uses_max_party_size() -> None:
    manifest_path = Path(__file__).resolve().parents[1] / "fixtures" / "full_campaign_corpus.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    entries = []
    for line in manifest["campaign_lines"]:
        entries.extend(line["modules"])
        entries.extend(line["player_materials"])
        entries.extend(line["assets"])
        party_size = line["play_requirements"]["recommended_party_size"]
        if party_size["status"] == "source_confirmed":
            assert party_size["selected"] == party_size["maximum"]
        elif party_size["status"] == "dm_review_completed":
            assert party_size["selected"] == party_size["maximum"]
            assert party_size["review"]["represented_as_module_recommendation"] is False
        else:
            assert party_size["status"] == "dm_review_required"
            assert party_size["selected"] is None
    entries.extend(manifest["unassigned_assets"])

    paths = [entry["path"] for entry in entries]
    assert len(paths) == manifest["expected_asset_count"] == 21
    assert len(paths) == len(set(paths))
    assert all(len(entry["sha256"]) == 64 for entry in entries)
    tyranny = next(
        line for line in manifest["campaign_lines"] if line["id"] == "tyranny-of-dragons"
    )
    assert [module["sequence"] for module in tyranny["modules"]] == [1, 2]
    assert tyranny["play_requirements"]["continuity"]["preserve_party"] is True
    waterdeep = next(
        line
        for line in manifest["campaign_lines"]
        if line["id"] == "waterdeep-dragon-heist"
    )
    reviewed_size = waterdeep["play_requirements"]["recommended_party_size"]
    assert reviewed_size["status"] == "dm_review_completed"
    assert reviewed_size["selected"] == 4
    assert reviewed_size["review"]["module_party_size_status"] == "not_stated"
