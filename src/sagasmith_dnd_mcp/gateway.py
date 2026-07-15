"""Principal-aware HTTP/SSE adapter over the authoritative D&D MCP tools."""

from __future__ import annotations

import asyncio
import hmac
import json
import os
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from aiohttp import web

from sagasmith_dnd_mcp.config import McpConfig
from sagasmith_dnd_mcp.server import create_server

JsonHandler = Callable[[web.Request], Awaitable[web.StreamResponse]]


@dataclass(frozen=True)
class GatewayConfig:
    host: str = "127.0.0.1"
    port: int = 8766
    bearer_token: str | None = None
    allowed_origins: tuple[str, ...] = (
        "http://127.0.0.1:4321",
        "http://localhost:4321",
    )

    @classmethod
    def from_environment(cls) -> "GatewayConfig":
        origins = tuple(
            item.strip()
            for item in os.environ.get(
                "SAGASMITH_DND_GATEWAY_ORIGINS",
                "http://127.0.0.1:4321,http://localhost:4321",
            ).split(",")
            if item.strip()
        )
        return cls(
            host=os.environ.get("SAGASMITH_DND_GATEWAY_HOST", "127.0.0.1"),
            port=int(os.environ.get("SAGASMITH_DND_GATEWAY_PORT", "8766")),
            bearer_token=os.environ.get("SAGASMITH_DND_GATEWAY_TOKEN") or None,
            allowed_origins=origins,
        )


