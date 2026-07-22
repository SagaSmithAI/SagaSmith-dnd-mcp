"""The MCP-owned SQLite and ChromaDB service boundary."""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Any

from sagasmith_core import (
    Database,
    RapidOcrProvider,
    VectorStore,
    create_embedder,
    file_sha256,
)
from sagasmith_core.database import sqlite_database_url

from sagasmith_dnd_mcp.config import McpConfig


class SagaSmithStorage:
    def __init__(self, config: McpConfig) -> None:
        self.config = config
        self.config.prepare()
        self.database = Database(config.database_url or sqlite_database_url(config.database_path))
        self.vectors = VectorStore("dnd5e")
        self._rule_ocr_provider: RapidOcrProvider | None = None

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
                "seed_root": str(self.config.dnd_skills_dir / "full" / "skills" / "dnd-dm" / "srd"),
                "rulebooks_dir": str(self.config.rulebooks_dir),
                "normalized_rulebooks_dir": str(self.config.normalized_rulebooks_dir),
                "import_roots": [str(path) for path in self.config.rule_import_roots],
                "ocr": {
                    "enabled": self.config.rule_ocr_enabled,
                    "provider": "rapidocr" if self.config.rule_ocr_enabled else None,
                    "scale": self.config.rule_ocr_scale,
                },
            },
            "modules": {
                "artifacts_dir": str(self.config.modules_dir),
                "import_roots": [str(path) for path in self.config.module_import_roots],
            },
        }

    def stage_rulebook(self, source_path: str | Path) -> dict[str, Any]:
        """Copy an allowlisted user document into content-addressed MCP storage."""
        source = Path(source_path).expanduser().resolve()
        if not source.is_file():
            raise LookupError(str(source))
        if source.suffix.casefold() not in {".pdf", ".md", ".markdown", ".txt"}:
            raise ValueError("rulebook must be PDF, Markdown, or text")
        if not self.config.rule_import_roots:
            raise PermissionError("no rulebook import roots are configured")
        if not any(source.is_relative_to(root.resolve()) for root in self.config.rule_import_roots):
            raise PermissionError("rulebook source is outside configured import roots")
        size = source.stat().st_size
        if size > 100 * 1024 * 1024:
            raise ValueError("rulebook exceeds the 100 MiB safety limit")
        checksum = file_sha256(source)
        safe_name = re.sub(r"[^A-Za-z0-9._-]+", "-", source.name).strip("-.")
        safe_name = safe_name or f"rulebook{source.suffix.casefold()}"
        artifact = f"{checksum[:12]}-{safe_name}"
        target = (self.config.rulebooks_dir / artifact).resolve()
        if target.parent != self.config.rulebooks_dir.resolve():
            raise ValueError("invalid rulebook artifact name")
        if not target.exists():
            shutil.copy2(source, target)
        elif file_sha256(target) != checksum:
            raise RuntimeError("managed rulebook artifact checksum mismatch")
        return {
            "artifact": artifact,
            "path": str(target),
            "checksum": checksum,
            "size": size,
            "staged": True,
        }

    def discover_rulebooks(self) -> list[dict[str, Any]]:
        """List importable documents under configured roots without staging them."""
        allowed = {".pdf", ".md", ".markdown", ".txt"}
        seen: set[Path] = set()
        result: list[dict[str, Any]] = []
        for root in self.config.rule_import_roots:
            resolved_root = root.resolve()
            if not resolved_root.is_dir():
                continue
            for source in sorted(resolved_root.rglob("*"), key=lambda item: str(item).casefold()):
                resolved = source.resolve()
                if (
                    not resolved.is_file()
                    or resolved.suffix.casefold() not in allowed
                    or resolved in seen
                ):
                    continue
                seen.add(resolved)
                result.append(
                    {
                        "path": str(resolved),
                        "root": str(resolved_root),
                        "relative_path": str(resolved.relative_to(resolved_root)),
                        "name": resolved.name,
                        "media_type": (
                            "application/pdf"
                            if resolved.suffix.casefold() == ".pdf"
                            else "text/markdown"
                        ),
                        "size": resolved.stat().st_size,
                    }
                )
        return result

    def rulebook_checksum(self, name: str) -> str:
        return file_sha256(self.artifact_rulebook_path(name))

    def rule_ocr_provider(self) -> RapidOcrProvider | None:
        if not self.config.rule_ocr_enabled:
            return None
        if self._rule_ocr_provider is None:
            self._rule_ocr_provider = RapidOcrProvider(scale=self.config.rule_ocr_scale)
        return self._rule_ocr_provider

    def artifact_rulebook_path(self, name: str) -> Path:
        target = (self.config.rulebooks_dir / name).resolve()
        if target.parent != self.config.rulebooks_dir.resolve() or target.suffix.casefold() not in {
            ".pdf",
            ".md",
            ".markdown",
            ".txt",
        }:
            raise ValueError("invalid managed rulebook artifact")
        if not target.is_file():
            raise LookupError(name)
        return target

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

    def stage_module(self, source_path: str | Path) -> dict[str, Any]:
        """Copy an allowlisted module document into content-addressed MCP storage."""
        source = Path(source_path).expanduser().resolve()
        if not source.is_file():
            raise LookupError(str(source))
        if source.suffix.casefold() not in {".pdf", ".md", ".markdown", ".txt"}:
            raise ValueError("module must be PDF, Markdown, or text")
        if not self.config.module_import_roots:
            raise PermissionError("no module import roots are configured")
        if not any(
            source.is_relative_to(root.resolve()) for root in self.config.module_import_roots
        ):
            raise PermissionError("module source is outside configured import roots")
        size = source.stat().st_size
        if size > 100 * 1024 * 1024:
            raise ValueError("module exceeds the 100 MiB safety limit")
        checksum = file_sha256(source)
        safe_name = re.sub(r"[^A-Za-z0-9._-]+", "-", source.name).strip("-.")
        safe_name = safe_name or f"module{source.suffix.casefold()}"
        artifact = f"{checksum[:12]}-{safe_name}"
        target = (self.config.modules_dir / artifact).resolve()
        if target.parent != self.config.modules_dir.resolve():
            raise ValueError("invalid module artifact name")
        if not target.exists():
            shutil.copy2(source, target)
        elif file_sha256(target) != checksum:
            raise RuntimeError("managed module artifact checksum mismatch")
        return {
            "artifact": artifact,
            "path": str(target),
            "checksum": checksum,
            "size": size,
            "media_type": (
                "application/pdf" if source.suffix.casefold() == ".pdf" else "text/markdown"
            ),
            "staged": True,
        }

    def artifact_module_path(self, name: str) -> Path:
        target = (self.config.modules_dir / name).resolve()
        if target.parent != self.config.modules_dir.resolve() or target.suffix.casefold() not in {
            ".pdf",
            ".md",
            ".markdown",
            ".txt",
        }:
            raise ValueError(
                "module artifact must be PDF, Markdown, or text directly under artifacts/modules"
            )
        if not target.is_file():
            raise LookupError(name)
        return target

    def store_rendered_module_page(
        self,
        *,
        module_id: str,
        source_checksum: str,
        page_number: int,
        scale: float,
        checksum: str,
        content: bytes,
    ) -> Path:
        """Persist a content-addressed rendered page beneath MCP-owned storage."""
        if not re.fullmatch(r"[0-9a-fA-F-]{36}", module_id):
            raise ValueError("invalid module id for rendered asset")
        directory = (self.config.module_assets_dir / module_id).resolve()
        if directory.parent != self.config.module_assets_dir.resolve():
            raise ValueError("invalid rendered module asset directory")
        directory.mkdir(parents=True, exist_ok=True)
        scale_key = f"{scale:.2f}".replace(".", "-")
        filename = (
            f"{source_checksum[:12]}-page-{page_number:04d}-"
            f"x{scale_key}-{checksum[:12]}.png"
        )
        target = (directory / filename).resolve()
        if target.parent != directory:
            raise ValueError("invalid rendered module asset path")
        if target.exists():
            if file_sha256(target) != checksum:
                raise RuntimeError("managed rendered page checksum mismatch")
        else:
            target.write_bytes(content)
        return target

    @staticmethod
    def _dense_enabled() -> bool:
        import os

        return os.environ.get("SAGASMITH_DND_MCP_DENSE_ENABLED", "0") == "1"

    def _collection_status(self, name: str) -> dict[str, Any]:
        if not self._dense_enabled():
            return {"name": self.vectors.scoped_name(name), "count": None, "status": "disabled"}
        return self.vectors.collection_stats(name)
