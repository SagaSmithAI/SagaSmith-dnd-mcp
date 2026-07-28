from __future__ import annotations

import asyncio
from copy import deepcopy

import pytest
from sagasmith_dnd.playthrough import new_playthrough_manifest

from scripts.regression_playthrough import _record_outcome, _validate_source_ref


def _source_ref(*, chunk_id: str = "chunk-trigger") -> dict:
    return {
        "module_id": "module-1",
        "scene_id": "scene-1",
        "chunk_id": chunk_id,
        "page_start": 27,
        "page_end": 27,
        "heading_path": ["Episode 2", "Ice Hunters"],
        "content_sha256": "a" * 64,
    }


def _manifest_source_ref() -> dict:
    return {
        "purpose": "test",
        "asset_path": "module.pdf",
        "asset_sha256": "b" * 64,
        "page_start": 27,
        "page_end": 27,
        "heading_path": ["Episode 2", "Ice Hunters"],
        "content_sha256": "a" * 64,
        **_source_ref(),
        "excerpt": "A result of 1 means an encounter occurs.",
    }


def test_source_ref_validation_uses_the_exact_public_chunk() -> None:
    source_ref = _source_ref()

    class Client:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict]] = []

        async def domain(self, tool_id: str, arguments: dict):
            self.calls.append((tool_id, deepcopy(arguments)))
            assert tool_id == "module_expand"
            return {
                "chunk_id": "chunk-trigger",
                "content": "A result of 1 means an encounter occurs.",
                "content_sha256": "a" * 64,
                "source_ref": deepcopy(source_ref),
            }

    client = Client()
    result = asyncio.run(
        _validate_source_ref(
            client,
            {
                "module_id": "module-1",
                "scene_id": "scene-1",
                "content": (
                    "A result of 1 means an encounter occurs. The fishers paddle northeast."
                ),
            },
            source_ref,
            excerpt="A result of 1 means an encounter occurs.",
        )
    )

    assert result == source_ref
    assert client.calls == [("module_expand", {"chunk_id": "chunk-trigger"})]


def test_source_ref_validation_rejects_an_excerpt_from_an_adjacent_chunk() -> None:
    source_ref = _source_ref()

    class Client:
        async def domain(self, tool_id: str, arguments: dict):
            assert tool_id == "module_expand"
            assert arguments == {"chunk_id": "chunk-trigger"}
            return {
                "chunk_id": "chunk-trigger",
                "content": "A result of 1 means an encounter occurs.",
                "content_sha256": "a" * 64,
                "source_ref": deepcopy(source_ref),
            }

    with pytest.raises(ValueError, match="cited chunk"):
        asyncio.run(
            _validate_source_ref(
                Client(),
                {
                    "module_id": "module-1",
                    "scene_id": "scene-1",
                    "content": (
                        "A result of 1 means an encounter occurs. The fishers paddle northeast."
                    ),
                },
                source_ref,
                excerpt="The fishers paddle northeast.",
            )
        )


def test_source_ref_validation_uses_canonical_typographic_normalization() -> None:
    source_ref = _source_ref()

    class Client:
        async def domain(self, tool_id: str, arguments: dict):
            assert tool_id == "module_expand"
            assert arguments == {"chunk_id": "chunk-trigger"}
            return {
                "chunk_id": "chunk-trigger",
                "content": "\x02The dragon’s \u00adhoard—HERE.",
                "content_sha256": "a" * 64,
                "source_ref": deepcopy(source_ref),
            }

    result = asyncio.run(
        _validate_source_ref(
            Client(),
            {
                "module_id": "module-1",
                "scene_id": "scene-1",
            },
            source_ref,
            excerpt="The dragon's hoard-here.",
        )
    )

    assert result == source_ref


def test_source_ref_validation_rejects_extra_fields_before_expansion() -> None:
    source_ref = {**_source_ref(), "unmanaged_field": "not a source contract field"}

    class Client:
        async def domain(self, tool_id: str, arguments: dict):
            raise AssertionError((tool_id, arguments))

    with pytest.raises(ValueError, match="unsupported fields: unmanaged_field"):
        asyncio.run(
            _validate_source_ref(
                Client(),
                {
                    "module_id": "module-1",
                    "scene_id": "scene-1",
                },
                source_ref,
            )
        )


def test_record_outcome_rejects_an_adjacent_chunk_before_any_mutation() -> None:
    source_ref = _source_ref()

    class Client:
        def __init__(self) -> None:
            self.calls: list[str] = []
            self.manifest = new_playthrough_manifest(
                run_id="run-1",
                campaign_line_id="line-1",
                module_ids=["module-1"],
                recommended_party_minimum=4,
                recommended_party_maximum=4,
                selected_party_size=4,
                source_refs=[_manifest_source_ref()],
            )

        async def load(self, *_group_ids: str) -> None:
            return None

        async def domain(self, tool_id: str, arguments: dict):
            self.calls.append(tool_id)
            if tool_id == "playthrough_manifest":
                assert arguments["action"] == "get"
                return {"manifest": deepcopy(self.manifest), "campaign_revision": 1}
            if tool_id == "module_query":
                assert arguments["view"] == "scene"
                return {
                    "module_id": "module-1",
                    "scene_id": "scene-1",
                    "content": (
                        "A result of 1 means an encounter occurs. The fishers paddle northeast."
                    ),
                    "locations": [{"key": "sea-of-moving-ice"}],
                }
            if tool_id == "module_expand":
                return {
                    "chunk_id": "chunk-trigger",
                    "content": "A result of 1 means an encounter occurs.",
                    "content_sha256": "a" * 64,
                    "source_ref": deepcopy(source_ref),
                }
            raise AssertionError((tool_id, arguments))

    client = Client()
    with pytest.raises(ValueError, match="cited chunk"):
        asyncio.run(
            _record_outcome(
                client,
                campaign_id="campaign-1",
                run_id="run-1",
                outcome_id="follow-fishers",
                scene_id="scene-1",
                location_key="sea-of-moving-ice",
                source_excerpt="The fishers paddle northeast.",
                source_ref=source_ref,
                event_type="navigation_choice",
                summary="The party follows the fishers northeast.",
                knowledge="",
                knowledge_actor_ids=[],
                facts=[
                    {
                        "fact_key": "sea_of_moving_ice:fishers:followed",
                        "content": "true",
                    }
                ],
                npc_states=[],
                quest_states=[],
                clue_states=[],
                world_state={},
                objective="Follow the fishers.",
                progress_percent=50,
            )
        )

    assert client.calls == [
        "playthrough_manifest",
        "module_query",
        "module_expand",
    ]
    assert "module_set_progress" not in client.calls
    assert "memory_change" not in client.calls
