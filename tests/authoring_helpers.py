import copy
import hashlib
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from sagasmith_core.content_pack import dumps_content_archive
from sagasmith_core.indexed_source import rule_chunk_key
from sagasmith_dnd.content_packages import build_rule_content_package

CallTool = Callable[[Any, str, dict[str, Any]], Awaitable[Any]]


async def import_and_activate_addon_fixture(
    call: CallTool,
    server: Any,
    campaign_id: str,
    home: Path,
    *,
    manifest: dict[str, Any],
    artifacts: list[dict[str, Any]],
    mechanics: list[dict[str, Any]],
    expected_revision: int,
    request_key: str,
) -> dict[str, Any]:
    """Import a finalized synthetic addon fixture through the new Pack boundary."""

    package_id = str(manifest["id"])
    version = str(manifest["version"])
    dependencies = [
        {
            "kind": str(item.get("kind") or "addon"),
            "id": str(item["id"]),
            "version": str(item["version"]),
            "checksum": str(item["checksum"]),
            "optional": bool(item.get("optional", False)),
        }
        for item in list(manifest.get("dependencies") or [])
    ]
    rule_dependencies = [
        {
            "kind": "rule_pack",
            "id": str(item["id"]),
            "version": str(item["version"]),
            "checksum": str(item["rule_checksum"]),
            "optional": False,
        }
        for item in list(manifest.get("dependencies") or [])
    ]
    package_manifest = {
        key: copy.deepcopy(value)
        for key, value in manifest.items()
        if key != "dependencies"
    }
    source_key = f"fixture.{request_key}"
    names = [str(dict(item.get("card") or {}).get("name") or item["id"]) for item in artifacts]
    source_text = "# Reviewed fixture\n\n" + "\n\n".join(
        f"## {name}\n\nMechanics and choices for {name} were reviewed for this fixture."
        for name in names
    )
    source_checksum = hashlib.sha256(source_text.encode()).hexdigest()
    chunk_key = rule_chunk_key(source_key, 0, 0, source_text)
    citation = {
        "source": f"rule-source:{source_key}",
        "source_key": source_key,
        "chunk_key": chunk_key,
        "source_checksum": source_checksum,
        "page_start": 1,
        "page_end": 1,
        "source_excerpt": source_text,
    }
    bound_artifacts = []
    for raw in artifacts:
        artifact = copy.deepcopy(raw)
        artifact["source_citations"] = [copy.deepcopy(citation)]
        bound_artifacts.append(artifact)
    bound_mechanics = []
    for raw in mechanics:
        mechanic = copy.deepcopy(raw)
        mechanic["citations"] = [copy.deepcopy(citation)]
        bound_mechanics.append(mechanic)
    descriptor = {
        "id": package_id,
        "version": version,
        "system_id": "dnd5e",
        "manifest": {
            **package_manifest,
            "system_id": "dnd5e",
            "dependencies": [
                {
                    "id": item["id"],
                    "version": item["version"],
                    "checksum": item["checksum"],
                }
                for item in rule_dependencies
            ],
        },
        "artifacts": bound_artifacts,
        "mechanics": bound_mechanics,
        "sources": [
            {
                "source_key": source_key,
                "title": str(manifest.get("title") or package_id),
                "edition": str(list(manifest.get("editions") or ["2014"])[0]),
                "locale": "en",
                "version": version,
                "publication_id": source_key,
                "authority": "supplement",
                "canonical_source_key": None,
                "checksum": source_checksum,
                "metadata": {},
                "sections": [
                    {
                        "ordinal": 0,
                        "parent_ordinal": None,
                        "level": 1,
                        "title": "Reviewed fixture",
                        "path": ["Reviewed fixture"],
                        "content": source_text,
                        "content_hash": source_checksum,
                        "start_offset": 0,
                        "end_offset": len(source_text),
                        "chunks": [
                            {
                                "key": chunk_key,
                                "ordinal": 0,
                                "heading_path": ["Reviewed fixture"],
                                "content": source_text,
                                "content_hash": source_checksum,
                                "token_count": len(source_text.split()),
                                "metadata": {
                                    "start_offset": 0,
                                    "end_offset": len(source_text),
                                    "page_start": 1,
                                    "page_end": 1,
                                },
                            }
                        ],
                    }
                ],
            }
        ],
        "metadata": {"distribution": "private"},
        "dependencies": rule_dependencies,
    }
    package, blobs = build_rule_content_package(
        package_id=package_id,
        version=version,
        system_id="dnd5e",
        manifest={
            **package_manifest,
            "classification": str(manifest.get("classification") or "third_party"),
            "editions": list(manifest.get("editions") or ["2014"]),
            "activation": {
                "rule_policy": "branch",
                "preset_policy": "none",
                "module_policy": "none",
            },
        },
        rule_descriptors=[descriptor],
        metadata={
            "distribution": "private",
            "license": "user-supplied",
            "attribution": "Test fixture",
        },
        dependencies=dependencies,
    )
    archive_dir = home / "artifacts" / "content-packages"
    archive_dir.mkdir(parents=True, exist_ok=True)
    archive_name = f"{request_key}.sagasmith-pack"
    (archive_dir / archive_name).write_bytes(dumps_content_archive(package, blobs))
    imported = await call(
        server,
        "content_pack",
        {
            "action": "import",
            "payload": {
                "campaign_id": campaign_id,
                "kind": "addon",
                "artifact": archive_name,
            },
            "idempotency_key": f"{request_key}:import",
        },
    )
    activated = await call(
        server,
        "content_pack",
        {
            "action": "activate",
            "payload": {
                "campaign_id": campaign_id,
                "kind": "addon",
                "addon_id": package_id,
                "version": version,
            },
            "expected_revision": expected_revision,
            "idempotency_key": f"{request_key}:activate",
        },
    )
    campaign = await call(
        server,
        "campaign_query",
        {"view": "get", "payload": {"campaign_id": campaign_id}},
    )
    return {
        "package": package,
        "imported": imported,
        "activated": activated,
        "campaign_revision": campaign["revision"],
    }


