from collections.abc import Awaitable, Callable
from typing import Any

CallTool = Callable[[Any, str, dict[str, Any]], Awaitable[Any]]


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
                "portable_id": portable_id,
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
                    "artifact": finalized["artifact"],
                    **({"progress_remaps": progress_remaps} if progress_remaps else {}),
                },
                "expected_revision": campaign["revision"],
                "idempotency_key": f"{operation_key}:activate",
            },
        )
    return {"finalized": finalized, "activated": activated}
