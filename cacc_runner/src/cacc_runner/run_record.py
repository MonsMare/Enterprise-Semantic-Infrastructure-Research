"""单 Worker 的轻量 JSON 运行记录。"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class ActiveRunError(RuntimeError):
    """已有任务占用唯一 Claude Code Worker 槽位。"""


class RunRecordError(RuntimeError):
    """本地运行记录损坏或无法安全更新。"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class RunRecord:
    run_id: str
    package_id: str
    project_id: str
    package: dict[str, Any]
    status: str
    worker_outcome: str | None
    phase: str
    summary: str | None
    session_id: str | None
    process_id: int | None
    created_at: str
    updated_at: str
    review_note: str | None
    report_path: str
    events_path: str
    stderr_path: str
    exit_code: int | None

    def with_update(self, **changes: object) -> "RunRecord":
        return replace(self, updated_at=now_iso(), **changes)


def _record_from_data(data: object) -> RunRecord:
    if not isinstance(data, dict):
        raise RunRecordError("运行记录必须是对象")
    fields = set(RunRecord.__dataclass_fields__)
    if set(data) != fields:
        raise RunRecordError("运行记录字段不完整或包含未知字段")
    try:
        return RunRecord(**data)
    except (TypeError, ValueError) as exc:
        raise RunRecordError("运行记录字段无效") from exc


class RunJournal:
    """只持久化一个活动槽位和每次运行的最终报告。"""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir.resolve(strict=False)
        self.active_path = self.data_dir / "active-run.json"

    def _write_json(self, path: Path, record: RunRecord, *, exclusive: bool = False) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(asdict(record), ensure_ascii=False, indent=2) + "\n"
        if exclusive:
            try:
                with path.open("x", encoding="utf-8", newline="\n") as handle:
                    handle.write(payload)
            except FileExistsError as exc:
                raise ActiveRunError("已有任务包等待 Claude Code 完成或 Codex 验收") from exc
            return
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        temporary.write_text(payload, encoding="utf-8", newline="\n")
        temporary.replace(path)

    def _read_json(self, path: Path) -> RunRecord:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RunRecordError(f"无法读取运行记录：{path}") from exc
        return _record_from_data(data)

    def create(self, record: RunRecord) -> None:
        self._write_json(self.active_path, record, exclusive=True)

    def read_active(self) -> RunRecord | None:
        if not self.active_path.is_file():
            return None
        return self._read_json(self.active_path)

    def update(self, record: RunRecord) -> None:
        active = self.read_active()
        if active is None or active.run_id != record.run_id:
            raise ActiveRunError("活动槽位不存在或已属于其他任务包")
        self._write_json(self.active_path, record)

    def clear(self, run_id: str) -> None:
        active = self.read_active()
        if active is None or active.run_id != run_id:
            raise ActiveRunError("活动槽位不存在或已属于其他任务包")
        try:
            self.active_path.unlink()
        except FileNotFoundError as exc:
            raise ActiveRunError("活动槽位已被释放") from exc

    def write_report(self, record: RunRecord) -> None:
        self._write_json(Path(record.report_path), record)

    def read_report(self, run_id: str) -> RunRecord:
        if not run_id or any(part in {"", ".", ".."} for part in Path(run_id).parts):
            raise KeyError("run_id 无效")
        return self._read_json(self.data_dir / "runs" / run_id / "report.json")

    def recover_interrupted(self) -> RunRecord | None:
        active = self.read_active()
        if active is None:
            return None
        if active.status != "running":
            return active
        recovered = active.with_update(
            status="awaiting_codex_review",
            worker_outcome="interrupted",
            phase="interrupted",
            summary="桥接器在 Claude Code 完成前退出；请由 Codex 核对本地现场后验收。",
        )
        self.update(recovered)
        self.write_report(recovered)
        return recovered
