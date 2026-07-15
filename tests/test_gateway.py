import asyncio
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

from sagasmith_dnd_mcp.config import McpConfig
from sagasmith_dnd_mcp.gateway import GATEWAY_KEY, GatewayConfig, create_app


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


def test_gateway_projects_mcp_data_and_enforces_origin(tmp_path: Path) -> None:
    async def exercise() -> None:
        app = create_app(
            config(tmp_path),
            GatewayConfig(allowed_origins=("http://ui.test",)),
        )
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
            payload = await response.json()
            assert payload["data"][0]["id"] == campaign["id"]
            assert payload["meta"]["audience"] == "system:local"

            preflight = await client.options(
                f"/api/campaigns/{campaign['id']}/combat/move",
                headers={
                    "Origin": "http://ui.test",
                    "Access-Control-Request-Method": "POST",
                    "Access-Control-Request-Headers": (
                        "content-type,x-sagasmith-principal"
                    ),
                },
            )
            assert preflight.status == 204
            assert preflight.headers["Access-Control-Allow-Origin"] == "http://ui.test"
            assert "X-SagaSmith-Principal" in preflight.headers[
                "Access-Control-Allow-Headers"
            ]

            detail = await client.get(f"/api/campaigns/{campaign['id']}")
            detail_payload = await detail.json()
            assert detail_payload["meta"]["campaign_revision"] == campaign["revision"]
            assert detail_payload["meta"]["branch_id"]

            staged = await gateway.call(
                "module_import",
                {
                    "campaign_id": campaign["id"],
                    "action": "stage",
                    "payload": {
                        "name": "gateway-map.md",
                        "title": "Gateway map",
                        "source_key": "gateway-map",
                        "content": "# Arrival\n## Gatehouse\nA guarded stone gate.",
                    },
                    "idempotency_key": "gateway-module-stage",
                },
            )
            job_id = staged["job"]["id"]
            for action in ("inspect", "validate", "ingest"):
                await gateway.call(
                    "module_import",
                    {
                        "campaign_id": campaign["id"],
                        "action": action,
                        "payload": {"job_id": job_id},
                        "idempotency_key": f"gateway-module-{action}",
                    },
                )
            current_campaign = await gateway.call(
                "campaign_query",
                {
                    "view": "get",
                    "payload": {"campaign_id": campaign["id"]},
                },
            )
            await gateway.call(
                "module_import",
                {
                    "campaign_id": campaign["id"],
                    "action": "activate",
                    "payload": {"job_id": job_id},
                    "expected_revision": current_campaign["revision"],
                    "idempotency_key": "gateway-module-activate",
                },
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
        app = create_app(config(tmp_path), GatewayConfig(bearer_token="secret"))
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
