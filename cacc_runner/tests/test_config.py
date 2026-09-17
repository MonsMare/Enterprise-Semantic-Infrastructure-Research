import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from cacc_runner.config import ConfigError, load_config


class ConfigTests(unittest.TestCase):
    def test_loads_claude_cli_settings_without_linear_or_check_commands(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            project_root = base / "repo"
            project_root.mkdir()
            config_path = base / "projects.toml"
            config_path.write_text(
                '[runner]\n'
                'data_dir = "./bridge-data"\n'
                'claude_command = "claude"\n'
                'max_turns = 17\n'
                '[projects."repo-a"]\n'
                'root = "./repo"\n',
                encoding="utf-8",
            )

            settings = load_config(config_path)

            self.assertEqual(settings.data_dir, (base / "bridge-data").resolve())
            self.assertEqual(settings.claude_command, "claude")
            self.assertEqual(settings.max_turns, 17)
            self.assertEqual(settings.projects["repo-a"].root, project_root.resolve())
            self.assertFalse(hasattr(settings, "linear_issue_id"))
            self.assertFalse(hasattr(settings.projects["repo-a"], "checks"))

    def test_rejects_removed_linear_and_check_configuration(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            (base / "repo").mkdir()
            config_path = base / "projects.toml"
            config_path.write_text(
                '[runner]\nlinear_issue_id = "issue-123"\n'
                '[projects.repo]\nroot = "./repo"\n'
                '[projects.repo.checks.unit]\nargv = ["pytest"]\n',
                encoding="utf-8",
            )
            with self.assertRaises(ConfigError):
                load_config(config_path)

    def test_rejects_empty_claude_command_and_invalid_turn_limit(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            (base / "repo").mkdir()
            config_path = base / "projects.toml"
            config_path.write_text(
                '[runner]\nclaude_command = ""\nmax_turns = 0\n'
                '[projects.repo]\nroot = "./repo"\n',
                encoding="utf-8",
            )
            with self.assertRaises(ConfigError):
                load_config(config_path)


if __name__ == "__main__":
    unittest.main()
