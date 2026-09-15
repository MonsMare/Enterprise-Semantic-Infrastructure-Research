from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Protocol

from .contracts import ArtifactRef, object_name


def artifact_key(document_id: str, revision_id: str, name: str) -> str:
    return f"documents/{document_id}/revisions/{revision_id}/{object_name(name)}"


class ArtifactStore(Protocol):
    def put_bytes(self, *, document_id: str, revision_id: str, name: str, data: bytes, media_type: str) -> ArtifactRef: ...

    def get_bytes(self, ref: ArtifactRef) -> bytes: ...

    def head(self, ref: ArtifactRef) -> ArtifactRef: ...

    def delete(self, ref: ArtifactRef, *, retention_token: str | None = None) -> None: ...


class FilesystemArtifactStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        path = self.root.joinpath(*key.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def put_bytes(self, *, document_id: str, revision_id: str, name: str, data: bytes, media_type: str) -> ArtifactRef:
        key = artifact_key(document_id, revision_id, name)
        digest = hashlib.sha256(data).hexdigest()
        path = self._path(key)
        if path.exists():
            existing = path.read_bytes()
            if hashlib.sha256(existing).hexdigest() != digest:
                raise ValueError("artifact is immutable and already exists with a different hash")
        else:
            path.write_bytes(data)
        return ArtifactRef(
            artifact_id=f"artifact-{digest[:24]}",
            object_key=key,
            media_type=media_type,
            sha256=digest,
            size_bytes=len(data),
            kind=object_name(name),
            revision_id=revision_id,
        )

    def get_bytes(self, ref: ArtifactRef) -> bytes:
        data = self._path(ref.object_key).read_bytes()
        if hashlib.sha256(data).hexdigest() != ref.sha256:
            raise ValueError("artifact hash verification failed")
        return data

    def head(self, ref: ArtifactRef) -> ArtifactRef:
        data = self._path(ref.object_key).read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        if digest != ref.sha256:
            raise ValueError("artifact hash verification failed")
        return ArtifactRef(
            artifact_id=ref.artifact_id,
            object_key=ref.object_key,
            media_type=ref.media_type,
            sha256=digest,
            size_bytes=len(data),
            kind=ref.kind,
            revision_id=ref.revision_id,
        )

    def delete(self, ref: ArtifactRef, *, retention_token: str | None = None) -> None:
        if retention_token != "ALLOW_RETENTION_DELETE":
            raise PermissionError("artifact deletion requires an explicit retention token")
        self._path(ref.object_key).unlink(missing_ok=True)


class S3ArtifactStore:
    def __init__(self, *, client: Any, bucket: str) -> None:
        if not bucket:
            raise ValueError("bucket is required")
        self.client = client
        self.bucket = bucket

    def put_bytes(self, *, document_id: str, revision_id: str, name: str, data: bytes, media_type: str) -> ArtifactRef:
        key = artifact_key(document_id, revision_id, name)
        digest = hashlib.sha256(data).hexdigest()
        try:
            existing = self.client.head_object(Bucket=self.bucket, Key=key)
        except Exception:
            existing = None
        if existing is not None:
            existing_hash = str(existing.get("Metadata", {}).get("sha256", ""))
            if existing_hash and existing_hash != digest:
                raise ValueError("artifact is immutable and already exists with a different hash")
            if not existing_hash:
                current = self.client.get_object(Bucket=self.bucket, Key=key)["Body"].read()
                if hashlib.sha256(current).hexdigest() != digest:
                    raise ValueError("artifact is immutable and already exists with a different hash")
        else:
            self.client.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=data,
                ContentType=media_type,
                Metadata={"sha256": digest},
            )
        return ArtifactRef(
            artifact_id=f"artifact-{digest[:24]}",
            object_key=key,
            media_type=media_type,
            sha256=digest,
            size_bytes=len(data),
            kind=object_name(name),
            revision_id=revision_id,
        )

    def get_bytes(self, ref: ArtifactRef) -> bytes:
        data = self.client.get_object(Bucket=self.bucket, Key=ref.object_key)["Body"].read()
        if hashlib.sha256(data).hexdigest() != ref.sha256:
            raise ValueError("artifact hash verification failed")
        return data

    def head(self, ref: ArtifactRef) -> ArtifactRef:
        response = self.client.head_object(Bucket=self.bucket, Key=ref.object_key)
        size = int(response.get("ContentLength", ref.size_bytes))
        return ArtifactRef(
            artifact_id=ref.artifact_id,
            object_key=ref.object_key,
            media_type=ref.media_type,
            sha256=str(response.get("Metadata", {}).get("sha256", ref.sha256)),
            size_bytes=size,
            kind=ref.kind,
            revision_id=ref.revision_id,
        )

    def delete(self, ref: ArtifactRef, *, retention_token: str | None = None) -> None:
        if retention_token != "ALLOW_RETENTION_DELETE":
            raise PermissionError("artifact deletion requires an explicit retention token")
        self.client.delete_object(Bucket=self.bucket, Key=ref.object_key)