class DndGateway:
    """Expose stable UI DTOs while routing every write through an MCP tool."""

    def __init__(self, mcp_config: McpConfig, config: GatewayConfig):
        self.config = config
        self.server = create_server(mcp_config)

    async def call(self, tool_id: str, arguments: dict[str, Any]) -> Any:
        value = await self.server.call_tool(tool_id, arguments)
        structured = value[1] if isinstance(value, tuple) and len(value) > 1 else value
        if isinstance(structured, dict) and set(structured) >= {"action", "result"}:
            return structured["result"]
        return structured

    def principal(self, request: web.Request) -> str:
        return request.headers.get("X-SagaSmith-Principal") or request.query.get(
            "principal_id", "system:local"
        )

    async def campaign_meta(self, campaign_id: str, principal_id: str) -> dict[str, Any]:
        campaign = await self.call(
            "campaign_query",
            {
                "view": "get",
                "payload": {"campaign_id": campaign_id},
                "principal_id": principal_id,
            },
        )
        branches = await self.call(
            "branch_query",
            {
                "campaign_id": campaign_id,
                "view": "list",
                "payload": {},
                "principal_id": principal_id,
            },
        )
        current = next((item for item in branches if item.get("is_current")), None)
        return {
            "schema_version": 1,
            "campaign_revision": campaign.get("revision"),
            "branch_id": current.get("id") if current else None,
            "audience": principal_id,
        }

    async def envelope(
        self, request: web.Request, data: Any, campaign_id: str | None = None
    ) -> web.Response:
        principal_id = self.principal(request)
        meta = (
            await self.campaign_meta(campaign_id, principal_id)
            if campaign_id
            else {"schema_version": 1, "audience": principal_id}
        )
        return web.json_response({"data": data, "meta": meta})

    async def health(self, request: web.Request) -> web.Response:
        capabilities = await self.call("server_capabilities", {})
        return await self.envelope(
            request,
            {
                "status": "ok",
                "version": "0.1.0",
                "dense": os.environ.get("SAGASMITH_DND_MCP_DENSE_ENABLED") == "1",
                "runtime": capabilities.get("server", "sagasmith-dnd-mcp"),
            },
        )

    async def campaigns(self, request: web.Request) -> web.Response:
        result = await self.call(
            "campaign_query",
            {
                "view": "list",
                "payload": {"status": request.query.get("status")},
                "principal_id": self.principal(request),
            },
        )
        return await self.envelope(request, result)

    async def campaign(self, request: web.Request) -> web.Response:
        campaign_id = request.match_info["campaign_id"]
        result = await self.call(
            "campaign_query",
            {
                "view": "get",
                "payload": {"campaign_id": campaign_id},
                "principal_id": self.principal(request),
            },
        )
        return await self.envelope(request, result, campaign_id)

    async def characters(self, request: web.Request) -> web.Response:
        campaign_id = request.match_info["campaign_id"]
        result = await self.call(
            "character_query",
            {
                "view": "list",
                "payload": {"campaign_id": campaign_id},
                "principal_id": self.principal(request),
            },
        )
        return await self.envelope(request, result, campaign_id)

    async def character(self, request: web.Request) -> web.Response:
        result = await self.call(
            "character_query",
            {
                "view": "get",
                "payload": {"character_id": request.match_info["character_id"]},
                "principal_id": self.principal(request),
            },
        )
        campaign_id = result.get("campaign_id") if isinstance(result, dict) else None
        return await self.envelope(request, result, campaign_id)

    async def module_view(self, request: web.Request, view: str) -> web.Response:
        campaign_id = request.match_info["campaign_id"]
        payload: dict[str, Any] = {}
        if request.query.get("module_id"):
            payload["module_id"] = request.query["module_id"]
        if view in {"current", "progress"}:
            payload["scope_id"] = request.query.get("scope", "party")
        result = await self.call(
            "module_query",
            {
                "campaign_id": campaign_id,
                "view": view,
                "payload": payload,
                "principal_id": self.principal(request),
            },
        )
        return await self.envelope(request, result, campaign_id)

    async def snapshots(self, request: web.Request, view: str) -> web.Response:
        campaign_id = request.match_info["campaign_id"]
        result = await self.call(
            "snapshot_query",
            {
                "campaign_id": campaign_id,
                "view": view,
                "payload": {},
                "principal_id": self.principal(request),
            },
        )
        return await self.envelope(request, result, campaign_id)

    async def events(self, request: web.Request) -> web.Response:
        campaign_id = request.match_info["campaign_id"]
        result = await self.call(
            "campaign_event",
            {
                "campaign_id": campaign_id,
                "action": "list",
                "payload": {"limit": min(int(request.query.get("limit", "50")), 200)},
                "principal_id": self.principal(request),
            },
        )
        return await self.envelope(request, result, campaign_id)

    async def module_search(self, request: web.Request) -> web.Response:
        campaign_id = request.match_info["campaign_id"]
        result = await self.call(
            "module_search",
            {
                "campaign_id": campaign_id,
                "query": request.query.get("query", ""),
                "top_k": min(int(request.query.get("limit", "8")), 50),
                "principal_id": self.principal(request),
            },
        )
        return await self.envelope(request, result, campaign_id)

    async def rule_sources(self, request: web.Request) -> web.Response:
        result = await self.call(
            "rule_pack_query",
            {
                "view": "sources",
                "payload": {
                    "system_id": request.query.get("system_id", "dnd5e"),
                    "edition": request.query.get("edition"),
                },
                "principal_id": self.principal(request),
            },
        )
        return await self.envelope(request, result)

    async def rule_search(self, request: web.Request) -> web.Response:
        result = await self.call(
            "rule_search",
            {
                "query": request.query.get("query", ""),
                "edition": request.query.get("edition"),
                "locale": request.query.get("locale"),
                "top_k": min(int(request.query.get("limit", "8")), 50),
            },
        )
        return await self.envelope(request, result)

    async def combat(self, request: web.Request) -> web.Response:
        campaign_id = request.match_info["campaign_id"]
        principal_id = self.principal(request)
        result = await self.call(
            "combat_query",
            {
                "campaign_id": campaign_id,
                "view": "status",
                "principal_id": principal_id,
            },
        )
        if isinstance(result, dict):
            meta = await self.campaign_meta(campaign_id, principal_id)
            result = {
                **result,
                "campaign_revision": meta.get("campaign_revision"),
                "branch_id": meta.get("branch_id"),
            }
        return await self.envelope(request, result, campaign_id)

    async def combat_move(self, request: web.Request) -> web.Response:
        campaign_id = request.match_info["campaign_id"]
        principal_id = self.principal(request)
        body = await request.json()
        await self.call(
            "combat_movement",
            {
                "campaign_id": campaign_id,
                "actor_id": body["actor_id"],
                "action": "move",
                "payload": {
                    "distance": body["distance"],
                    "destination": body["destination"],
                    "path": body.get("path"),
                    "movement_mode": body.get("movement_mode", "voluntary"),
                },
                "principal_id": principal_id,
                "expected_revision": body["expected_revision"],
                "branch_id": body.get("branch_id"),
                "idempotency_key": body["idempotency_key"],
            },
        )
        return await self.combat(request)

    async def stream(self, request: web.Request) -> web.StreamResponse:
        campaign_id = request.match_info["campaign_id"]
        principal_id = self.principal(request)
        response = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
            },
        )
        await response.prepare(request)
        last_revision: int | None = None
        try:
            while True:
                meta = await self.campaign_meta(campaign_id, principal_id)
                revision = int(meta.get("campaign_revision") or 0)
                if revision != last_revision:
                    payload = json.dumps(meta, ensure_ascii=False, separators=(",", ":"))
                    await response.write(f"event: revision\ndata: {payload}\n\n".encode())
                    last_revision = revision
                await asyncio.sleep(0.75)
        except (asyncio.CancelledError, ConnectionError, RuntimeError):
            pass
        return response


