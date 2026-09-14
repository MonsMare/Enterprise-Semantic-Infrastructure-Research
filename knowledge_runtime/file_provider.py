from __future__ import annotations

from pathlib import Path

from .memory_provider import MemoryProvider, _Resource


class FileProvider(MemoryProvider):
    provider_id = "file"

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        super().__init__({}, provider_id="file")
        self._refresh()

    def _refresh(self) -> None:
        documents = {
            str(path.relative_to(self.root)).replace("\\", "/"): path.read_text(encoding="utf-8", errors="replace")
            for path in self.root.rglob("*")
            if path.is_file() and path.suffix.lower() in {".md", ".markdown", ".txt"}
        }
        previous = self._resources
        self._resources = {}
        for resource_id, content in documents.items():
            old = previous.get(resource_id)
            if old is not None and old.content == content:
                self._resources[resource_id] = old
            else:
                self._resources[resource_id] = _Resource(
                    name=resource_id,
                    content=content,
                    media_type="text/markdown" if resource_id.lower().endswith((".md", ".markdown")) else "text/plain",
                )

    def list(self, **kwargs):
        self._refresh()
        return super().list(**kwargs)

    def find(self, *args, **kwargs):
        self._refresh()
        return super().find(*args, **kwargs)

    def search(self, *args, **kwargs):
        self._refresh()
        return super().search(*args, **kwargs)

    def read(self, *args, **kwargs):
        self._refresh()
        return super().read(*args, **kwargs)

    def stat(self, *args, **kwargs):
        self._refresh()
        return super().stat(*args, **kwargs)
