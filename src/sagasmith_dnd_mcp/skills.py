"""Read-only adapters for the D&D and module-generation skill repositories."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SkillDocument:
    id: str
    title: str
    source: str
    path: Path


class SkillCatalog:
    def __init__(self, *, dnd_root: Path, modulegen_root: Path) -> None:
        self._roots = {"dnd": dnd_root, "modulegen": modulegen_root}

    def list(self) -> list[SkillDocument]:
        documents: list[SkillDocument] = []
        for source, root in self._roots.items():
            if not root.is_dir():
                continue
            for path in sorted(root.rglob("SKILL.md")):
                relative = path.relative_to(root).parent
                suffix = "root" if relative == Path(".") else ".".join(relative.parts)
                documents.append(
                    SkillDocument(
                        id=f"{source}.{suffix}",
                        title=self._title(path, suffix),
                        source=source,
                        path=path,
                    )
                )
        return documents

    def get(self, skill_id: str) -> SkillDocument:
        for document in self.list():
            if document.id == skill_id:
                return document
        raise LookupError(f"unknown skill document {skill_id!r}")

    def read(self, skill_id: str) -> str:
        return self.get(skill_id).path.read_text(encoding="utf-8")

    @staticmethod
    def _title(path: Path, fallback: str) -> str:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("# "):
                return line[2:].strip()
        return fallback
