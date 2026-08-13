import asyncio
import base64
import hashlib
import json
from pathlib import Path

from aiohttp import FormData
from aiohttp.test_utils import TestClient, TestServer
from mcp.types import CallToolResult, ImageContent, TextContent
from sagasmith_core.content_pack import dumps_content_archive
from sagasmith_core.idempotency import IdempotencyConflictError
from sagasmith_dnd.character_schema import default_character_notes, default_character_sheet
from sagasmith_dnd.content_actors import build_dnd_content_actor
from sagasmith_dnd.content_packages import build_preset_content_package

from sagasmith_dnd_mcp import gateway as gateway_module
from sagasmith_dnd_mcp.config import McpConfig
from sagasmith_dnd_mcp.gateway import (
    GATEWAY_KEY,
    DndClientPool,
    GatewayConfig,
    create_app,
)
from sagasmith_dnd_mcp.server import create_server
from tests.authoring_helpers import finalize_and_activate_module


def config(tmp_path: Path) -> McpConfig:
    return McpConfig(
        home=tmp_path / "home",
        database_url=None,
        chroma_url=None,
        chroma_path_override=None,
        dnd_skills_dir=tmp_path / "dnd",
        modulegen_skills_dir=tmp_path / "modulegen",
        auto_seed_rules=False,
    )


class InProcessTestClient:
    """Unit-test client; real streamable HTTP is covered separately."""

    def __init__(self, value: McpConfig):
        self.server = create_server(value)

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def call_tool(self, tool_id: str, arguments: dict) -> CallToolResult:
        content, structured = await self.server.call_tool(tool_id, arguments)
        return CallToolResult(content=content, structuredContent=structured)


def app_for(tmp_path: Path, gateway_config: GatewayConfig | None = None):
    value = config(tmp_path)
    return create_app(
        gateway_config or GatewayConfig(),
        InProcessTestClient(value),
        value,
    )


def test_gateway_pool_is_sticky_and_rotates_only_switching_browser(monkeypatch) -> None:
    class FakeClient:
        instances = []

        def __init__(self, url: str):
            self.url = url
            self.started = False
            self.stopped = False
            self.instances.append(self)

        async def start(self) -> None:
            self.started = True

        async def stop(self) -> None:
            self.stopped = True

    monkeypatch.setattr(gateway_module, "DndMcpClient", FakeClient)

    async def exercise() -> None:
        pool = DndClientPool(GatewayConfig())
        first_token, first, created = await pool.session(None, "campaign-a")
        assert created is True
        same_token, same, created = await pool.session(first_token, "campaign-a")
        assert same_token == first_token
        assert same is first
        assert created is False

        second_token, second, second_created = await pool.session(None, "campaign-a")
        assert second_created is True
        assert second_token != first_token
        assert second is not first

        rotated_token, rotated, rotated_created = await pool.session(
            first_token, "campaign-b"
        )
        assert rotated_token == first_token
        assert rotated_created is False
        assert rotated is not first
        assert first.stopped is True
        assert second.stopped is False

        await pool.close()
        assert second.stopped is True
        assert rotated.stopped is True

    asyncio.run(exercise())


def test_character_route_carries_campaign_into_the_mcp_exposure(tmp_path: Path) -> None:
    async def exercise() -> None:
        app = app_for(tmp_path)
        gateway = app[GATEWAY_KEY]
        calls: list[tuple[str, dict]] = []

        async def call(tool_id: str, arguments: dict):
            calls.append((tool_id, arguments))
            if tool_id == "character_query":
                return {"id": "actor-1", "campaign_id": "campaign-1"}
            if tool_id == "campaign_query":
                return {"id": "campaign-1", "revision": 3}
            if tool_id == "branch_query":
                return [{"id": "branch-1", "is_current": True}]
            raise AssertionError(tool_id)

        gateway.call = call
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            response = await client.get(
                "/api/campaigns/campaign-1/characters/actor-1"
            )
            assert response.status == 200
            assert (await response.json())["data"]["id"] == "actor-1"
            character_call = next(item for item in calls if item[0] == "character_query")
            assert character_call[1]["payload"] == {
                "campaign_id": "campaign-1",
                "character_id": "actor-1",
            }
            assert (await client.get("/api/characters/actor-1")).status in {404, 405}
        finally:
            await client.close()

    asyncio.run(exercise())


