import asyncio
from pathlib import Path

from scripts.smoke_seed import seed


def test_smoke_seed_uses_only_registered_public_facades(tmp_path: Path) -> None:
    result = asyncio.run(seed(tmp_path / "mcp-home"))

    assert result["campaign_id"]
    assert len(result["pc_ids"]) == 2
    assert result["npc_id"]
    assert result["event_id"]
    assert len(result["actor_knowledge_ids"]) == 2
    assert result["baseline_snapshot_id"]