GATEWAY_KEY = web.AppKey("gateway", DndGateway)


def create_app(
    mcp_config: McpConfig | None = None,
    gateway_config: GatewayConfig | None = None,
) -> web.Application:
    config = gateway_config or GatewayConfig.from_environment()
    gateway = DndGateway(mcp_config or McpConfig.from_environment(), config)

    @web.middleware
    async def boundary(request: web.Request, handler: JsonHandler) -> web.StreamResponse:
        origin = request.headers.get("Origin")
        if origin and origin not in config.allowed_origins:
            raise web.HTTPForbidden(text="origin is not allowed")
        if config.bearer_token:
            supplied = request.headers.get("Authorization", "").removeprefix("Bearer ")
            supplied = supplied or request.query.get("token", "")
            if not hmac.compare_digest(supplied, config.bearer_token):
                raise web.HTTPUnauthorized(text="invalid gateway token")
        elif request.remote not in {"127.0.0.1", "::1", None}:
            raise web.HTTPForbidden(text="a bearer token is required for non-loopback access")
        if request.method == "OPTIONS":
            response: web.StreamResponse = web.Response(status=204)
        else:
            try:
                response = await handler(request)
            except web.HTTPException:
                raise
            except (KeyError, TypeError, ValueError, PermissionError) as exc:
                response = web.json_response({"error": str(exc)}, status=400)
            except Exception as exc:  # MCP errors remain typed only inside the trusted process.
                response = web.json_response({"error": str(exc)}, status=409)
        if origin and origin in config.allowed_origins:
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Vary"] = "Origin"
            response.headers["Access-Control-Allow-Headers"] = (
                "Authorization, Content-Type, X-SagaSmith-Principal"
            )
            response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        return response

    async def options(_: web.Request) -> web.Response:
        return web.Response(status=204)

    async def modules(request: web.Request) -> web.Response:
        return await gateway.module_view(request, "list")

    async def scenes(request: web.Request) -> web.Response:
        return await gateway.module_view(request, "index")

    async def current_scene(request: web.Request) -> web.Response:
        return await gateway.module_view(request, "current")

    async def scene_progress(request: web.Request) -> web.Response:
        return await gateway.module_view(request, "progress")

    async def saves(request: web.Request) -> web.Response:
        return await gateway.snapshots(request, "list")

    async def lineage(request: web.Request) -> web.Response:
        return await gateway.snapshots(request, "lineage")

    app = web.Application(middlewares=[boundary])
    app[GATEWAY_KEY] = gateway
    app.router.add_route("OPTIONS", "/{tail:.*}", options)
    app.router.add_get("/api/health", gateway.health)
    app.router.add_get("/api/campaigns", gateway.campaigns)
    app.router.add_get("/api/campaigns/{campaign_id}", gateway.campaign)
    app.router.add_get("/api/campaigns/{campaign_id}/characters", gateway.characters)
    app.router.add_get("/api/characters/{character_id}", gateway.character)
    app.router.add_get(
        "/api/campaigns/{campaign_id}/modules",
        modules,
    )
    app.router.add_get(
        "/api/campaigns/{campaign_id}/scenes",
        scenes,
    )
    app.router.add_get(
        "/api/campaigns/{campaign_id}/current-scene",
        current_scene,
    )
    app.router.add_get(
        "/api/campaigns/{campaign_id}/scene-progress",
        scene_progress,
    )
    app.router.add_get(
        "/api/campaigns/{campaign_id}/saves",
        saves,
    )
    app.router.add_get(
        "/api/campaigns/{campaign_id}/lineage",
        lineage,
    )
    app.router.add_get("/api/campaigns/{campaign_id}/events", gateway.events)
    app.router.add_get("/api/campaigns/{campaign_id}/search", gateway.module_search)
    app.router.add_get("/api/campaigns/{campaign_id}/combat", gateway.combat)
    app.router.add_post("/api/campaigns/{campaign_id}/combat/move", gateway.combat_move)
    app.router.add_get("/api/campaigns/{campaign_id}/stream", gateway.stream)
    app.router.add_get("/api/rules", gateway.rule_sources)
    app.router.add_get("/api/rules/search", gateway.rule_search)
    return app


def main() -> None:
    config = GatewayConfig.from_environment()
    web.run_app(create_app(gateway_config=config), host=config.host, port=config.port)


if __name__ == "__main__":
    main()
