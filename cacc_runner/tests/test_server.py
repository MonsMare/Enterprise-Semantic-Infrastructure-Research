import asyncio
import sys
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from cacc_runner.claude_code import ClaudeCodeResult
from cacc_runner.config import ProjectConfig, RunnerConfig
from cacc_runner.run_record import ActiveRunError
from cacc_runner.server import RunnerService


def task_package(package_id="KR-001"):
    return {
        "schema_version": "cacc-task-package/v2",
        "package_id": package_id,
        "project_id": "repo",
        "goal": "实现一个可审阅的小改动",
        "allowed_paths": ["src/", "tests/"],
        "acceptance_criteria": ["行为符合预期", "相关测试通过"],
    }


@dataclass
class FakeWorker:
    started: asyncio.Event
    release: asyncio.Event
    cancelled: list[str]
    calls: list[tuple]
    outcome: str = "completed"
    summary: str = "完成最小改动并运行相关测试"

    def __init__(self):
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = []
        self.calls = []

    async def ensure_available(self):
        return None

    async def run_package(self, validated, run_id, run_dir, on_progress, on_session_id):
        self.calls.append((validated, run_id, run_dir))
        run_dir.mkdir(parents=True, exist_ok=True)
        events_path = run_dir / "events.jsonl"
        stderr_path = run_dir / "stderr.log"
        events_path.write_text("{}\n", encoding="utf-8")
        stderr_path.write_text("", encoding="utf-8")
        await on_session_id("claude-session-1")
        await on_progress("working", "Claude Code 正在执行任务包")
        self.started.set()
        await self.release.wait()
        return ClaudeCodeResult(
            outcome=self.outcome,
            summary=self.summary,
            session_id="claude-session-1",
            terminal_reason="success" if self.outcome == "completed" else "cancelled",
            duration_ms=20,
            num_turns=1,
            exit_code=0 if self.outcome == "completed" else 1,
            events_path=events_path,
            stderr_path=stderr_path,
        )

    async def cancel(self, run_id):
        self.cancelled.append(run_id)
        self.outcome = "cancelled"
        self.summary = "Codex 请求取消当前 Claude Code 任务"
        self.release.set()


class UnavailableWorker:
    async def ensure_available(self):
        raise RuntimeError("Claude Code 不可用")


class RunnerServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addAsyncCleanup(self._cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root / "src").mkdir()
        (self.root / "tests").mkdir()
        self.settings = RunnerConfig(
            data_dir=self.root / ".bridge-data",
            claude_command="claude",
            max_turns=40,
            projects={"repo": ProjectConfig("repo", self.root)},
        )
        self.worker = FakeWorker()
        self.service = RunnerService(self.settings, worker=self.worker)
        self.service.open()

    async def _cleanup(self):
        await self.service.aclose()
        self.temp.cleanup()

    async def _start_and_wait(self, package_id="KR-001"):
        started = await self.service.start_package(task_package(package_id))
        await self.worker.started.wait()
        return started

    async def test_one_worker_is_held_until_explicit_codex_acceptance(self):
        started = await self._start_and_wait()
        self.assertEqual(started["status"], "running")
        self.assertTrue(Path(started["report_path"]).is_absolute())
        with self.assertRaises(ActiveRunError):
            await self.service.start_package(task_package("KR-002"))

        self.worker.release.set()
        await self.service.wait_for_run(started["run_id"])
        review = await self.service.get_package_status(started["run_id"])
        self.assertEqual(review["status"], "awaiting_codex_review")
        self.assertEqual(review["worker_outcome"], "completed")
        with self.assertRaises(ActiveRunError):
            await self.service.start_package(task_package("KR-002"))

        accepted = await self.service.accept_package(started["run_id"], "已检查差异与测试结果")
        self.assertEqual(accepted["status"], "accepted")
        next_run = await self.service.start_package(task_package("KR-002"))
        self.assertEqual(next_run["status"], "running")

    async def test_cancel_waits_for_result_and_never_retries(self):
        started = await self._start_and_wait()

        result = await self.service.cancel_package(started["run_id"])

        self.assertEqual(self.worker.cancelled, [started["run_id"]])
        self.assertEqual(result["status"], "awaiting_codex_review")
        self.assertEqual(result["worker_outcome"], "cancelled")
        self.assertEqual(len(self.worker.calls), 1)

    async def test_runner_status_has_no_sdk_or_linear_key_state(self):
        status = await self.service.runner_status()

        self.assertEqual(status["service"], "cacc_claude_code_bridge")
        self.assertEqual(status["active_run"], None)
        self.assertNotIn("api_key", str(status).lower())
        self.assertNotIn("linear", str(status).lower())

    async def test_invalid_or_unavailable_work_never_claims_slot(self):
        invalid = task_package()
        invalid["schema_version"] = "cacc-task-package/v1"
        with self.assertRaises(ValueError):
            await self.service.start_package(invalid)
        self.assertIsNone(self.service.journal.read_active())

        unavailable = RunnerService(self.settings, worker=UnavailableWorker())
        unavailable.open()
        self.addAsyncCleanup(unavailable.aclose)
        with self.assertRaises(RuntimeError):
            await unavailable.start_package(task_package())
        self.assertIsNone(unavailable.journal.read_active())


if __name__ == "__main__":
    unittest.main()
