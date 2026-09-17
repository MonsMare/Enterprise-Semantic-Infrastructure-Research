import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from cacc_runner.run_record import ActiveRunError, RunJournal, RunRecord


def record(root: Path) -> RunRecord:
    run_dir = root / "runs" / "run-001"
    return RunRecord(
        run_id="run-001",
        package_id="KR-001",
        project_id="repo",
        package={"schema_version": "cacc-task-package/v2", "package_id": "KR-001"},
        status="running",
        worker_outcome=None,
        phase="starting",
        summary=None,
        session_id=None,
        process_id=4321,
        created_at="2026-09-16T00:00:00+00:00",
        updated_at="2026-09-16T00:00:00+00:00",
        review_note=None,
        report_path=str(run_dir / "report.json"),
        events_path=str(run_dir / "events.jsonl"),
        stderr_path=str(run_dir / "stderr.log"),
        exit_code=None,
    )


class RunJournalTests(unittest.TestCase):
    def test_active_record_is_exclusive_and_report_survives_slot_release(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            journal = RunJournal(root)
            active = record(root)

            journal.create(active)
            with self.assertRaises(ActiveRunError):
                journal.create(replace(active, run_id="run-002"))

            finished = replace(
                active,
                status="awaiting_codex_review",
                worker_outcome="completed",
                phase="finished",
                summary="测试通过",
            )
            journal.update(finished)
            journal.write_report(finished)
            journal.clear(active.run_id)

            self.assertIsNone(journal.read_active())
            report = journal.read_report(active.run_id)
            self.assertEqual(report.status, "awaiting_codex_review")
            self.assertEqual(report.worker_outcome, "completed")

    def test_recovery_marks_running_record_interrupted_and_keeps_slot(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            journal = RunJournal(root)
            journal.create(record(root))

            recovered = journal.recover_interrupted()

            self.assertEqual(recovered.status, "awaiting_codex_review")
            self.assertEqual(recovered.worker_outcome, "interrupted")
            self.assertEqual(journal.read_active().run_id, "run-001")


if __name__ == "__main__":
    unittest.main()
