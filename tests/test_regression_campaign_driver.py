from __future__ import annotations

import argparse
import asyncio
import hashlib
import io
import json
import sys
from pathlib import Path

import pytest

from scripts.regression_campaign import (
    _arguments,
    _character_summary,
    _configure_utf8_streams,
    _expanded_source_ref,
    _load_json_object,
    _load_review_override,
    _prepare_statblock,
    _restore_statblock_preparation_context,
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