def test_gateway_projects_mcp_data_and_enforces_origin(tmp_path: Path) -> None:
    async def exercise() -> None:
        app = app_for(tmp_path, GatewayConfig(allowed_origins=("http://ui.test",)))
        gateway = app[GATEWAY_KEY]
        campaign = await gateway.call(
            "campaign_create",
            {"name": "Gateway Table", "idempotency_key": "gateway-campaign"},
        )
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            health = await client.get("/api/health")
            assert health.status == 200
            health_payload = await health.json()
            assert health_payload["data"]["status"] == "ok"

            response = await client.get(
                "/api/campaigns",
                headers={"Origin": "http://ui.test"},
            )
            assert response.status == 200
            assert response.headers["Access-Control-Allow-Origin"] == "http://ui.test"
            assert response.headers["Access-Control-Allow-Credentials"] == "true"
            payload = await response.json()
            assert payload["data"][0]["id"] == campaign["id"]
            assert payload["meta"]["audience"] == "system:local"

            preflight = await client.options(
                f"/api/campaigns/{campaign['id']}/combat/move",
                headers={
                    "Origin": "http://ui.test",
                    "Access-Control-Request-Method": "POST",
                    "Access-Control-Request-Headers": "content-type",
                },
            )
            assert preflight.status == 204
            assert preflight.headers["Access-Control-Allow-Origin"] == "http://ui.test"
            assert "X-SagaSmith-Principal" not in preflight.headers[
                "Access-Control-Allow-Headers"
            ]

            detail = await client.get(f"/api/campaigns/{campaign['id']}")
            detail_payload = await detail.json()
            assert detail_payload["meta"]["campaign_revision"] == campaign["revision"]
            assert detail_payload["meta"]["branch_id"]

            staged = await gateway.call(
                "module_draft",
                {
                    "campaign_id": campaign["id"],
                    "action": "start",
                    "payload": {
                        "name": "gateway-map.md",
                        "title": "Gateway map",
                        "source_key": "gateway-map",
                        "content": "# Arrival\n## Gatehouse\nA guarded stone gate.",
                    },
                    "idempotency_key": "gateway-module-stage",
                },
            )
            await finalize_and_activate_module(
                lambda _server, name, arguments: gateway.call(name, arguments),
                gateway,
                campaign["id"],
                staged,
                source_key="gateway-map",
                title="Gateway map",
                portable_id="dnd5e.module.gateway-map",
                edition="2024",
            )
            scene_response = await client.get(
                f"/api/campaigns/{campaign['id']}/scenes"
            )
            assert scene_response.status == 200
            scene_payload = await scene_response.json()
            assert any(
                scene["title"] == "Gatehouse" for scene in scene_payload["data"]
            )
            progress_response = await client.get(
                f"/api/campaigns/{campaign['id']}/scene-progress?scope=party"
            )
            assert progress_response.status == 200
            progress_payload = await progress_response.json()
            assert progress_payload["meta"]["campaign_revision"] >= campaign["revision"]

            denied = await client.get(
                "/api/campaigns",
                headers={"Origin": "http://untrusted.test"},
            )
            assert denied.status == 403
        finally:
            await client.close()

    asyncio.run(exercise())


def test_gateway_requires_configured_bearer_token(tmp_path: Path) -> None:
    async def exercise() -> None:
        app = app_for(tmp_path, GatewayConfig(bearer_token="secret"))
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            denied = await client.get("/api/health")
            assert denied.status == 401
            allowed = await client.get(
                "/api/health",
                headers={"Authorization": "Bearer secret"},
            )
            assert allowed.status == 200
        finally:
            await client.close()

    asyncio.run(exercise())


def test_gateway_serves_built_workbench_and_redirects_to_agent(tmp_path: Path) -> None:
    ui_dist = tmp_path / "ui-dist"
    ui_dist.mkdir()
    (ui_dist / "index.html").write_text("<h1>D&D Workbench</h1>", encoding="utf-8")

    async def exercise() -> None:
        app = app_for(
            tmp_path,
            GatewayConfig(
                ui_dist=ui_dist,
                agent_webui_url="http://127.0.0.1:18765/",
            ),
        )
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            workbench = await client.get("/")
            assert workbench.status == 200
            assert "D&D Workbench" in await workbench.text()

            agent = await client.get("/agent", allow_redirects=False)
            assert agent.status == 302
            assert agent.headers["Location"] == "http://127.0.0.1:18765/"
        finally:
            await client.close()

    asyncio.run(exercise())


def test_gateway_streams_native_combat_render_content(tmp_path: Path) -> None:
    async def exercise() -> None:
        app = app_for(tmp_path)
        gateway = app[GATEWAY_KEY]
        image = b"\x89PNG\r\n\x1a\nrender"
        metadata = {
            "mime_type": "image/png",
            "image_checksum": hashlib.sha256(image).hexdigest(),
        }
        calls = []

        async def render(tool_id, arguments):
            calls.append((tool_id, arguments))
            return CallToolResult(
                content=[
                    TextContent(type="text", text=json.dumps(metadata)),
                    ImageContent(
                        type="image",
                        mimeType="image/png",
                        data=base64.b64encode(image).decode("ascii"),
                    ),
                ],
                structuredContent=metadata,
            )

        gateway.client.call_tool = render
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            response = await client.get(
                "/api/campaigns/campaign-1/combat/render",
                headers={"X-SagaSmith-Principal": "user:forged"},
            )
            assert response.status == 200
            assert response.content_type == "image/png"
            assert await response.read() == image
            assert response.headers["Cache-Control"] == "no-store"
            assert calls == [
                (
                    "combat_query",
                    {
                        "campaign_id": "campaign-1",
                        "view": "render",
                        "payload": {"audience_projection": "party_public"},
                        "principal_id": "system:local",
                    },
                )
            ]
        finally:
            await client.close()

    asyncio.run(exercise())


