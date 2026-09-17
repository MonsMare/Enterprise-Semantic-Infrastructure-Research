"""C&CC Claude Code Bridge 的本地 stdio MCP 服务。"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import sys
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any, Mapping

from .claude_code import ClaudeCodeResult, ClaudeCodeWorker
from .config import RunnerConfig, load_config
from .run_record import ActiveRunError, RunJournal, RunRecord, RunRecordError, now_iso
from .task_package import parse_task_package


class RunnerServiceError(RuntimeError):
    """桥接器调用不满足生命周期或输入约束。"""


def _short_text(value: object, maximum: int = 4000) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text[:maximum] if text else None


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


class RunnerService:
    """将一个任务包桥接到一个 Claude Code CLI 进程。"""

    def __init__(self, settings: RunnerConfig, *, worker: ClaudeCodeWorker | Any | None = None) -> None:
        self.settings = settings
        self.journal = RunJournal(settings.data_dir)
        self.worker = worker or ClaudeCodeWorker(settings.claude_command, max_turns=settings.max_turns)
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._opened = False
        self._recovered_run_id: str | None = None

    def open(self) -> None:
        if self._opened:
            return
        recovered = self.journal.recover_interrupted()
        self._recovered_run_id = recovered.run_id if recovered and recovered.worker_outcome == "interrupted" else None
        self._opened = True

    async def aclose(self) -> None:
        if not self._opened:
            return
        tasks = list(self._tasks.items())
        for run_id, task in tasks:
            if task.done():
                continue
            try:
                await _maybe_await(self.worker.cancel(run_id))
            except Exception:
                task.cancel()
        if tasks:
            await asyncio.gather(*(task for _, task in tasks), return_exceptions=True)
        self._tasks.clear()
        self._opened = False

    def _require_open(self) -> None:
        if not self._opened:
            raise RunnerServiceError("桥接器尚未打开或已关闭")

    async def _check_worker_dependency(self) -> None:
        checker = getattr(self.worker, "ensure_available", None)
        if callable(checker):
            await _maybe_await(checker())

    @staticmethod
    def _record_dict(record: RunRecord) -> dict[str, Any]:
        return {
            "run_id": record.run_id,
            "package_id": record.package_id,
            "project_id": record.project_id,
            "status": record.status,
            "worker_outcome": record.worker_outcome,
            "phase": record.phase,
            "summary": record.summary,
            "session_id": record.session_id,
            "process_id": record.process_id,
            "created_at": record.created_at,
            "updated_at": record.updated_at,
            "review_note": record.review_note,
            "report_path": record.report_path,
            "events_path": record.events_path,
            "stderr_path": record.stderr_path,
            "exit_code": record.exit_code,
        }

    async def runner_status(self) -> dict[str, Any]:
        self._require_open()
        try:
            await self._check_worker_dependency()
            cli_available = True
        except Exception:
            cli_available = False
        active = self.journal.read_active()
        return {
            "service": "cacc_claude_code_bridge",
            "opened": True,
            "claude_command": self.settings.claude_command,
            "claude_cli_available": cli_available,
            "projects": sorted(self.settings.projects),
            "active_run": self._record_dict(active) if active else None,
            "active_worker_task": active is not None and active.run_id in self._tasks,
            "recovered_run_id": self._recovered_run_id,
        }

    def _run_directory(self, run_id: str) -> Path:
        return self.journal.data_dir / "runs" / run_id

    def _new_record(self, validated: Any, run_id: str, run_dir: Path) -> RunRecord:
        timestamp = now_iso()
        return RunRecord(
            run_id=run_id,
            package_id=validated.package.package_id,
            project_id=validated.package.project_id,
            package=asdict(validated.package),
            status="running",
            worker_outcome=None,
            phase="starting",
            summary="正在启动 Claude Code",
            session_id=None,
            process_id=None,
            created_at=timestamp,
            updated_at=timestamp,
            review_note=None,
            report_path=str((run_dir / "report.json").resolve(strict=False)),
            events_path=str((run_dir / "events.jsonl").resolve(strict=False)),
            stderr_path=str((run_dir / "stderr.log").resolve(strict=False)),
            exit_code=None,
        )

    async def start_package(self, package: Mapping[str, Any] | dict[str, Any]) -> dict[str, Any]:
        self._require_open()
        validated = parse_task_package(package, self.settings.projects)
        await self._check_worker_dependency()
        run_id = f"{validated.package.package_id}-{uuid.uuid4().hex[:12]}"
        run_dir = self._run_directory(run_id)
        run_dir.mkdir(parents=True, exist_ok=False)
        (run_dir / "task-package.json").write_text(
            json.dumps(dict(package), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        record = self._new_record(validated, run_id, run_dir)
        try:
            self.journal.create(record)
        except Exception:
            try:
                (run_dir / "task-package.json").unlink()
                run_dir.rmdir()
            except OSError:
                pass
            raise
        try:
            task = asyncio.create_task(
                self._execute_worker(record.run_id, validated, run_dir),
                name=f"cacc-claude-code-{record.run_id}",
            )
        except Exception:
            self.journal.clear(record.run_id)
            raise
        self._tasks[record.run_id] = task
        return self._record_dict(record)

    def _active_for(self, run_id: str) -> RunRecord | None:
        record = self.journal.read_active()
        return record if record and record.run_id == run_id else None

    async def _set_phase(self, run_id: str, phase: str, summary: str) -> None:
        record = self._active_for(run_id)
        if record is None or record.status != "running":
            return
        self.journal.update(record.with_update(phase=phase, summary=_short_text(summary)))

    async def _set_session(self, run_id: str, session_id: str) -> None:
        record = self._active_for(run_id)
        if record is None or record.status != "running":
            return
        self.journal.update(record.with_update(session_id=_short_text(session_id, 500)))

    def _finish_record(
        self,
        run_id: str,
        *,
        outcome: str,
        summary: str,
        result: ClaudeCodeResult | None = None,
    ) -> RunRecord | None:
        record = self._active_for(run_id)
        if record is None:
            return None
        finished = record.with_update(
            status="awaiting_codex_review",
            worker_outcome=outcome,
            phase="review",
            summary=_short_text(summary),
            session_id=(result.session_id if result and result.session_id else record.session_id),
            process_id=None,
            events_path=(str(result.events_path) if result else record.events_path),
            stderr_path=(str(result.stderr_path) if result else record.stderr_path),
            exit_code=(result.exit_code if result else record.exit_code),
        )
        self.journal.update(finished)
        self.journal.write_report(finished)
        return finished

    async def _execute_worker(self, run_id: str, validated: Any, run_dir: Path) -> None:
        try:
            result: ClaudeCodeResult = await self.worker.run_package(
                validated,
                run_id,
                run_dir,
                on_progress=lambda phase, summary: self._set_phase(run_id, phase, summary),
                on_session_id=lambda session_id: self._set_session(run_id, session_id),
            )
            self._finish_record(run_id, outcome=result.outcome, summary=result.summary, result=result)
        except asyncio.CancelledError:
            self._finish_record(
                run_id,
                outcome="interrupted",
                summary="桥接器在 Claude Code 完成前关闭；请由 Codex 审阅本地现场。",
            )
            raise
        except Exception as exc:
            self._finish_record(
                run_id,
                outcome="failed",
                summary=f"桥接器调用 Claude Code 失败：{_short_text(exc, 3000) or type(exc).__name__}",
            )
        finally:
            self._tasks.pop(run_id, None)

    @staticmethod
    def _recent_events(path: str, limit: int = 20) -> list[dict[str, str]]:
        try:
            lines = Path(path).read_text(encoding="utf-8").splitlines()[-limit:]
        except OSError:
            return []
        events: list[dict[str, str]] = []
        for line in lines:
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(data, dict):
                continue
            event_type = data.get("type")
            if event_type == "system" and data.get("subtype") == "init":
                summary = "Claude Code 已连接"
            elif event_type == "assistant":
                summary = "Claude Code 正在执行任务包"
            elif event_type == "result":
                raw = data.get("result") or data.get("error") or "Claude Code 返回最终结果"
                summary = _short_text(raw, 1000) or "Claude Code 返回最终结果"
            else:
                summary = f"Claude Code 事件：{event_type or 'unknown'}"
            events.append({"type": str(event_type or "unknown"), "summary": summary})
        return events

    async def get_package_status(self, run_id: str | None = None) -> dict[str, Any]:
        self._require_open()
        active = self.journal.read_active()
        if run_id is None:
            if active is None:
                raise KeyError("没有活动任务包；请指定 run_id 查询已完成任务")
            record = active
        elif active is not None and active.run_id == run_id:
            record = active
        else:
            record = self.journal.read_report(run_id)
        result = self._record_dict(record)
        result["events"] = self._recent_events(record.events_path)
        return result

    async def wait_for_run(self, run_id: str) -> dict[str, Any]:
        self._require_open()
        task = self._tasks.get(run_id)
        if task is not None:
            await asyncio.shield(task)
        return await self.get_package_status(run_id)

    async def cancel_package(self, run_id: str) -> dict[str, Any]:
        self._require_open()
        record = self._active_for(run_id)
        if record is None or record.status != "running":
            raise ActiveRunError("只可取消正在运行的任务包")
        task = self._tasks.get(run_id)
        if task is None:
            raise RunnerServiceError("该任务不属于当前桥接器进程，不能安全取消")
        await _maybe_await(self.worker.cancel(run_id))
        await asyncio.shield(task)
        return await self.get_package_status(run_id)

    async def accept_package(self, run_id: str, review_note: str | None = None) -> dict[str, Any]:
        self._require_open()
        record = self._active_for(run_id)
        if record is None or record.status != "awaiting_codex_review":
            raise ActiveRunError("只有等待 Codex 审阅的任务包可以验收")
        accepted = record.with_update(status="accepted", phase="accepted", review_note=_short_text(review_note))
        self.journal.write_report(accepted)
        self.journal.clear(run_id)
        return self._record_dict(accepted)


def create_mcp_server(service: RunnerService):
    """为已打开的桥接器创建 stdio MCP 服务。"""
    try:
        from mcp.server import MCPServer
    except ImportError as exc:
        raise RunnerServiceError("未安装 MCP Python SDK；请安装 cacc-runner。") from exc

    mcp = MCPServer("C&CC Claude Code Bridge")

    @mcp.tool()
    async def runner_status() -> dict[str, Any]:
        """显示 Claude Code CLI 可用性、已登记项目和当前唯一任务。"""
        return await service.runner_status()

    @mcp.tool()
    async def start_package(package: dict[str, Any]) -> dict[str, Any]:
        """校验并启动唯一的 cacc-task-package/v2 任务包。"""
        return await service.start_package(package)

    @mcp.tool()
    async def get_package_status(run_id: str | None = None) -> dict[str, Any]:
        """读取任务包的短状态、事件摘要和本地工件路径。"""
        return await service.get_package_status(run_id)

    @mcp.tool()
    async def cancel_package(run_id: str) -> dict[str, Any]:
        """结束当前 Claude Code 进程，不自动重试。"""
        return await service.cancel_package(run_id)

    @mcp.tool()
    async def accept_package(run_id: str, review_note: str | None = None) -> dict[str, Any]:
        """记录 Codex 已审阅任务包，并释放下一包资格。"""
        return await service.accept_package(run_id, review_note)

    return mcp


def main() -> int:
    config_value = os.environ.get("CACC_RUNNER_CONFIG")
    if not config_value:
        sys.stderr.write("CACC_RUNNER_CONFIG 必须指向本机 projects.toml 配置。\n")
        return 2
    try:
        service = RunnerService(load_config(config_value))
        service.open()
        mcp = create_mcp_server(service)
    except Exception as exc:
        sys.stderr.write(f"C&CC Claude Code Bridge 无法启动：{type(exc).__name__}: {exc}\n")
        return 2
    try:
        mcp.run()
    finally:
        asyncio.run(service.aclose())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["RunnerService", "RunnerServiceError", "create_mcp_server", "main"]