async def finalize_and_activate_module(
    call: CallTool,
    server: Any,
    campaign_id: str,
    started: dict[str, Any],
    *,
    source_key: str,
    title: str,
    portable_id: str,
    edition: str | None = None,
    request_key: str | None = None,
    progress_remaps: list[dict[str, Any]] | None = None,
    activate: bool = True,
) -> dict[str, Any]:
    """Finalize a reviewed fixture draft and activate its immutable Module Pack."""

    operation_key = request_key or source_key
    chunks = await call(
        server,
        "module_draft",
        {
            "campaign_id": campaign_id,
            "action": "evidence",
            "payload": {
                "job_id": started["job"]["id"],
                "kind": "chunks",
                "limit": 1,
            },
        },
    )
    if not chunks:
        raise ValueError("reviewed module fixture has no source chunk evidence")
    source_ref = {
        "source_key": source_key,
        "page": None,
        "chunk_hash": chunks[0]["content_hash"],
        "note": "Reviewed test fixture source.",
    }
    finalized = await call(
        server,
        "module_draft",
        {
            "campaign_id": campaign_id,
            "action": "finalize",
            "payload": {
                "job_id": started["job"]["id"],
                "pack_id": portable_id,
                "version": "1.0.0",
                "confirmation": {
                    "confirmed": True,
                    "note": "The Agent reviewed this test fixture and confirms finalization.",
                },
                "manifest": {
                    "title": title,
                    "classification": "adventure",
                    "compatibility": {
                        "editions": [edition] if edition else ["2014", "2024"],
                        "required_capabilities": ["module_pack_v2"],
                    },
                    "play_profile": {
                        "party_size": {
                            "minimum": 3,
                            "maximum": 5,
                            "source_refs": [source_ref],
                        },
                        "starting_level": {"value": 1, "source_refs": [source_ref]},
                        "expected_end_level": {"value": 1, "source_refs": [source_ref]},
                        "advancement": {
                            "modes": ["milestone"],
                            "recommended": "milestone",
                            "source_refs": [source_ref],
                        },
                        "pregenerated_characters": {
                            "available": False,
                            "applicability": "Reviewed; none are included.",
                            "source_refs": [source_ref],
                        },
                    },
                    "continuity": {
                        "series_id": None,
                        "order": None,
                        "continues_from": None,
                        "state_policy": {},
                    },
                    "activation": {"mode": "campaign_attach", "default_active": False},
                    "content_summary": {},
                },
            },
            "idempotency_key": f"{operation_key}:finalize",
        },
    )
    activated = None
    if activate:
        imported = await call(
            server,
            "content_pack",
            {
                "action": "import",
                "payload": {
                    "campaign_id": campaign_id,
                    "kind": "module",
                    "artifact": finalized["artifact"],
                },
                "idempotency_key": f"{operation_key}:import",
            },
        )
        campaign = await call(
            server,
            "campaign_query",
            {"view": "get", "payload": {"campaign_id": campaign_id}},
        )
        activated = await call(
            server,
            "content_pack",
            {
                "action": "activate",
                "payload": {
                    "campaign_id": campaign_id,
                    "kind": "module",
                    "module_id": imported["module_id"],
                    **({"progress_remaps": progress_remaps} if progress_remaps else {}),
                },
                "expected_revision": campaign["revision"],
                "idempotency_key": f"{operation_key}:activate",
            },
        )
    return {
        "finalized": finalized,
        "imported": imported if activate else None,
        "activated": activated,
    }
