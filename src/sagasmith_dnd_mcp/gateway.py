"""Principal-aware HTTP/SSE adapter over the authoritative D&D MCP tools."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import tempfile
import time
from contextvars import ContextVar
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from pathlib import Path
from typing import Any, Awaitable, Callable

from aiohttp import web
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import CallToolResult, ImageContent, ToolListChangedNotification
from sagasmith_core.access import LOCAL_SYSTEM_PRINCIPAL_ID
from sagasmith_core.idempotency import IdempotencyConflictError

from sagasmith_dnd_mcp.config import McpConfig
from sagasmith_dnd_mcp.tool_profiles import CORE_TOOLS

JsonHandler = Callable[[web.Request], Awaitable[web.StreamResponse]]
LOGGER = logging.getLogger(__name__)
CONTENT_PACK_KINDS = ("core_rules", "addon", "module", "preset")
COOKIE_NAME = "sagasmith_dnd_session"
_REQUEST_CLIENT: ContextVar[DndMcpClient | None]


class McpToolRejectedError(ValueError):
    """A connected MCP server rejected a well-formed tool request."""


@dataclass(frozen=True)
class GatewayConfig:
    host: str = "127.0.0.1"
    port: int = 8766
    bearer_token: str | None = None
    principal_id: str = LOCAL_SYSTEM_PRINCIPAL_ID
    upload_limit_bytes: int = 64 * 1024 * 1024
    mcp_url: str = "http://127.0.0.1:8767/mcp"
    agent_webui_url: str = "http://127.0.0.1:8765/"
    ui_dist: Path | None = None
    session_ttl_seconds: int = 12 * 60 * 60
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
            principal_id=os.environ.get(
                "SAGASMITH_DND_GATEWAY_PRINCIPAL_ID", LOCAL_SYSTEM_PRINCIPAL_ID
            ),
            upload_limit_bytes=int(
                os.environ.get("SAGASMITH_DND_GATEWAY_UPLOAD_LIMIT", str(64 * 1024 * 1024))
            ),
            mcp_url=os.environ.get(
                "SAGASMITH_DND_MCP_URL", "http://127.0.0.1:8767/mcp"
            ),
            agent_webui_url=os.environ.get(
                "SAGASMITH_AGENT_WEBUI_URL", "http://127.0.0.1:8765/"
            ),
            ui_dist=(
                Path(value).expanduser().resolve()
                if (value := os.environ.get("SAGASMITH_DND_UI_DIST", "")).strip()
                else None
            ),
            session_ttl_seconds=int(
                os.environ.get("SAGASMITH_DND_GATEWAY_SESSION_TTL", str(12 * 60 * 60))
            ),
            allowed_origins=origins,
        )


@dataclass
class _McpRequest:
    tool_id: str
    arguments: dict[str, Any]
    future: asyncio.Future[CallToolResult]
    attempts: int = 0


@dataclass
class DndMcpClient:
    """Own one real MCP session from one long-lived asyncio task."""

    url: str
    startup_timeout: float = 15.0
    _queue: asyncio.Queue[_McpRequest | None] = dataclass_field(
        init=False, default_factory=asyncio.Queue
    )
    _ready: asyncio.Event = dataclass_field(init=False, default_factory=asyncio.Event)
    _runner_task: asyncio.Task[None] | None = dataclass_field(init=False, default=None)
    _startup_error: BaseException | None = dataclass_field(init=False, default=None)

    async def start(self) -> None:
        if self._runner_task is not None:
            return
        self._runner_task = asyncio.create_task(self._run(), name="sagasmith-dnd-gateway-mcp")
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=self.startup_timeout)
        except TimeoutError:
            await self.stop()
            raise RuntimeError(f"D&D MCP did not become ready at {self.url}") from None
        if self._startup_error is not None:
            error = self._startup_error
            await self.stop()
            raise RuntimeError(f"D&D MCP connection failed at {self.url}") from error

    async def stop(self) -> None:
        task = self._runner_task
        if task is None:
            return
        await self._queue.put(None)
        try:
            await asyncio.wait_for(task, timeout=5)
        except TimeoutError:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        finally:
            self._runner_task = None

    async def call_tool(self, tool_id: str, arguments: dict[str, Any]) -> CallToolResult:
        if self._runner_task is None:
            raise RuntimeError("D&D MCP client is not started")
        future = asyncio.get_running_loop().create_future()
        await self._queue.put(_McpRequest(tool_id, dict(arguments), future))
        return await future

    async def _run(self) -> None:
        pending: _McpRequest | None = None
        first_attempt = True
        while True:
            try:
                async with streamable_http_client(self.url) as streams:
                    read_stream, write_stream, _ = streams
                    refresh_generation = 0
                    synced_generation = 0
                    call_lock = asyncio.Lock()

                    async def handle_server_message(message: Any) -> None:
                        nonlocal refresh_generation
                        if isinstance(
                            getattr(message, "root", None),
                            ToolListChangedNotification,
                        ):
                            refresh_generation += 1
                        await asyncio.sleep(0)

                    async def refresh_changed_tools(session: ClientSession) -> None:
                        nonlocal synced_generation
                        while synced_generation < refresh_generation:
                            target = refresh_generation

                            async def refresh() -> None:
                                await session.list_tools()

                            await asyncio.shield(
                                asyncio.create_task(
                                    refresh(),
                                    name="sagasmith-dnd-gateway-tools-refresh",
                                )
                            )
                            synced_generation = target

                    async def force_refresh_tools(session: ClientSession) -> None:
                        nonlocal synced_generation

                        async def refresh() -> None:
                            await asyncio.sleep(0)
                            await session.list_tools()

                        await asyncio.shield(
                            asyncio.create_task(
                                refresh(),
                                name="sagasmith-dnd-gateway-tools-force-refresh",
                            )
                        )
                        synced_generation = refresh_generation

                    async with ClientSession(
                        read_stream,
                        write_stream,
                        message_handler=handle_server_message,
                    ) as session:
                        await session.initialize()
                        await session.list_tools()
                        if first_attempt:
                            self._ready.set()
                            first_attempt = False
                        while True:
                            request = pending or await self._queue.get()
                            pending = None
                            if request is None:
                                return
                            try:
                                async with call_lock:
                                    result = await self._call_in_session(
                                        session,
                                        request.tool_id,
                                        request.arguments,
                                        force_refresh_tools,
                                    )
                                    await refresh_changed_tools(session)
                            except McpToolRejectedError as exc:
                                if not request.future.done():
                                    request.future.set_exception(exc)
                            except Exception as exc:
                                if request.attempts == 0:
                                    request.attempts += 1
                                    pending = request
                                    break
                                if not request.future.done():
                                    request.future.set_exception(exc)
                            else:
                                if not request.future.done():
                                    request.future.set_result(result)
            except asyncio.CancelledError:
                if pending is not None and not pending.future.done():
                    pending.future.cancel()
                raise
            except Exception as exc:
                if first_attempt:
                    self._startup_error = exc
                    self._ready.set()
                    return
                if pending is not None and pending.attempts >= 1:
                    if not pending.future.done():
                        pending.future.set_exception(exc)
                    pending = None
                await asyncio.sleep(0.25)

    @staticmethod
    def _principal(arguments: dict[str, Any]) -> str:
        return str(
            arguments.get("by_principal_id")
            or arguments.get("principal_id")
            or LOCAL_SYSTEM_PRINCIPAL_ID
        )

    async def _call_in_session(
        self,
        session: ClientSession,
        tool_id: str,
        arguments: dict[str, Any],
        refresh_changed_tools: Callable[[ClientSession], Awaitable[None]],
    ) -> CallToolResult:
        dynamic_tool = tool_id not in CORE_TOOLS
        if dynamic_tool:
            payload = arguments.get("payload")
            payload_campaign = (
                payload.get("campaign_id") if isinstance(payload, dict) else None
            )
            campaign_id = str(
                arguments.get("campaign_id") or payload_campaign or ""
            ).strip() or None
            principal_id = self._principal(arguments)
            try:
                status = await session.call_tool(
                    "exposure",
                    {"action": "get", "principal_id": principal_id},
                )
                current = dict(status.structuredContent or {})
                current = dict(current.get("result") or current)
            except Exception:
                current = {}
            if (
                current.get("campaign_id") != campaign_id
                or current.get("principal_id") != principal_id
            ):
                opened = await session.call_tool(
                    "exposure",
                    {
                        "action": "open",
                        "campaign_id": campaign_id,
                        "principal_id": principal_id,
                    },
                )
                self._raise_tool_error(opened)
                current = dict(opened.structuredContent or {})
                current = dict(current.get("result") or current)
            if tool_id not in set(current.get("loaded_tools") or []):
                loaded = await session.call_tool(
                    "exposure",
                    {
                        "action": "set",
                        "campaign_id": campaign_id,
                        "add_tool_ids": [tool_id],
                        "principal_id": principal_id,
                    },
                )
                self._raise_tool_error(loaded)
                await refresh_changed_tools(session)
                loaded_value = dict(loaded.structuredContent or {})
                loaded_value = dict(loaded_value.get("result") or loaded_value)
                if tool_id not in set(loaded_value.get("loaded_tools") or []):
                    raise RuntimeError(
                        f"D&D MCP did not load {tool_id!r} into the active exposure"
                    )
        result = await session.call_tool(tool_id, arguments)
        self._raise_tool_error(result)
        return result

    @staticmethod
    def _raise_tool_error(result: CallToolResult) -> None:
        if not result.isError:
            return
        message = next(
            (
                str(getattr(item, "text", "")).strip()
                for item in result.content
                if str(getattr(item, "text", "")).strip()
            ),
            "D&D MCP rejected the request",
        )
        raise McpToolRejectedError(message[:2000])


_REQUEST_CLIENT = ContextVar("sagasmith_dnd_gateway_client", default=None)


@dataclass
class _BrowserSession:
    client: DndMcpClient
    touched_at: float
    campaign_id: str | None = None


class DndClientPool:
    """Keep dynamic MCP exposure isolated to one browser and campaign."""

    def __init__(self, config: GatewayConfig):
        self.config = config
        self.sessions: dict[str, _BrowserSession] = {}
        self._lock = asyncio.Lock()

    async def session(
        self,
        token: str | None,
        campaign_id: str | None,
    ) -> tuple[str, DndMcpClient, bool]:
        async with self._lock:
            now = time.monotonic()
            expired = [
                key
                for key, value in self.sessions.items()
                if now - value.touched_at > self.config.session_ttl_seconds
            ]
            for key in expired:
                stale = self.sessions.pop(key)
                await stale.client.stop()
            if token and token in self.sessions:
                current = self.sessions[token]
                if (
                    current.campaign_id is None
                    or campaign_id is None
                    or current.campaign_id == campaign_id
                ):
                    current.touched_at = now
                    current.campaign_id = current.campaign_id or campaign_id
                    return token, current.client, False
                await current.client.stop()
                client = DndMcpClient(self.config.mcp_url)
                await client.start()
                self.sessions[token] = _BrowserSession(client, now, campaign_id)
                return token, client, False
            token = secrets.token_urlsafe(32)
            client = DndMcpClient(self.config.mcp_url)
            await client.start()
            self.sessions[token] = _BrowserSession(client, now, campaign_id)
            return token, client, True

    async def close(self) -> None:
        for current in list(self.sessions.values()):
            await current.client.stop()
        self.sessions.clear()


class DndGateway:
    """Expose stable UI DTOs while routing every write through an MCP tool."""

    def __init__(
        self,
        config: GatewayConfig,
        client: DndMcpClient | None,
        mcp_config: McpConfig,
    ):
        self.config = config
        self._default_client = client
        self.mcp_config = mcp_config
        self.mcp_config.prepare()

    @property
    def client(self) -> DndMcpClient:
        client = _REQUEST_CLIENT.get() or self._default_client
        if client is None:
            raise RuntimeError("D&D MCP request session is not bound")
        return client

    @client.setter
    def client(self, value: DndMcpClient) -> None:
        """Allow focused tests to replace the injected client explicitly."""

        self._default_client = value

    @staticmethod
    def pack_summary(kind: str, value: dict[str, Any]) -> dict[str, Any]:
        """Project the four storage models into one stable UI summary."""

        manifest = dict(value.get("manifest") or {})
        metadata = dict(value.get("metadata") or {})
        package_ref = (
            dict(metadata.get("content_package") or {})
            if kind == "module"
            else {}
        )
        activation = value.get("activation")
        if kind == "addon":
            identifier = str(value.get("addon_id") or manifest.get("id") or "")
        elif kind == "module" and package_ref:
            identifier = str(package_ref.get("id") or value.get("id") or "")
        else:
            identifier = str(
                value.get("pack_id")
                or value.get("id")
                or value.get("module_id")
                or manifest.get("id")
                or ""
            )
        title = str(
            value.get("title")
            or manifest.get("title")
            or metadata.get("title")
            or identifier
        )
        status = str(value.get("status") or ("active" if value.get("active") else "stored"))
        return {
            "kind": kind,
            "id": identifier,
            "local_ref": str(
                value.get("local_ref")
                or value.get("id")
                or value.get("module_id")
                or identifier
            ),
            "version": str(
                package_ref.get("version")
                or value.get("version")
                or manifest.get("version")
                or ""
            ),
            "checksum": str(package_ref.get("checksum") or value.get("checksum") or ""),
            "title": title,
            "status": status,
            "active": bool(
                value.get("active")
                or (isinstance(activation, dict) and activation.get("enabled"))
            ),
            "editions": list(manifest.get("editions") or metadata.get("editions") or []),
            "classification": manifest.get("classification") or metadata.get("classification"),
            "license": metadata.get("license") or value.get("license"),
            "dependencies": list(manifest.get("dependencies") or []),
            "component_counts": {
                key: value[key]
                for key in ("chapters", "scenes", "chunks")
                if isinstance(value.get(key), int)
            },
            "warnings": list(value.get("warnings") or []),
            "activation": activation if isinstance(activation, dict) else None,
        }

    async def campaign_record(self, campaign_id: str, principal_id: str) -> dict[str, Any]:
        return await self.call(
            "campaign_query",
            {
                "view": "get",
                "payload": {"campaign_id": campaign_id},
                "principal_id": principal_id,
            },
        )

    async def call(self, tool_id: str, arguments: dict[str, Any]) -> Any:
        value = await self.client.call_tool(tool_id, arguments)
        structured = value.structuredContent
        if isinstance(structured, dict) and set(structured) >= {"action", "result"}:
            return structured["result"]
        if isinstance(structured, dict) and set(structured) >= {"result"}:
            return structured["result"]
        return structured

    def principal(self, request: web.Request) -> str:
        del request
        return self.config.principal_id

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
        campaign_id = request.match_info["campaign_id"]
        result = await self.call(
            "character_query",
            {
                "view": "get",
                "payload": {
                    "campaign_id": campaign_id,
                    "character_id": request.match_info["character_id"],
                },
                "principal_id": self.principal(request),
            },
        )
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
        campaign_id = request.query.get("campaign_id", "")
        if not campaign_id:
            raise web.HTTPBadRequest(text="campaign_id is required")
        principal_id = self.principal(request)
        core_result = await self.call(
            "content_pack",
            {
                "action": "list",
                "payload": {
                    "kind": "core_rules",
                    "campaign_id": campaign_id,
                    "pack_id": request.query.get("pack_id"),
                },
                "principal_id": principal_id,
            },
        )
        addon_result = await self.call(
            "content_pack",
            {
                "action": "list",
                "payload": {
                    "kind": "addon",
                    "campaign_id": campaign_id,
                    "addon_id": request.query.get("pack_id"),
                },
                "principal_id": principal_id,
            },
        )
        projected = []
        values = [
            ("core_rules", item)
            for item in core_result if isinstance(core_result, list)
        ] + [
            ("addon", item)
            for item in addon_result if isinstance(addon_result, list)
        ]
        for kind, item in values:
            manifest = dict(item.get("manifest") or {})
            editions = list(manifest.get("editions") or [])
            identifier = str(
                item.get("pack_id")
                or item.get("addon_id")
                or manifest.get("id")
                or ""
            )
            projected.append(
                {
                    "id": identifier,
                    "source_key": identifier,
                    "title": str(
                        item.get("title")
                        or manifest.get("title")
                        or identifier
                        or manifest.get("id")
                        or "Untitled rule Pack"
                    ),
                    "edition": str(editions[0] if editions else "all"),
                    "locale": str(manifest.get("locale") or "en"),
                    "version": str(item.get("version") or ""),
                    "authority": str(
                        manifest.get("classification")
                        or ("addon" if kind == "addon" else "content_pack")
                    ),
                    "status": str(
                        "active"
                        if isinstance(item.get("activation"), dict)
                        and item["activation"].get("enabled")
                        else item.get("status") or "stored"
                    ),
                    "checksum": str(item.get("checksum") or ""),
                }
            )
        return await self.envelope(request, projected, campaign_id)

    async def rule_search(self, request: web.Request) -> web.Response:
        campaign_id = request.query.get("campaign_id", "")
        if not campaign_id:
            raise web.HTTPBadRequest(text="campaign_id is required")
        filters = {
            key: value
            for key in ("edition", "locale")
            if (value := request.query.get(key))
        }
        result = await self.call(
            "rule_search",
            {
                "campaign_id": campaign_id,
                "query": request.query.get("query", ""),
                "filters": filters,
                "top_k": min(int(request.query.get("limit", "8")), 50),
                "principal_id": self.principal(request),
            },
        )
        return await self.envelope(request, result, campaign_id)

    async def content_packs(self, request: web.Request) -> web.Response:
        campaign_id = request.match_info["campaign_id"]
        principal_id = self.principal(request)
        campaign = await self.campaign_record(campaign_id, principal_id)
        requested_kind = request.query.get("kind")
        if requested_kind and requested_kind not in CONTENT_PACK_KINDS:
            raise web.HTTPBadRequest(text="kind must be core_rules, addon, module, or preset")
        kinds = (requested_kind,) if requested_kind else CONTENT_PACK_KINDS
        collections: dict[str, Any] = {}
        summaries: list[dict[str, Any]] = []
        for kind in kinds:
            payload: dict[str, Any] = {"campaign_id": campaign_id, "kind": kind}
            if kind == "preset":
                payload["edition"] = str(campaign.get("edition") or "2024")
            raw = await self.call(
                "content_pack",
                {
                    "action": "list",
                    "payload": payload,
                    "principal_id": principal_id,
                },
            )
            collections[kind] = raw
            if kind == "preset":
                for item in raw if isinstance(raw, list) else []:
                    if isinstance(item, dict):
                        summaries.append(self.pack_summary(kind, item))
            else:
                for item in raw if isinstance(raw, list) else []:
                    if isinstance(item, dict):
                        summaries.append(self.pack_summary(kind, item))
        rule_context = await self.call(
            "campaign_rules",
            {
                "campaign_id": campaign_id,
                "action": "explain",
                "payload": {},
                "principal_id": principal_id,
            },
        )
        active_rule_versions = {
            (
                str(dict(rule_context.get("core_pack") or {}).get("id") or ""),
                str(dict(rule_context.get("core_pack") or {}).get("version") or ""),
            ),
            *{
                (str(item.get("pack_id") or ""), str(item.get("version") or ""))
                for item in rule_context.get("lock") or []
                if isinstance(item, dict)
            },
        }
        summaries = [
            {
                **item,
                "active": (
                    (item["local_ref"], item["version"]) in active_rule_versions
                    if item["kind"] == "core_rules"
                    else item["active"]
                ),
            }
            for item in summaries
        ]
        return await self.envelope(
            request,
            {
                "campaign": {
                    "id": campaign_id,
                    "edition": campaign.get("edition"),
                    "phase": dict(campaign.get("state") or {}).get("game_phase", "lobby"),
                },
                "packs": summaries,
                "collections": collections,
            },
            campaign_id,
        )

    async def content_pack_detail(self, request: web.Request) -> web.Response:
        campaign_id = request.match_info["campaign_id"]
        principal_id = self.principal(request)
        kind = request.query.get("kind", "")
        if kind not in CONTENT_PACK_KINDS:
            raise web.HTTPBadRequest(text="kind must be core_rules, addon, module, or preset")
        payload: dict[str, Any] = {"campaign_id": campaign_id, "kind": kind}
        if kind == "core_rules":
            payload.update(
                pack_id=request.query.get("local_ref") or request.query.get("pack_id"),
                version=request.query.get("version"),
                include_package=True,
            )
        elif kind == "addon":
            payload.update(
                addon_id=request.query.get("pack_id"),
                version=request.query.get("version"),
                include_package=True,
            )
        elif kind == "module":
            payload["module_id"] = request.query.get("local_ref") or request.query.get("pack_id")
            payload["include_package"] = True
        else:
            campaign = await self.campaign_record(campaign_id, principal_id)
            payload.update(
                edition=request.query.get("edition") or campaign.get("edition") or "2024",
                pack_id=request.query.get("pack_id"),
                version=request.query.get("version"),
                artifact_id=request.query.get("artifact_id"),
                include_package=True,
            )
        result = await self.call(
            "content_pack",
            {
                "action": "get",
                "payload": payload,
                "principal_id": principal_id,
            },
        )
        return await self.envelope(request, result, campaign_id)

    async def content_pack_import(self, request: web.Request) -> web.Response:
        campaign_id = request.match_info["campaign_id"]
        reader = await request.multipart()
        fields: dict[str, str] = {}
        temporary_path: Path | None = None
        archive_name = ""
        archive_size = 0
        archive_hash = hashlib.sha256()
        try:
            while part := await reader.next():
                if part.name != "archive":
                    fields[str(part.name)] = await part.text()
                    continue
                archive_name = str(part.filename or "")
                if not archive_name.casefold().endswith(".sagasmith-pack"):
                    raise web.HTTPBadRequest(text="archive must be a .sagasmith-pack file")
                with tempfile.NamedTemporaryFile(
                    mode="wb",
                    prefix="gateway-upload-",
                    suffix=".sagasmith-pack",
                    dir=self.mcp_config.content_packages_dir,
                    delete=False,
                ) as target:
                    temporary_path = Path(target.name)
                    while chunk := await part.read_chunk(size=64 * 1024):
                        archive_size += len(chunk)
                        if archive_size > self.config.upload_limit_bytes:
                            raise web.HTTPRequestEntityTooLarge(
                                max_size=self.config.upload_limit_bytes,
                                actual_size=archive_size,
                            )
                        archive_hash.update(chunk)
                        target.write(chunk)
            kind = fields.get("kind", "")
            if kind not in CONTENT_PACK_KINDS:
                raise web.HTTPBadRequest(
                    text="kind must be core_rules, addon, module, or preset"
                )
            if temporary_path is None or not archive_size:
                raise web.HTTPBadRequest(text="archive is required")
            idempotency_key = fields.get("idempotency_key", "").strip()
            if not idempotency_key:
                raise web.HTTPBadRequest(text="idempotency_key is required")
            payload: dict[str, Any] = {
                "campaign_id": campaign_id,
                "kind": kind,
                "artifact": temporary_path.name,
            }
            if fields.get("progress_remaps"):
                payload["progress_remaps"] = json.loads(fields["progress_remaps"])
            result = await self.call(
                "content_pack",
                {
                    "action": "import",
                    "payload": payload,
                    "principal_id": self.principal(request),
                    "idempotency_key": idempotency_key,
                },
            )
            return await self.envelope(
                request,
                {
                    "archive": {
                        "name": archive_name,
                        "size": archive_size,
                        "sha256": archive_hash.hexdigest(),
                    },
                    "import": result,
                },
                campaign_id,
            )
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    async def content_pack_action(self, request: web.Request) -> web.Response:
        campaign_id = request.match_info["campaign_id"]
        body = await request.json()
        kind = str(body.get("kind") or "")
        action = str(body.get("action") or "")
        if kind not in CONTENT_PACK_KINDS:
            raise web.HTTPBadRequest(text="invalid content Pack kind")
        if action not in {"activate", "deactivate", "remove", "export"}:
            raise web.HTTPBadRequest(text="invalid content Pack action")
        payload = {
            key: value
            for key, value in body.items()
            if key
            in {
                "pack_id",
                "addon_id",
                "module_id",
                "version",
                "enabled",
                "options",
                "branch_id",
                "progress_remaps",
                "metadata",
                "manifest",
                "dependencies",
                "catalogs",
                "narrative",
                "include_package",
            }
        }
        payload.update({"campaign_id": campaign_id, "kind": kind})
        if kind == "preset" and action == "export":
            campaign = await self.campaign_record(campaign_id, self.principal(request))
            payload["edition"] = str(campaign.get("edition") or "2024")
        result = await self.call(
            "content_pack",
            {
                "action": action,
                "payload": payload,
                "principal_id": self.principal(request),
                "expected_revision": body.get("expected_revision"),
                "idempotency_key": body.get("idempotency_key"),
            },
        )
        return await self.envelope(request, result, campaign_id)

    async def content_pack_artifact(self, request: web.Request) -> web.StreamResponse:
        campaign_id = request.match_info["campaign_id"]
        artifact = request.match_info["artifact"]
        kind = request.query.get("kind", "")
        if kind not in CONTENT_PACK_KINDS or Path(artifact).name != artifact:
            raise web.HTTPBadRequest(text="invalid content Pack artifact request")
        await self.call(
            "content_pack",
            {
                "action": "get",
                "payload": {
                    "campaign_id": campaign_id,
                    "kind": kind,
                    "artifact": artifact,
                },
                "principal_id": self.principal(request),
            },
        )
        target = (self.mcp_config.content_packages_dir / artifact).resolve()
        if target.parent != self.mcp_config.content_packages_dir.resolve() or not target.is_file():
            raise web.HTTPNotFound(text="content Pack artifact is unavailable")
        return web.FileResponse(
            target,
            headers={
                "Cache-Control": "private, no-store",
                "Content-Disposition": f'attachment; filename="{artifact}"',
            },
        )

    async def actor_from_preset(self, request: web.Request) -> web.Response:
        campaign_id = request.match_info["campaign_id"]
        body = await request.json()
        required_fields = ("pack_id", "version", "artifact_id")
        if not all(str(body.get(field) or "").strip() for field in required_fields):
            raise ValueError("pack_id, version, and artifact_id are required")
        principal_id = self.principal(request)
        campaign = await self.campaign_record(campaign_id, principal_id)
        preset = await self.call(
            "content_pack",
            {
                "action": "get",
                "payload": {
                    "campaign_id": campaign_id,
                    "kind": "preset",
                    "edition": str(campaign.get("edition") or "2024"),
                    "pack_id": body.get("pack_id"),
                    "version": body.get("version"),
                },
                "principal_id": principal_id,
            },
        )
        artifact = str(dict(preset.get("artifact") or {}).get("artifact") or "")
        if not artifact:
            raise LookupError("installed preset has no finalized archive")
        payload = {
            "campaign_id": campaign_id,
            "artifact_id": body.get("artifact_id"),
            "artifact": artifact,
        }
        for field in ("name", "player_name"):
            if body.get(field) is not None:
                payload[field] = body[field]
        result = await self.call(
            "character_create_from",
            {
                "mode": "content_actor",
                "payload": payload,
                "principal_id": principal_id,
                "idempotency_key": body.get("idempotency_key"),
            },
        )
        return await self.envelope(request, result, campaign_id)

    async def rule_context(self, request: web.Request) -> web.Response:
        campaign_id = request.match_info["campaign_id"]
        result = await self.call(
            "campaign_rules",
            {
                "campaign_id": campaign_id,
                "action": "explain",
                "payload": {},
                "principal_id": self.principal(request),
                "branch_id": request.query.get("branch_id"),
            },
        )
        return await self.envelope(request, result, campaign_id)

    async def drafts(self, request: web.Request) -> web.Response:
        campaign_id = request.match_info["campaign_id"]
        principal_id = self.principal(request)
        kind = request.query.get("kind")
        if kind not in {None, "rulebook", "module"}:
            raise web.HTTPBadRequest(text="kind must be rulebook or module")
        kinds = (kind,) if kind else ("rulebook", "module")
        result: dict[str, Any] = {}
        for draft_kind in kinds:
            result[draft_kind] = await self.call(
                f"{draft_kind}_draft",
                {
                    "campaign_id": campaign_id,
                    "action": "get",
                    "payload": {},
                    "principal_id": principal_id,
                },
            )
        return await self.envelope(request, result, campaign_id)

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

    async def combat_render(self, request: web.Request) -> web.Response:
        """Return the MCP-rendered PNG without recreating combat projection in the UI."""

        campaign_id = request.match_info["campaign_id"]
        audience_projection = request.query.get("audience_projection", "party_public")
        if audience_projection not in {"caller", "party_public"}:
            raise web.HTTPBadRequest(
                text="audience_projection must be caller or party_public"
            )
        rendered = await self.client.call_tool(
            "combat_query",
            {
                "campaign_id": campaign_id,
                "view": "render",
                "payload": {"audience_projection": audience_projection},
                "principal_id": self.principal(request),
            },
        )
        image = next(
            (item for item in rendered.content if isinstance(item, ImageContent)),
            None,
        )
        if image is None:
            raise RuntimeError("combat render returned no image content")
        metadata = rendered.structuredContent or {}
        checksum = str(metadata.get("image_checksum") or "")
        headers = {
            "Cache-Control": "no-store",
            "Content-Disposition": (
                f'attachment; filename="sagasmith-combat-{campaign_id}.png"'
            ),
        }
        if checksum:
            headers["ETag"] = f'"{checksum}"'
        return web.Response(
            body=base64.b64decode(image.data, validate=True),
            content_type=image.mimeType,
            headers=headers,
        )

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
    gateway_config: GatewayConfig | None = None,
    mcp_client: DndMcpClient | None = None,
    mcp_config: McpConfig | None = None,
) -> web.Application:
    config = gateway_config or GatewayConfig.from_environment()
    pool = None if mcp_client is not None else DndClientPool(config)
    gateway = DndGateway(config, mcp_client, mcp_config or McpConfig.from_environment())

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
            context_token = None
            browser_token = None
            created = False
            try:
                if pool is not None and request.path.startswith("/api/"):
                    browser_token, client, created = await pool.session(
                        request.cookies.get(COOKIE_NAME),
                        request.match_info.get("campaign_id") or None,
                    )
                    context_token = _REQUEST_CLIENT.set(client)
                response = await handler(request)
            except web.HTTPException:
                raise
            except IdempotencyConflictError as exc:
                response = web.json_response({"error": str(exc)}, status=409)
            except PermissionError as exc:
                response = web.json_response({"error": str(exc)}, status=403)
            except LookupError as exc:
                response = web.json_response({"error": str(exc)}, status=404)
            except (KeyError, TypeError, ValueError) as exc:
                response = web.json_response({"error": str(exc)}, status=400)
            except Exception:
                LOGGER.exception("unhandled D&D gateway request failure")
                response = web.json_response({"error": "internal gateway error"}, status=500)
            finally:
                if context_token is not None:
                    _REQUEST_CLIENT.reset(context_token)
            if created and browser_token and not response.prepared:
                response.set_cookie(
                    COOKIE_NAME,
                    browser_token,
                    httponly=True,
                    samesite="Strict",
                    secure=False,
                    max_age=config.session_ttl_seconds,
                )
        if origin and origin in config.allowed_origins:
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Access-Control-Allow-Credentials"] = "true"
            response.headers["Vary"] = "Origin"
            response.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type"
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

    app = web.Application(
        middlewares=[boundary],
        client_max_size=config.upload_limit_bytes,
    )
    app[GATEWAY_KEY] = gateway

    async def mcp_lifecycle(_: web.Application):
        if mcp_client is not None:
            await mcp_client.start()
        try:
            yield
        finally:
            if mcp_client is not None:
                await mcp_client.stop()
            elif pool is not None:
                await pool.close()

    app.cleanup_ctx.append(mcp_lifecycle)
    app.router.add_route("OPTIONS", "/{tail:.*}", options)
    app.router.add_get("/api/health", gateway.health)
    app.router.add_get("/api/campaigns", gateway.campaigns)
    app.router.add_get("/api/campaigns/{campaign_id}", gateway.campaign)
    app.router.add_get("/api/campaigns/{campaign_id}/characters", gateway.characters)
    app.router.add_get(
        "/api/campaigns/{campaign_id}/characters/{character_id}", gateway.character
    )
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
    app.router.add_get(
        "/api/campaigns/{campaign_id}/content-packs",
        gateway.content_packs,
    )
    app.router.add_get(
        "/api/campaigns/{campaign_id}/content-packs/detail",
        gateway.content_pack_detail,
    )
    app.router.add_post(
        "/api/campaigns/{campaign_id}/content-packs/import",
        gateway.content_pack_import,
    )
    app.router.add_post(
        "/api/campaigns/{campaign_id}/content-packs/action",
        gateway.content_pack_action,
    )
    app.router.add_get(
        "/api/campaigns/{campaign_id}/content-packs/artifacts/{artifact}",
        gateway.content_pack_artifact,
    )
    app.router.add_post(
        "/api/campaigns/{campaign_id}/actors/from-preset",
        gateway.actor_from_preset,
    )
    app.router.add_get(
        "/api/campaigns/{campaign_id}/rule-context",
        gateway.rule_context,
    )
    app.router.add_get(
        "/api/campaigns/{campaign_id}/drafts",
        gateway.drafts,
    )
    app.router.add_get("/api/campaigns/{campaign_id}/combat", gateway.combat)
    app.router.add_get(
        "/api/campaigns/{campaign_id}/combat/render",
        gateway.combat_render,
    )
    app.router.add_post("/api/campaigns/{campaign_id}/combat/move", gateway.combat_move)
    app.router.add_get("/api/campaigns/{campaign_id}/stream", gateway.stream)
    app.router.add_get("/api/rules", gateway.rule_sources)
    app.router.add_get("/api/rules/search", gateway.rule_search)

    async def agent_webui(_: web.Request) -> web.Response:
        raise web.HTTPFound(config.agent_webui_url)

    app.router.add_get("/agent", agent_webui)

    if config.ui_dist is not None:
        ui_root = config.ui_dist.resolve()
        if not (ui_root / "index.html").is_file():
            raise ValueError(f"D&D UI dist is missing index.html: {ui_root}")

        async def ui_file(request: web.Request) -> web.FileResponse:
            relative = request.match_info.get("path", "").strip("/")
            candidates = [
                ui_root / relative,
                ui_root / relative / "index.html",
            ] if relative else [ui_root / "index.html"]
            target = next(
                (
                    candidate.resolve()
                    for candidate in candidates
                    if candidate.is_file() and candidate.resolve().is_relative_to(ui_root)
                ),
                None,
            )
            if target is None:
                raise web.HTTPNotFound(text="D&D UI route not found")
            return web.FileResponse(target)

        app.router.add_get("/{path:.*}", ui_file)
    return app


def main() -> None:
    config = GatewayConfig.from_environment()
    web.run_app(create_app(gateway_config=config), host=config.host, port=config.port)


if __name__ == "__main__":
    main()