def test_gateway_exposes_known_errors_and_hides_unknown_failures(tmp_path: Path) -> None:
    async def exercise() -> None:
        cases = [
            (ValueError("bad request"), 400, "bad request"),
            (IdempotencyConflictError("request key conflict"), 409, "request key conflict"),
            (RuntimeError("database secret"), 500, "internal gateway error"),
        ]
        for error, expected_status, expected_message in cases:
            app = app_for(tmp_path)
            gateway = app[GATEWAY_KEY]

            async def fail(_tool_id, _arguments, *, raised=error):
                raise raised

            gateway.call = fail
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                response = await client.get("/api/health")
                assert response.status == expected_status
                assert await response.json() == {"error": expected_message}
            finally:
                await client.close()

    asyncio.run(exercise())


def test_gateway_imports_and_projects_finalized_preset_inventory(tmp_path: Path) -> None:
    notes = default_character_notes()
    notes["profile"]["summary"] = "Gateway preset actor."
    sheet = default_character_sheet()
    sheet["edition"] = "2024"
    actor = build_dnd_content_actor(
        actor_id="example.gateway.actor",
        version="1.0.0",
        actor_type="npc",
        name="Gateway Actor",
        sheet=sheet,
        notes=notes,
    )
    package, blobs = build_preset_content_package(
        package_id="example.gateway-presets",
        version="1.0.0",
        system_id="dnd5e",
        title="Gateway Presets",
        cards=[actor],
        metadata={
            "edition": "2024",
            "distribution": "private",
            "license": "user-supplied",
            "attribution": "Gateway test fixture",
        },
    )
    archive = dumps_content_archive(package, blobs)

    async def exercise() -> None:
        app = app_for(tmp_path)
        gateway = app[GATEWAY_KEY]
        campaign = await gateway.call(
            "campaign_create",
            {
                "name": "Gateway content",
                "edition": "2024",
                "idempotency_key": "gateway-content-campaign",
            },
        )
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            form = FormData()
            form.add_field("kind", "preset")
            form.add_field("idempotency_key", "gateway-import-preset")
            form.add_field(
                "archive",
                archive,
                filename="gateway.sagasmith-pack",
                content_type="application/octet-stream",
            )
            imported = await client.post(
                f"/api/campaigns/{campaign['id']}/content-packs/import",
                data=form,
            )
            assert imported.status == 200
            import_payload = await imported.json()
            assert import_payload["data"]["archive"]["sha256"] == hashlib.sha256(
                archive
            ).hexdigest()
            assert import_payload["data"]["import"]["stored"] is True

            inventory = await client.get(
                f"/api/campaigns/{campaign['id']}/content-packs"
            )
            assert inventory.status == 200
            inventory_payload = await inventory.json()
            preset = next(
                item
                for item in inventory_payload["data"]["packs"]
                if item["kind"] == "preset" and item["id"] == package["id"]
            )
            assert preset["local_ref"] == "example.gateway-presets.actors"
            assert preset["active"] is False

            detail = await client.get(
                f"/api/campaigns/{campaign['id']}/content-packs/detail",
                params={
                    "kind": "preset",
                    "pack_id": package["id"],
                    "version": package["version"],
                },
            )
            assert detail.status == 200
            assert (await detail.json())["data"]["content_package"] == package

            created = await client.post(
                f"/api/campaigns/{campaign['id']}/actors/from-preset",
                json={
                    "pack_id": package["id"],
                    "version": package["version"],
                    "artifact_id": actor["id"],
                    "idempotency_key": "gateway-create-preset-actor",
                },
            )
            assert created.status == 200
            assert (await created.json())["data"]["character"]["name"] == "Gateway Actor"

            exported = await client.post(
                f"/api/campaigns/{campaign['id']}/content-packs/action",
                json={
                    "kind": "preset",
                    "action": "export",
                    "pack_id": "example.gateway-presets.actors",
                    "version": package["version"],
                    "idempotency_key": "gateway-export-preset",
                },
            )
            assert exported.status == 200
            export_payload = await exported.json()
            artifact = export_payload["data"]["artifact"]["artifact"]
            downloaded = await client.get(
                f"/api/campaigns/{campaign['id']}/content-packs/artifacts/{artifact}",
                params={"kind": "preset"},
            )
            assert downloaded.status == 200
            assert await downloaded.read() == archive
        finally:
            await client.close()

    asyncio.run(exercise())
