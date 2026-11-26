from __future__ import annotations

import contextlib
import importlib.util
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ASSEMBLY_DIR = Path(__file__).resolve().parents[1] / "pipeline" / "assembly"
sys.path.insert(0, str(ASSEMBLY_DIR))

import assemble  # noqa: E402


class AssemblyFilterTests(unittest.TestCase):
    def test_title_end_card_credits_and_narration_subtitle_offset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            landscape_srt = root / "narration_16x9.srt"
            portrait_srt = root / "narration_9x16.srt"
            cue = "1\n00:00:00,100 --> 00:00:00,600\nSubtitle\n"
            landscape_srt.write_text(cue, encoding="utf-8")
            portrait_srt.write_text(cue, encoding="utf-8")
            assets = assemble._write_card_assets(root, {
                "title": "Film title", "end_card_s": 4.0,
                "end_card_text": "Made with AI", "credits": "Directed by someone",
                "subtitles_path": landscape_srt,
                "subtitles_portrait_path": portrait_srt,
            }, 3.0)
            self.assertIn("Film title", assets["title"].read_text(encoding="utf-8"))
            self.assertIn("Made with AI", assets["end_card"].read_text(encoding="utf-8"))
            self.assertIn("Directed by someone", assets["credits"].read_text(encoding="utf-8"))
            shifted = assets["subtitles_16x9"].read_text(encoding="utf-8")
            self.assertIn("00:00:03,100 --> 00:00:03,600", shifted)
            for path in assets.values():
                path.unlink(missing_ok=True)

    def test_hard_cut_filter_graph_concatenates_in_input_order(self) -> None:
        graph, label = assemble.build_filter_graph(3, 24, [2.0, 3.0, 4.0], "cut", 0.5)
        self.assertEqual(label, "vout")
        self.assertIn("[v0][v1][v2]concat=n=3:v=1:a=0[vshots]", graph)
        self.assertLess(graph.index("[0:v:0]"), graph.index("[1:v:0]"))
        self.assertLess(graph.index("[1:v:0]"), graph.index("[2:v:0]"))

    def test_crossfade_graph_uses_cumulative_overlapped_offsets(self) -> None:
        graph, label = assemble.build_filter_graph(3, 24, [5.0, 6.0, 7.0], "crossfade", 0.5)
        self.assertEqual(label, "vout")
        self.assertIn("xfade=transition=fade:duration=0.500000:offset=5.000000", graph)
        self.assertIn("xfade=transition=fade:duration=0.500000:offset=11.000000", graph)

    def test_portrait_graph_uses_720_by_1280(self) -> None:
        graph, _ = assemble.build_filter_graph(
            2, 24, [2.0, 3.0], "cut", 0.5, end_card_duration=0.25,
            width=720, height=1280,
        )
        self.assertIn("scale=720:1280", graph)
        self.assertIn("color=c=black:s=720x1280", graph)

    def test_graph_keeps_missing_shot_slot_as_black_with_timeline_duration(self) -> None:
        graph, _ = assemble.build_filter_graph(
            3, 24, [2.0, 3.0, 4.0], "cut", 0.5,
            input_indexes=[0, None, 1],
        )
        self.assertIn("color=c=black:s=1280x720:r=24:d=3.000000", graph)
        self.assertIn("[0:v:0]", graph)
        self.assertIn("[1:v:0]", graph)

    def test_duration_calculation_counts_overlaps(self) -> None:
        self.assertEqual(assemble.calculate_duration([5.0, 6.0, 7.0], "cut", 0.5), 18.0)
        self.assertEqual(assemble.calculate_duration([5.0, 6.0, 7.0], "crossfade", 0.5), 17.0)

    def test_crossfade_cannot_consume_short_clip(self) -> None:
        with self.assertRaisesRegex(ValueError, "shorter than each adjacent clip"):
            assemble.build_filter_graph(2, 24, [0.5, 2.0], "crossfade", 0.5)


