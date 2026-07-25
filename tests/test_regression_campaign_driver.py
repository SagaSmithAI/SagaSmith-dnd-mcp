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
    _expanded_source_ref,
    _load_json_object,
    _load_review_override,
    _prepare_rule_statblock,
    _prepare_rule_statblock_with_recovery,
    _prepare_statblock,
    _restore_statblock_preparation_context,
    _review_override_page,
    _statblock_creation_key,
    _validate_noncombat_scene,
)


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
    def __init__(self) -> None:
        self.revision = 10
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
                    "id": "branch-1",
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
        if tool_id == "rule_pack_query":
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
            return {"id": "actor-1", "revision": 4}
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


def test_prepare_rule_statblock_discovers_chunks_by_source_page_and_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _RuleStatblockClient()
    _patch_rule_statblock_transport(monkeypatch, client)
    args = _rule_statblock_args(tmp_path, defer_checkpoint=True)
    args.chunk_id = []
    args.source_query = "Kenku"
    args.source_page = 195

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
