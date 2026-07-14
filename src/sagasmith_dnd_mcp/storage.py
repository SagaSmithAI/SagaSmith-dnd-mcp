"""The MCP-owned SQLite and ChromaDB service boundary."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sagasmith_core import Database, VectorStore, create_embedder
from sagasmith_core.database import sqlite_database_url

from sagasmith_dnd_mcp.config import McpConfig


class SagaSmithStorage:
    def __init__(self, config: McpConfig) -> None:
        self.config = config
        self.config.prepare()
        self.database = Database(config.database_url or sqlite_database_url(config.database_path))
        self.vectors = VectorStore("dnd5e")

    def migrate(self) -> None:
        self.database.upgrade_schema()

    def dense_components(self) -> tuple[Any | None, VectorStore | None]:
        """Lazily create the embedder so a normal FTS-only server stays lightweight."""
        if not self._dense_enabled():
            return None, None
        return create_embedder(env_prefix="DND5E"), self.vectors

    def status(self) -> dict[str, Any]:
        return {
            "home": str(self.config.home),
            "database": {
                "url": self.database.url,
                "path": str(self.config.database_path),
                "exists": self.config.database_path.exists(),
            },
            "chroma": {
                "url": self.config.chroma_url,
                "path": str(self.config.chroma_path),
                "configured": self.vectors.enabled,
                "dense_enabled": self._dense_enabled(),
                "rules": self._collection_status("rules"),
                "modules": self._collection_status("modules"),
            },
            "artifacts_dir": str(self.config.artifacts_dir),
            "rules": {
                "auto_seed": self.config.auto_seed_rules,
                "seed_root": str(
                    self.config.dnd_skills_dir / "full" / "skills" / "dnd-dm" / "srd"
                ),
            },
        }

    def write_module(self, name: str, content: str) -> Path:
        if not name.strip():
            raise ValueError("module name must not be empty")
        if len(content.encode("utf-8")) > 20 * 1024 * 1024:
            raise ValueError("module artifact exceeds the 20 MiB safety limit")
        filename = name if name.casefold().endswith(".md") else f"{name}.md"
        target = (self.config.modules_dir / filename).resolve()
        if target.parent != self.config.modules_dir.resolve():
            raise ValueError("module name must not contain a path")
        target.write_text(content, encoding="utf-8")
        return target

    def artifact_module_path(self, name: str) -> Path:
        target = (self.config.modules_dir / name).resolve()
        if target.parent != self.config.modules_dir.resolve() or target.suffix != ".md":
            raise ValueError("module artifact must be a .md file directly under artifacts/modules")
        if not target.is_file():
            raise LookupError(name)
        return target

    @staticmethod
    def _dense_enabled() -> bool:
        import os

        return os.environ.get("SAGASMITH_DND_MCP_DENSE_ENABLED", "0") == "1"

    def _collection_status(self, name: str) -> dict[str, Any]:
        if not self._dense_enabled():
            return {"name": self.vectors.scoped_name(name), "count": None, "status": "disabled"}
        return self.vectors.collection_stats(name)
