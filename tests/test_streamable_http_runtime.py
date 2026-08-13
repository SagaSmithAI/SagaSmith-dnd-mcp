import asyncio
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

from sagasmith_dnd_mcp.config import McpConfig
from sagasmith_dnd_mcp.gateway import DndMcpClient, GatewayConfig, create_app


def _unused_loopback_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _wait_for_port(port: int, process: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            raise AssertionError(
                f"D&D MCP exited before startup ({process.returncode}):\n{stdout}\n{stderr}"
            )
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.25):
                return
        except OSError:
            time.sleep(0.1)
    raise AssertionError("D&D MCP streamable HTTP endpoint did not start")


def test_real_streamable_http_client_tracks_dynamic_tools(tmp_path: Path) -> None:
    port = _unused_loopback_port()
    environment = os.environ.copy()
    environment.update(
        {
            "SAGASMITH_DND_MCP_TRANSPORT": "streamable-http",
            "SAGASMITH_DND_MCP_HTTP_HOST": "127.0.0.1",
            "SAGASMITH_DND_MCP_HTTP_PORT": str(port),
            "SAGASMITH_DND_MCP_HOME": str(tmp_path / "home"),
            "SAGASMITH_DND_MCP_AUTO_SEED": "0",
            "SAGASMITH_DND_SKILLS_DIR": str(tmp_path / "dnd-skills"),
            "SAGASMITH_MODULEGEN_SKILLS_DIR": str(tmp_path / "modulegen-skills"),
        }
    )
    process = subprocess.Popen(
        [sys.executable, "-m", "sagasmith_dnd_mcp.server"],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _wait_for_port(port, process)

        async def exercise() -> None:
            client = DndMcpClient(f"http://127.0.0.1:{port}/mcp")
            await client.start()
            try:
                capabilities = await client.call_tool("server_capabilities", {})
                assert capabilities.isError is not True

                created = await client.call_tool(
                    "campaign_create",
                    {"name": "HTTP Table", "idempotency_key": "http-campaign"},
                )
                assert created.isError is not True
                campaign = dict(created.structuredContent or {})
                campaign = dict(campaign.get("result") or campaign)

                queried = await client.call_tool(
                    "campaign_query",
                    {"action": "get", "campaign_id": campaign["id"]},
                )
                assert queried.isError is not True
                characters_direct = await asyncio.wait_for(
                    client.call_tool(
                        "character_query",
                        {
                            "view": "list",
                            "payload": {"campaign_id": campaign["id"]},
                        },
                    ),
                    15,
                )
                assert characters_direct.isError is not True
            finally:
                await client.stop()

            ui_dist = tmp_path / "ui-dist"
            ui_dist.mkdir()
            (ui_dist / "index.html").write_text("<h1>Remote Workbench</h1>", encoding="utf-8")
            app = create_app(
                GatewayConfig(
                    mcp_url=f"http://127.0.0.1:{port}/mcp",
                    ui_dist=ui_dist,
                ),
                mcp_config=McpConfig(
                    home=tmp_path / "home",
                    database_url=None,
                    chroma_url=None,
                    chroma_path_override=None,
                    dnd_skills_dir=tmp_path / "dnd-skills",
                    modulegen_skills_dir=tmp_path / "modulegen-skills",
                    auto_seed_rules=False,
                ),
            )
            gateway = TestClient(TestServer(app))
            await gateway.start_server()
            try:
                health = await asyncio.wait_for(gateway.get("/api/health"), 15)
                assert health.status == 200
                campaigns = await asyncio.wait_for(gateway.get("/api/campaigns"), 15)
                assert campaigns.status == 200
                assert (await campaigns.json())["data"][0]["id"] == campaign["id"]
                workbench = await asyncio.wait_for(gateway.get("/"), 15)
                assert "Remote Workbench" in await workbench.text()
            finally:
                await gateway.close()

        asyncio.run(exercise())
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