class AssemblyProjectTests(unittest.TestCase):
    def setUp(self) -> None:
        self.storyboard_validator = patch.object(assemble, "_validate_storyboard_file")
        self.storyboard_validator.start()
        self.addCleanup(self.storyboard_validator.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.project = Path(self.temp.name) / "sample"
        self.production = self.project / "production"
        self.shot_dir = self.project / "outputs" / "shots"
        self.production.mkdir(parents=True)
        self.shot_dir.mkdir(parents=True)
        self.shot_ids = ["shot_02", "shot_01"]  # deliberately not alphabetical
        (self.production / "storyboard.json").write_text(json.dumps({
            "meta": {"fps": 24, "title": "Sample film title"},
            "end_card": {"text": "Made with AI", "credits": "Directed by Sample", "duration_s": 0.5},
            "shots": [{"id": shot_id, "duration_s": 1} for shot_id in self.shot_ids],
        }), encoding="utf-8")
        for shot_id in self.shot_ids:
            (self.shot_dir / f"{shot_id}.mp4").write_bytes((shot_id.encode("ascii") + b"-source") * 100)
        narration = self.project / "outputs" / "narration"
        radio_cut = self.project / "outputs" / "radio_cut"
        narration.mkdir(parents=True)
        radio_cut.mkdir(parents=True)
        timing_shots = [
            {"id": shot_id, "duration_s": 1.5, "start_s": index * 1.5}
            for index, shot_id in enumerate(self.shot_ids)
        ]
        (narration / "timing.json").write_text(json.dumps({
            "total_s": 3.5, "end_card_s": 0.5, "shots": timing_shots,
        }), encoding="utf-8")
        (narration / "voice_track.wav").write_bytes(b"voice track")
        (narration / "narration_16x9.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\nHello\n", encoding="utf-8")
        (narration / "narration_9x16.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\nHello\n", encoding="utf-8")
        (radio_cut / "placeholder_music.wav").write_bytes(b"music track")
        self.info = {
            "duration_s": 2.0, "width": 640, "height": 360, "fps": 24.0,
            "codec": "h264", "pixel_format": "yuv420p", "audio_stream_count": 0,
            "file_size": 2048,
        }
        self.final_info = {
            "duration_s": 6.5, "width": 1280, "height": 720, "fps": 24.0,
            "codec": "h264", "pixel_format": "yuv420p", "audio_stream_count": 1,
            "file_size": 4096,
        }
        self.portrait_info = {**self.final_info, "width": 720, "height": 1280}

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _patch_tools(self):
        return (
            patch.object(assemble, "ffmpeg_path", return_value="ffmpeg"),
            patch.object(assemble, "ffprobe_path", return_value="ffprobe"),
        )

    def test_storyboard_order_and_dry_run_do_not_render_or_write(self) -> None:
        stdout = io.StringIO()
        tools = self._patch_tools()
        with tools[0], tools[1], \
             patch.object(assemble, "inspect_clip", return_value=self.info), \
             patch.object(assemble, "_run_ffmpeg", side_effect=AssertionError("dry run rendered")), \
             contextlib.redirect_stdout(stdout):
            report = assemble.assemble_project(self.project, dry_run=True)
        self.assertEqual(report["shot_ids"], ["shot_02", "shot_01"])
        self.assertEqual(report["available_clip_count"], 2)
        self.assertEqual(report["estimated_final_duration_s"], 6.5)
        self.assertLess(stdout.getvalue().index("shot_02: 2.000s"), stdout.getvalue().index("shot_01: 2.000s"))
        self.assertFalse((self.production / "final.mp4").exists())
        self.assertFalse((self.production / "assembly_manifest.json").exists())

    def test_missing_clips_are_reported_and_dry_run_uses_placeholders(self) -> None:
        timing_path = self.project / "outputs" / "narration" / "timing.json"
        (self.production / "storyboard.json").write_text(json.dumps({
            "meta": {"fps": 24}, "shots": [{"id": "shot_02"}, {"id": "shot_01"}, {"id": "shot_03"}],
        }), encoding="utf-8")
        (timing_path).write_text(json.dumps({
            "total_s": 5.0, "end_card_s": 0.5,
            "shots": [{"id": shot_id, "duration_s": 1.5} for shot_id in ("shot_02", "shot_01", "shot_03")],
        }), encoding="utf-8")
        (self.shot_dir / "shot_02.mp4").unlink()
        stdout = io.StringIO()
        stderr = io.StringIO()
        tools = self._patch_tools()
        with tools[0], tools[1], patch.object(assemble, "inspect_clip", return_value=self.info), \
             patch.object(assemble, "_run_ffmpeg", side_effect=AssertionError("dry run rendered")), \
             contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            report = assemble.assemble_project(self.project, dry_run=True)
        self.assertIn("shot_02, shot_03", stderr.getvalue())
        self.assertEqual(report["missing_shots"], ["shot_02", "shot_03"])
        self.assertEqual(report["valid_clip_count"], 1)
        self.assertEqual(report["estimated_final_duration_s"], 8.0)
        self.assertIn("black placeholders for 2 missing shot(s)", stdout.getvalue())

    def test_render_continues_with_available_clips_and_black_missing_slots(self) -> None:
        (self.shot_dir / "shot_02.mp4").unlink()
        graphs: list[str] = []

        def render(command: list[str], **_kwargs: object) -> None:
            graphs.append(command[command.index("-filter_complex") + 1])
            Path(command[-1]).write_bytes(b"assembled partial film" * 30)

        def inspect(path: Path, _ffprobe: str, _ffmpeg: str, **_kwargs: object):
            return self.portrait_info if path.name.startswith(".final_9x16.") else self.final_info

        tools = self._patch_tools()
        with tools[0], tools[1], patch.object(assemble, "inspect_clip", side_effect=inspect), \
             patch.object(assemble, "_run_ffmpeg", side_effect=render):
            report = assemble.assemble_project(self.project)

        self.assertEqual(report["missing_shots"], ["shot_02"])
        self.assertEqual(len(graphs), 2)
        self.assertIn("color=c=black:s=1280x720:r=24:d=1.500000", graphs[0])
        self.assertIn("color=c=black:s=720x1280:r=24:d=1.500000", graphs[1])
        manifest = json.loads((self.production / "assembly_manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["missing_shot_ids"], ["shot_02"])
        self.assertEqual(manifest["ordered_shot_ids"], self.shot_ids)

    def test_reports_invalid_clip_and_checks_remaining_inputs(self) -> None:
        checked: list[str] = []

        def inspect(path: Path, _ffprobe: str, _ffmpeg: str):
            checked.append(path.name)
            if path.name == "shot_02.mp4":
                raise ValueError("truncated video")
            return self.info

        stderr = io.StringIO()
        tools = self._patch_tools()
        with tools[0], tools[1], patch.object(assemble, "inspect_clip", side_effect=inspect), \
             contextlib.redirect_stderr(stderr), self.assertRaisesRegex(ValueError, "present storyboard clips are invalid"):
            assemble.assemble_project(self.project)
        self.assertEqual(checked, ["shot_02.mp4", "shot_01.mp4"])
        self.assertIn("shot_02", stderr.getvalue())
        self.assertIn("truncated video", stderr.getvalue())

    def test_existing_output_is_protected_without_force(self) -> None:
        output = self.production / "final.mp4"
        output.write_bytes(b"old final")
        tools = self._patch_tools()
        with tools[0], tools[1], patch.object(assemble, "inspect_clip", return_value=self.info), \
             patch.object(assemble, "_run_ffmpeg", side_effect=AssertionError("existing output rendered")), \
             self.assertRaisesRegex(FileExistsError, "--force"):
            assemble.assemble_project(self.project)
        self.assertEqual(output.read_bytes(), b"old final")

    def test_existing_portrait_output_also_requires_force(self) -> None:
        portrait = self.production / "final_9x16.mp4"
        portrait.write_bytes(b"old portrait")
        tools = self._patch_tools()
        with tools[0], tools[1], patch.object(assemble, "inspect_clip", return_value=self.info), \
             patch.object(assemble, "_run_ffmpeg", side_effect=AssertionError("existing output rendered")), \
             self.assertRaisesRegex(FileExistsError, "--force"):
            assemble.assemble_project(self.project)
        self.assertEqual(portrait.read_bytes(), b"old portrait")

    def test_force_renders_storyboard_order_and_writes_validated_manifest(self) -> None:
        output = self.production / "final.mp4"
        output.write_bytes(b"old final")
        rendered_commands: list[list[str]] = []

        def render(command: list[str], **_kwargs: object) -> None:
            rendered_commands.append(command)
            Path(command[-1]).write_bytes(b"new validated video" * 100)

        def inspect(path: Path, _ffprobe: str, _ffmpeg: str, **_kwargs: object):
            if path.name.startswith(".final_9x16."):
                return self.portrait_info
            return self.final_info if path.name.startswith(".final.") else self.info

        tools = self._patch_tools()
        with tools[0], tools[1], patch.object(assemble, "inspect_clip", side_effect=inspect), \
             patch.object(assemble, "_run_ffmpeg", side_effect=render):
            report = assemble.assemble_project(self.project, force=True, transition="crossfade", transition_duration=0.5)
        self.assertEqual(len(rendered_commands), 2)
        rendered_command, portrait_command = rendered_commands
        self.assertLess(rendered_command.index(str(self.shot_dir / "shot_02.mp4")),
                        rendered_command.index(str(self.shot_dir / "shot_01.mp4")))
        self.assertIn("-movflags", rendered_command)
        self.assertIn("+faststart", rendered_command)
        self.assertIn("[afinal]", rendered_command[rendered_command.index("-filter_complex") + 1])
        self.assertIn("[vfinal]", rendered_command[rendered_command.index("-filter_complex") + 1])
        self.assertIn("[titlecard]", rendered_command[rendered_command.index("-filter_complex") + 1])
        self.assertIn("[endcardtitle]", rendered_command[rendered_command.index("-filter_complex") + 1])
        self.assertIn("[vendcard]", rendered_command[rendered_command.index("-filter_complex") + 1])
        self.assertIn("setparams=range=limited[vfinal]", rendered_command[rendered_command.index("-filter_complex") + 1])
        self.assertIn("-color_range", rendered_command)
        self.assertIn("tv", rendered_command)
        self.assertIn("-itsoffset", rendered_command)
        self.assertIn("3.000", rendered_command)
        self.assertIn(str(self.project / "outputs" / "narration" / "voice_track.wav"), rendered_command)
        self.assertIn("scale=720:1280", portrait_command[portrait_command.index("-filter_complex") + 1])
        self.assertIn(".assembly-narration-9x16-", portrait_command[portrait_command.index("-filter_complex") + 1])
        self.assertEqual(report["actual_final_duration_s"], 6.5)
        self.assertNotEqual(output.read_bytes(), b"old final")
        portrait = self.production / "final_9x16.mp4"
        self.assertTrue(portrait.is_file())
        manifest = json.loads((self.production / "assembly_manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["ordered_shot_ids"], self.shot_ids)
        self.assertEqual(manifest["opening_title_card"]["text"], "Sample film title")
        self.assertEqual(manifest["end_card"]["credits"], "Directed by Sample")
        self.assertEqual(manifest["transition"]["type"], "crossfade")
        self.assertEqual(manifest["outputs"]["16x9"]["codec"], "h264")
        self.assertEqual(manifest["outputs"]["9x16"]["width"], 720)
        self.assertEqual(manifest["outputs"]["9x16"]["height"], 1280)
        self.assertTrue(manifest["validation"]["full_decode"])

    def test_failed_render_preserves_existing_output_and_writes_no_manifest(self) -> None:
        output = self.production / "final.mp4"
        output.write_bytes(b"previous final")
        portrait = self.production / "final_9x16.mp4"
        portrait.write_bytes(b"previous portrait")

        def fail_portrait_render(command: list[str], **_kwargs: object) -> None:
            temporary_output = Path(command[-1])
            if temporary_output.name.startswith(".final_9x16."):
                raise RuntimeError("portrait FFmpeg test failure")
            temporary_output.write_bytes(b"rendered landscape temp")

        def inspect(path: Path, _ffprobe: str, _ffmpeg: str, **_kwargs: object):
            return self.final_info if path.name.startswith(".final.") else self.portrait_info

        tools = self._patch_tools()
        with tools[0], tools[1], patch.object(assemble, "inspect_clip", side_effect=inspect), \
             patch.object(assemble, "_run_ffmpeg", side_effect=fail_portrait_render), \
             self.assertRaisesRegex(RuntimeError, "portrait FFmpeg test failure"):
            assemble.assemble_project(self.project, force=True)
        self.assertEqual(output.read_bytes(), b"previous final")
        self.assertEqual(portrait.read_bytes(), b"previous portrait")
        self.assertFalse((self.production / "assembly_manifest.json").exists())
        self.assertEqual(list(self.production.glob(".final.*.tmp.mp4")), [])
        self.assertEqual(list(self.production.glob(".final_9x16.*.tmp.mp4")), [])

    def test_failed_output_validation_preserves_previous_final(self) -> None:
        output = self.production / "final.mp4"
        output.write_bytes(b"previous final")
        portrait = self.production / "final_9x16.mp4"
        portrait.write_bytes(b"previous portrait")

        def render(command: list[str], **_kwargs: object) -> None:
            Path(command[-1]).write_bytes(b"invalid final" * 100)

        invalid_final = {**self.final_info, "width": 640}
        def inspect(path: Path, _ffprobe: str, _ffmpeg: str, **_kwargs: object):
            return invalid_final if path.name.startswith(".final.") else self.info

        tools = self._patch_tools()
        with tools[0], tools[1], patch.object(assemble, "inspect_clip", side_effect=inspect), \
             patch.object(assemble, "_run_ffmpeg", side_effect=render), \
             self.assertRaisesRegex(ValueError, "final resolution"):
            assemble.assemble_project(self.project, force=True)
        self.assertEqual(output.read_bytes(), b"previous final")
        self.assertEqual(portrait.read_bytes(), b"previous portrait")
        self.assertFalse((self.production / "assembly_manifest.json").exists())

    def test_output_filename_cannot_escape_production_directory(self) -> None:
        with self.assertRaisesRegex(ValueError, "simple .mp4 filename"):
            assemble._output_path(self.project, "../outside.mp4")


class AssemblyCliTests(unittest.TestCase):
    def test_cli_dispatches_assembly_options(self) -> None:
        from vid_forge import cli

        with tempfile.TemporaryDirectory() as tmp:
            projects = Path(tmp) / "projects"
            project = projects / "sample"
            project.mkdir(parents=True)
            fake_module = Mock()
            fake_module.run_assembly.return_value = 7
            with patch.object(cli, "PROJECTS_DIR", projects), patch.object(cli, "_load_module", return_value=fake_module) as load:
                result = cli.main([
                    "assemble", "project=sample", "--transition", "crossfade",
                    "--transition-duration", "0.75", "--output", "final-v2.mp4", "--dry-run", "--force",
                ])
        self.assertEqual(result, 7)
        self.assertEqual(load.call_args.args[0], cli.FILM_ASSEMBLY_SCRIPT)
        kwargs = fake_module.run_assembly.call_args.kwargs
        self.assertEqual(kwargs["transition"], "crossfade")
        self.assertEqual(kwargs["transition_duration"], 0.75)
        self.assertEqual(kwargs["output_name"], "final-v2.mp4")
        self.assertTrue(kwargs["dry_run"])
        self.assertTrue(kwargs["force"])


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg/ffprobe not installed")
class AssemblyIntegrationTests(unittest.TestCase):
    def test_real_ffmpeg_crossfade_outputs_valid_mp4(self) -> None:
        ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
        assert ffmpeg and ffprobe
        encoder_check = subprocess.run([ffmpeg, "-hide_banner", "-encoders"], capture_output=True, text=True, check=False)
        if "libx264" not in encoder_check.stdout:
            self.skipTest("FFmpeg lacks libx264")
        filters_check = subprocess.run([ffmpeg, "-hide_banner", "-filters"], capture_output=True, text=True, check=False)
        if " subtitles " not in filters_check.stdout:
            self.skipTest("FFmpeg lacks the subtitles/libass filter")
        with tempfile.TemporaryDirectory() as tmp:
            validator_patch = patch.object(assemble, "_validate_storyboard_file")
            validator_patch.start()
            self.addCleanup(validator_patch.stop)
            project = Path(tmp) / "sample"
            (project / "production").mkdir(parents=True)
            shots = project / "outputs" / "shots"
            shots.mkdir(parents=True)
            for shot_id, color in (("shot_02", "red"), ("shot_01", "blue")):
                subprocess.run([
                    ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                    f"color=c={color}:s=160x90:r=24:d=0.8", "-an", "-c:v", "libx264",
                    "-pix_fmt", "yuv420p", str(shots / f"{shot_id}.mp4"),
                ], capture_output=True, text=True, check=True)
            (project / "production" / "storyboard.json").write_text(json.dumps({
                "meta": {"fps": 24}, "shots": [{"id": "shot_02"}, {"id": "shot_01"}],
            }), encoding="utf-8")
            narration = project / "outputs" / "narration"
            radio = project / "outputs" / "radio_cut"
            narration.mkdir(parents=True)
            radio.mkdir(parents=True)
            for audio_path in (narration / "voice_track.wav", radio / "placeholder_music.wav"):
                subprocess.run([
                    ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                    "anullsrc=r=48000:cl=stereo", "-t", "1.8", str(audio_path),
                ], capture_output=True, text=True, check=True)
            (narration / "narration_16x9.srt").write_text(
                "1\n00:00:00,100 --> 00:00:00,600\nTest subtitle\n", encoding="utf-8",
            )
            (narration / "narration_9x16.srt").write_text(
                "1\n00:00:00,100 --> 00:00:00,600\nTest subtitle\n", encoding="utf-8",
            )
            (narration / "timing.json").write_text(json.dumps({
                "total_s": 1.8, "end_card_s": 0.2,
                "shots": [
                    {"id": "shot_02", "duration_s": 0.8},
                    {"id": "shot_01", "duration_s": 0.8},
                ],
            }), encoding="utf-8")
            report = assemble.assemble_project(
                project, transition="crossfade", transition_duration=0.2,
            )
            self.assertTrue((project / "production" / "final.mp4").is_file())
            self.assertTrue((project / "production" / "final_9x16.mp4").is_file())
            self.assertAlmostEqual(report["actual_final_duration_s"], 4.8, delta=1.0 / 24)
            self.assertTrue((project / "production" / "assembly_manifest.json").is_file())


if __name__ == "__main__":
    unittest.main()
