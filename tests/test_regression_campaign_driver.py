from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path

import pytest

from scripts.regression_campaign import (
    _configure_utf8_streams,
    _expanded_source_ref,
    _load_json_object,
    _load_review_override,
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
