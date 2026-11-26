from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from vid_forge import cli  # noqa: E402


class FilmCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.projects = Path(self.temp.name)
        self.project = self.projects / "sample"
        (self.project / "input").mkdir(parents=True)
        (self.project / "production").mkdir()
        (self.project / "input" / "brief.md").write_text("A seed grows into a tree.", encoding="utf-8")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_dry_run_lists_pipeline_without_credentials_or_stage_calls(self) -> None:
        with patch.object(cli, "PROJECTS_DIR", self.projects), patch.dict(os.environ, {}, clear=True), \
             patch.object(cli, "cmd_storyboard", side_effect=AssertionError("called API stage")):
            result = cli.cmd_film(["project=sample", "--dry-run"])
        self.assertEqual(result, 0)

    def test_full_run_calls_stages_in_order_and_stops_on_failure(self) -> None:
        credentials = {
            "GEMINI_API_KEY": "test",
            "CLOUDFLARE_WORKERS_AI_ACCOUNT_ID": "test",
            "CLOUDFLARE_WORKERS_AI_API_KEY": "test",
            "KAGGLE_API_TOKEN": "test",
            "KAGGLE_USERNAME": "test",
        }
        stage_names = ("cmd_storyboard", "cmd_validate", "cmd_keyframes", "cmd_narration", "cmd_video", "cmd_assemble")
        calls: list[str] = []

        def stage(name: str):
            def run(_args: list[str]) -> int:
                calls.append(name)
                return 1 if name == "cmd_video" else 0
            return run

        with patch.object(cli, "PROJECTS_DIR", self.projects), patch.dict(os.environ, credentials, clear=True), \
             patch.object(cli.shutil, "which", return_value="ffmpeg.exe"):
            patches = [patch.object(cli, name, side_effect=stage(name)) for name in stage_names]
            for mocked in patches:
                mocked.start()
            try:
                result = cli.cmd_film(["project=sample", "--candidate-count", "2", "--steps", "20"])
            finally:
                for mocked in reversed(patches):
                    mocked.stop()

        self.assertEqual(result, 1)
        self.assertEqual(calls, list(stage_names[:-1]))

    def test_invalid_candidate_count_fails_before_running_stages(self) -> None:
        with patch.object(cli, "PROJECTS_DIR", self.projects), \
             patch.object(cli, "cmd_storyboard", side_effect=AssertionError("stage should not run")):
            result = cli.cmd_film(["project=sample", "--candidate-count", "7"])
        self.assertEqual(result, 2)


if __name__ == "__main__":
    unittest.main()
