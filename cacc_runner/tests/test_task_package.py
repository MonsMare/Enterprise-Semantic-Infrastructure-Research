import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from cacc_runner.config import ProjectConfig
from cacc_runner.task_package import PackageValidationError, parse_task_package


def package(**overrides):
    data = {
        "schema_version": "cacc-task-package/v2",
        "package_id": "KR-001",
        "project_id": "repo",
        "goal": "实现一个小型改动",
        "allowed_paths": ["src/", "tests/"],
        "acceptance_criteria": ["行为正确", "测试通过"],
    }
    data.update(overrides)
    return data


class TaskPackageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root / "src").mkdir()
        (self.root / "tests").mkdir()
        self.projects = {"repo": ProjectConfig(project_id="repo", root=self.root)}

    def test_parses_v2_package_without_shell_check_fields(self):
        result = parse_task_package(package(), self.projects)

        self.assertEqual(result.package.schema_version, "cacc-task-package/v2")
        self.assertEqual(result.package.package_id, "KR-001")
        self.assertEqual(result.allowed_roots, ((self.root / "src").resolve(), (self.root / "tests").resolve()))
        self.assertFalse(hasattr(result.package, "checks"))

    def test_rejects_old_v1_and_removed_checks_field(self):
        with self.assertRaises(PackageValidationError):
            parse_task_package(package(schema_version="cacc-task-package/v1"), self.projects)
        with self.assertRaises(PackageValidationError):
            parse_task_package(package(checks=["unit"]), self.projects)

    def test_rejects_unregistered_project_empty_criteria_and_bad_paths(self):
        with self.assertRaises(PackageValidationError):
            parse_task_package(package(project_id="unknown"), self.projects)
        with self.assertRaises(PackageValidationError):
            parse_task_package(package(acceptance_criteria=[]), self.projects)
        for bad_path in ("../outside", str(self.root / "src"), "C:\\Windows\\system.ini", ""):
            with self.subTest(bad_path=bad_path):
                with self.assertRaises(PackageValidationError):
                    parse_task_package(package(allowed_paths=[bad_path]), self.projects)

    def test_rejects_credential_shaped_text_before_claude_starts(self):
        with self.assertRaises(PackageValidationError):
            parse_task_package(
                package(goal="请使用 api_key=unit-test-placeholder 完成改动"),
                self.projects,
            )


if __name__ == "__main__":
    unittest.main()
