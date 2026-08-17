"""The MCP-owned SQLite and ChromaDB service boundary."""

from __future__ import annotations

import hashlib
import re
import shutil
from mimetypes import guess_type
from pathlib import Path
from typing import Any

from sagasmith_core import (
    DOCUMENT_SOURCE_SUFFIXES,
    CascadingOcrProvider,
    Database,
    RapidOcrProvider,
    VectorStore,
    create_embedder,
    file_sha256,
)
from sagasmith_core.database import sqlite_database_url
from sagasmith_core.managed_artifacts import (
    read_content_archive as read_managed_content_archive,
)
from sagasmith_core.managed_artifacts import (
    write_content_archive as write_managed_content_archive,
)
from sagasmith_dnd.system import DND5E

from sagasmith_dnd_mcp.config import McpConfig


class SagaSmithStorage:
    def __init__(self, config: McpConfig) -> None:
        self.config = config
        self.config.prepare()
        self.database = Database(config.database_url or sqlite_database_url(config.database_path))
        self.vectors = VectorStore(DND5E.id)
        self._rule_ocr_provider: RapidOcrProvider | None = None
        self._module_ocr_provider: RapidOcrProvider | None = None
        self._rule_document_ocr_provider: CascadingOcrProvider | None = None
        self._module_document_ocr_provider: CascadingOcrProvider | None = None

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
            "content_packages_dir": str(self.config.content_packages_dir),
            "actor_images_dir": str(self.config.actor_images_dir),
            "rules": {
                "auto_seed": self.config.auto_seed_rules,
                "seed_root": str(self.config.dnd_skills_dir / "full" / "skills" / "dnd-dm" / "srd"),
                "rulebooks_dir": str(self.config.rulebooks_dir),
                "normalized_rulebooks_dir": str(self.config.normalized_rulebooks_dir),
                "import_roots": [str(path) for path in self.config.rule_import_roots],
                "ocr": {
                    "enabled": self.config.rule_ocr_enabled,
                    "provider": ("rapidocr-cascade" if self.config.rule_ocr_enabled else None),
                    "scale": self.config.rule_ocr_scale,
                    "models": (
                        self.ocr_model_chain(self.config.rule_ocr_model)
                        if self.config.rule_ocr_enabled
                        else []
                    ),
                },
            },
            "modules": {
                "artifacts_dir": str(self.config.modules_dir),
                "normalized_modules_dir": str(self.config.normalized_modules_dir),
                "import_roots": [str(path) for path in self.config.module_import_roots],
                "ocr": {
                    "enabled": self.config.module_ocr_enabled,
                    "provider": ("rapidocr-cascade" if self.config.module_ocr_enabled else None),
                    "scale": self.config.module_ocr_scale,
                    "models": (
                        self.ocr_model_chain(self.config.module_ocr_model)
                        if self.config.module_ocr_enabled
                        else []
                    ),
                },
            },
        }

    def stage_rulebook(self, source_path: str | Path) -> dict[str, Any]:
        """Copy an allowlisted user document into content-addressed MCP storage."""
        source = Path(source_path).expanduser().resolve()
        if not source.is_file():
            raise LookupError(str(source))
        if source.suffix.casefold() not in DOCUMENT_SOURCE_SUFFIXES:
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
                    or resolved.suffix.casefold() not in DOCUMENT_SOURCE_SUFFIXES
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

    @staticmethod
    def _alternate_ocr_model(model: str) -> str:
        return "medium" if model == "small" else "small"

    @classmethod
    def ocr_model_chain(cls, model: str) -> list[str]:
        return [model, cls._alternate_ocr_model(model)]

    def _document_ocr_provider(
        self,
        *,
        primary: RapidOcrProvider,
        scale: float,
        model: str,
    ) -> CascadingOcrProvider:
        return CascadingOcrProvider(
            primary,
            RapidOcrProvider(
                scale=scale,
                model_type=self._alternate_ocr_model(model),
                cache_dir=self.config.ocr_page_cache_dir,
            ),
        )

    def rule_ocr_provider(self) -> RapidOcrProvider | None:
        if not self.config.rule_ocr_enabled:
            return None
        if self._rule_ocr_provider is None:
            self._rule_ocr_provider = RapidOcrProvider(
                scale=self.config.rule_ocr_scale,
                model_type=self.config.rule_ocr_model,
                cache_dir=self.config.ocr_page_cache_dir,
            )
        return self._rule_ocr_provider

    def rule_document_ocr_provider(self) -> CascadingOcrProvider | None:
        primary = self.rule_ocr_provider()
        if primary is None:
            return None
        if self._rule_document_ocr_provider is None:
            self._rule_document_ocr_provider = self._document_ocr_provider(
                primary=primary,
                scale=self.config.rule_ocr_scale,
                model=self.config.rule_ocr_model,
            )
        return self._rule_document_ocr_provider

    def module_ocr_provider(self) -> RapidOcrProvider | None:
        if not self.config.module_ocr_enabled:
            return None
        if self._module_ocr_provider is None:
            self._module_ocr_provider = RapidOcrProvider(
                scale=self.config.module_ocr_scale,
                model_type=self.config.module_ocr_model,
                cache_dir=self.config.ocr_page_cache_dir,
            )
        return self._module_ocr_provider

    def module_document_ocr_provider(self) -> CascadingOcrProvider | None:
        primary = self.module_ocr_provider()
        if primary is None:
            return None
        if self._module_document_ocr_provider is None:
            self._module_document_ocr_provider = self._document_ocr_provider(
                primary=primary,
                scale=self.config.module_ocr_scale,
                model=self.config.module_ocr_model,
            )
        return self._module_document_ocr_provider

    def artifact_rulebook_path(self, name: str) -> Path:
        target = (self.config.rulebooks_dir / name).resolve()
        if (
            target.parent != self.config.rulebooks_dir.resolve()
            or target.suffix.casefold() not in DOCUMENT_SOURCE_SUFFIXES
        ):
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
        if source.suffix.casefold() not in DOCUMENT_SOURCE_SUFFIXES:
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
        if (
            target.parent != self.config.modules_dir.resolve()
            or target.suffix.casefold() not in DOCUMENT_SOURCE_SUFFIXES
        ):
            raise ValueError(
                "module artifact must be PDF, Markdown, or text directly under artifacts/modules"
            )
        if not target.is_file():
            raise LookupError(name)
        return target

    def stage_module_asset(self, module_id: str, source_path: str | Path) -> dict[str, Any]:
        """Copy an allowlisted campaign asset into module-scoped managed storage."""
        if not re.fullmatch(r"[0-9a-fA-F-]{36}", module_id):
            raise ValueError("invalid module id for managed asset")
        source = Path(source_path).expanduser().resolve()
        if not source.is_file():
            raise LookupError(str(source))
        allowed = {
            ".gif",
            ".htm",
            ".html",
            ".jpeg",
            ".jpg",
            ".pdf",
            ".png",
            ".svg",
            ".txt",
            ".webp",
        }
        if source.suffix.casefold() not in allowed:
            raise ValueError("module asset must be an image, PDF, HTML, SVG, or text document")
        if not self.config.module_import_roots:
            raise PermissionError("no module import roots are configured")
        if not any(
            source.is_relative_to(root.resolve()) for root in self.config.module_import_roots
        ):
            raise PermissionError("module asset source is outside configured import roots")
        size = source.stat().st_size
        if size > 100 * 1024 * 1024:
            raise ValueError("module asset exceeds the 100 MiB safety limit")
        checksum = file_sha256(source)
        safe_name = re.sub(r"[^A-Za-z0-9._-]+", "-", source.name).strip("-.")
        safe_name = safe_name or f"asset{source.suffix.casefold()}"
        directory = (self.config.module_assets_dir / module_id).resolve()
        if directory.parent != self.config.module_assets_dir.resolve():
            raise ValueError("invalid managed module asset directory")
        directory.mkdir(parents=True, exist_ok=True)
        artifact = f"{checksum[:12]}-{safe_name}"
        target = (directory / artifact).resolve()
        if target.parent != directory:
            raise ValueError("invalid managed module asset name")
        if not target.exists():
            shutil.copy2(source, target)
        elif file_sha256(target) != checksum:
            raise RuntimeError("managed module asset checksum mismatch")
        media_type = guess_type(source.name)[0] or "application/octet-stream"
        return {
            "artifact": artifact,
            "path": str(target),
            "checksum": checksum,
            "size": size,
            "media_type": media_type,
            "staged": True,
        }

    def write_content_archive(
        self, package: dict[str, Any], blobs: dict[str, bytes]
    ) -> dict[str, Any]:
        """Persist any unified content package using the shared archive format."""
        return write_managed_content_archive(
            self.config.content_packages_dir,
            package,
            blobs,
        )

    def read_content_archive(
        self, *, artifact: str | None = None, source_path: str | Path | None = None
    ) -> tuple[dict[str, Any], dict[str, bytes]]:
        """Read one managed or allowlisted unified content package archive."""

        return read_managed_content_archive(
            self.config.content_packages_dir,
            artifact=artifact,
            source_path=source_path,
            allowed_roots=[
                *self.config.module_import_roots,
                *self.config.rule_import_roots,
            ],
        )

    def store_actor_image(self, asset: dict[str, Any], content: bytes) -> dict[str, Any]:
        """Store one immutable content-pack actor image by its verified checksum."""

        media_type = str(asset.get("media_type") or "").casefold()
        if not media_type.startswith("image/"):
            raise ValueError("actor image asset must use an image media type")
        checksum = str(asset.get("checksum") or "").casefold()
        if not re.fullmatch(r"[0-9a-f]{64}", checksum):
            raise ValueError("actor image asset requires a SHA-256 checksum")
        if hashlib.sha256(content).hexdigest() != checksum:
            raise ValueError("actor image asset checksum does not match its blob")
        target = (self.config.actor_images_dir / checksum).resolve()
        if target.parent != self.config.actor_images_dir.resolve():
            raise ValueError("invalid managed actor image path")
        if not target.exists():
            target.write_bytes(content)
        elif file_sha256(target) != checksum:
            raise RuntimeError("managed actor image checksum mismatch")
        return {
            "checksum": checksum,
            "media_type": media_type,
            "path": str(target),
        }

    def read_actor_image(self, checksum: str) -> bytes | None:
        """Read an immutable actor image without accepting an arbitrary path."""

        normalized = str(checksum or "").casefold()
        if not re.fullmatch(r"[0-9a-f]{64}", normalized):
            return None
        target = (self.config.actor_images_dir / normalized).resolve()
        if target.parent != self.config.actor_images_dir.resolve() or not target.is_file():
            return None
        if file_sha256(target) != normalized:
            raise RuntimeError("managed actor image checksum mismatch")
        return target.read_bytes()

    def store_content_module_asset(
        self, module_id: str, asset: dict[str, Any], content: bytes
    ) -> str:
        """Materialize one checksum-verified archive blob beneath module storage."""

        if not re.fullmatch(r"[0-9a-fA-F-]{36}", module_id):
            raise ValueError("invalid module id for portable asset")
        checksum = hashlib.sha256(content).hexdigest()
        if checksum != asset.get("checksum") or len(content) != asset.get("size"):
            raise ValueError("content module asset checksum or size mismatch")
        safe_name = re.sub(r"[^A-Za-z0-9._-]+", "-", str(asset.get("name") or "asset")).strip("-.")
        directory = (self.config.module_assets_dir / module_id).resolve()
        if directory.parent != self.config.module_assets_dir.resolve():
            raise ValueError("invalid content module asset directory")
        directory.mkdir(parents=True, exist_ok=True)
        target = (directory / f"{checksum[:12]}-{safe_name or 'asset'}").resolve()
        if target.parent != directory:
            raise ValueError("invalid content module asset name")
        if not target.exists():
            target.write_bytes(content)
        elif file_sha256(target) != checksum:
            raise RuntimeError("managed content module asset checksum mismatch")
        return str(target)

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
        filename = f"{source_checksum[:12]}-page-{page_number:04d}-x{scale_key}-{checksum[:12]}.png"
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
