from __future__ import annotations

import ast
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import os
import shutil
import threading
import time
import unittest
from unittest.mock import patch

STAGE = Path(__file__).resolve().parents[1] / "pipeline" / "video-assemble"
sys.path.insert(0, str(STAGE))

from providers import HunyuanVideo15Provider, VideoRequest, Wan22Provider  # noqa: E402
from video_cache import VideoCache, fingerprint  # noqa: E402
import video_assemble  # noqa: E402
import video_worker  # noqa: E402


class WorkerSetupTests(unittest.TestCase):
    def test_pipeline_uses_sequential_cpu_offload_and_vae_tiling(self) -> None:
        class FakeVae:
            def __init__(self) -> None:
                self.tiling_enabled = False

            @classmethod
            def from_pretrained(cls, *args: object, **kwargs: object) -> "FakeVae":
                cls.load_args = (args, kwargs)
                return cls()

            def enable_tiling(self) -> None:
                self.tiling_enabled = True

        class FakePipeline:
            def __init__(self) -> None:
                self.vae = FakeVae()
                self.offload_enabled = False

            @classmethod
            def from_pretrained(cls, *args: object, **kwargs: object) -> "FakePipeline":
                cls.load_args = (args, kwargs)
                return cls()

            def enable_model_cpu_offload(self) -> None:
                self.offload_enabled = True

        import types
        diffusers = types.ModuleType("diffusers")
        diffusers.AutoencoderKLWan = FakeVae
        diffusers.WanImageToVideoPipeline = FakePipeline
        torch = type("Torch", (), {"float32": "fp32", "float16": "fp16"})
        with patch.dict(sys.modules, {"diffusers": diffusers}):
            pipeline = video_worker._load_pipeline("model/id", "revision", Path("cache"), torch)
        self.assertTrue(pipeline.offload_enabled)
        self.assertTrue(pipeline.vae.tiling_enabled)
        self.assertEqual(FakePipeline.load_args[1]["torch_dtype"], "fp16")
        self.assertEqual(FakeVae.load_args[1]["torch_dtype"], "fp32")

    def test_setup_flags_and_dependencies_precede_torch_import(self) -> None:
        source = (STAGE / "video_worker.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        main = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "main")
        install_line = next(
            node.lineno for node in ast.walk(main)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id == "_install_diffusers_requirements"
        )
        torch_import_line = next(
            node.lineno for node in ast.walk(main)
            if isinstance(node, ast.Import) and any(alias.name == "torch" for alias in node.names)
        )
        self.assertLess(install_line, torch_import_line)
        for flag in ("CUDA_VISIBLE_DEVICES", "USE_TF", "USE_FLAX"):
            flag_line = next(
                node.lineno for node in ast.walk(main)
                if isinstance(node, ast.Assign)
                and any(
                    isinstance(target, ast.Subscript)
                    and isinstance(target.value, ast.Attribute)
                    and isinstance(target.value.value, ast.Name)
                    and target.value.value.id == "os"
                    and target.value.attr == "environ"
                    and isinstance(target.slice, ast.Constant)
                    and target.slice.value == flag
                    for target in node.targets
                )
            )
            self.assertLess(flag_line, torch_import_line)
        self.assertLess(source.index('os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF"'), source.index("import torch"))
        self.assertIn("enable_model_cpu_offload", source)
        self.assertIn("enable_tiling", source)
        self.assertIn('generator=torch.Generator(device="cpu")', source)

def make_request(**changes: object) -> VideoRequest:
    values: dict[str, object] = {
        "shot_id": "shot_01", "image": "not-in-fingerprint.png", "prompt": "gentle motion",
        "negative_prompt": "blur", "seed": 9, "duration_s": 5.0, "fps": 24,
        "width": 1280, "height": 720, "frame_count": 121, "steps": 30,
        "guidance": 5.0, "sampling_solver": "unipc", "sampling_shift": 5.0,
        "provider": "wan22", "model": "Wan-AI/Wan2.2-TI2V-5B-Diffusers",
        "model_revision": Wan22Provider.model_revision,
        "implementation_revision": Wan22Provider.implementation_revision, "image_sha256": "ab" * 32,
    }
    values.update(changes)
    return VideoRequest(**values)  # type: ignore[arg-type]


class FingerprintTests(unittest.TestCase):
    def test_same_request_and_key_order_are_stable(self) -> None:
        request = make_request().fingerprint_payload()
        self.assertEqual(fingerprint(request), fingerprint(dict(reversed(list(request.items())))))

    def test_generation_inputs_change_key(self) -> None:
        original = make_request().fingerprint_payload()
        variants = [
            make_request(image_sha256="cd" * 32), make_request(prompt="different"),
            make_request(seed=10), make_request(model_revision="other"),
            make_request(width=1920),
        ]
        self.assertTrue(all(fingerprint(item.fingerprint_payload()) != fingerprint(original) for item in variants))

    def test_paths_and_runtime_metadata_are_excluded(self) -> None:
        first = make_request(image="C:/one/image.png").fingerprint_payload()
        second = make_request(image="/tmp/other/image.png").fingerprint_payload()
        self.assertEqual(fingerprint(first), fingerprint(second))
        self.assertNotIn("timestamp", first)
        self.assertNotIn("kaggle_job_id", first)

    def test_wan_enforces_frame_constraint_and_hunyuan_is_explicitly_unavailable(self) -> None:
        Wan22Provider().validate(make_request())
        with self.assertRaisesRegex(ValueError, r"4n\+1"):
            Wan22Provider().validate(make_request(frame_count=120))
        with self.assertRaisesRegex(NotImplementedError, "not implemented"):
            HunyuanVideo15Provider().validate(make_request(provider="hunyuan15"))


class CacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.project = Path(self.temp.name)
        self.cache = VideoCache(self.project)
        self.request = make_request().fingerprint_payload()
        self.key = fingerprint(self.request)
        self.source = self.project / "generated.mp4"
        self.source.write_bytes(b"example mp4 payload" * 100)
        self.probe = {"duration_s": 5.0, "width": 1280, "height": 720, "fps": 24.0,
                      "codec": "h264", "pixel_format": "yuv420p", "file_size": self.source.stat().st_size,
                      "decodable": True, "video_stream": True}

    def tearDown(self) -> None:
        self.temp.cleanup()

    def commit(self, force: bool = False) -> Path:
        with patch("video_cache.probe_video", return_value=self.probe):
            path, _ = self.cache.commit(self.key, self.request, self.source, {"shot_id": "shot_01"}, force=force)
        return path

    def test_valid_entry_is_a_hit(self) -> None:
        self.commit()
        with patch("video_cache.probe_video", return_value=self.probe):
            hit, metadata, reason = self.cache.validate(self.key)
        self.assertTrue(hit, reason)
        self.assertIsNotNone(metadata)

    def test_missing_checksum_or_incomplete_artifact_is_a_miss(self) -> None:
        self.commit()
        video = self.cache.entry_dir(self.key) / "video.mp4"
        video.unlink()
        hit, _, _ = self.cache.validate(self.key)
        self.assertFalse(hit)
        directory = self.cache.entry_dir(self.key)
        video.write_bytes(b"x" * 2000)
        (directory / "request.json").write_text(json.dumps(self.request), encoding="utf-8")
        (directory / "metadata.json").write_text(json.dumps({"fingerprint": self.key, "status": "running"}), encoding="utf-8")
        hit, _, _ = self.cache.validate(self.key)
        self.assertFalse(hit)

    def test_corrupted_video_is_a_miss(self) -> None:
        path = self.commit()
        path.write_bytes(b"corrupted" * 300)
        hit, _, reason = self.cache.validate(self.key)
        self.assertFalse(hit)
        self.assertIn("checksum", str(reason))

    def test_force_preserves_old_immutable_artifact(self) -> None:
        original = self.commit()
        forced = self.commit(force=True)
        self.assertTrue(original.is_file())
        self.assertTrue(forced.is_file())
        self.assertNotEqual(original.parent, forced.parent)
        self.assertEqual(len(list(self.cache.entry_dir(self.key).parent.iterdir())), 2)

    def test_lock_is_scoped_to_fingerprint(self) -> None:
        first, second = self.cache.lock(self.key), self.cache.lock("f" * 64)
        self.assertNotEqual(str(first.lock_file), str(second.lock_file))

    def test_same_fingerprint_lock_serializes_threads(self) -> None:
        acquired = threading.Event()
        with self.cache.lock(self.key):
            def take_lock() -> None:
                with self.cache.lock(self.key):
                    acquired.set()
            worker = threading.Thread(target=take_lock)
            worker.start()
            time.sleep(0.05)
            self.assertFalse(acquired.is_set())
        worker.join(timeout=2)
        self.assertTrue(acquired.is_set())


class PipelineTests(unittest.TestCase):
    def test_only_manifest_selected_keyframe_is_resolved_and_dry_run_has_no_remote_call(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "sample"
            key_dir = project / "production" / "keyframes" / "shot_01"
            key_dir.mkdir(parents=True)
            (key_dir / "candidate_01.jpg").write_bytes(b"first candidate")
            (key_dir / "candidate_02.jpg").write_bytes(b"selected candidate")
            (project / "production" / "storyboard.json").write_text(json.dumps({
                "meta": {"fps": 24}, "style": {"prefix": "film look", "negative": "blur", "characters": {}},
                "shots": [{"id": "shot_01", "purpose": "A scene", "duration_s": 5,
                           "keyframe": {"selected": 2, "seed": 12},
                           "motion": {"type": "i2v", "prompt": "camera drifts", "seed": 13}}],
            }), encoding="utf-8")
            (project / "production" / "keyframes.json").write_text(json.dumps({"shots": {"shot_01": {"candidates": {
                "1": {"path": "projects/sample/production/keyframes/shot_01/candidate_01.jpg", "success": True},
                "2": {"path": "projects/sample/production/keyframes/shot_01/candidate_02.jpg", "success": True},
            }}}}), encoding="utf-8")
            with patch.object(video_assemble, "_kaggle_batch", side_effect=AssertionError("dry run called Kaggle")):
                result = video_assemble.run_video(project, dry_run=True)
            self.assertEqual(result, 0)
            self.assertEqual(video_assemble._selected_keyframe(
                project, {"id": "shot_01", "keyframe": {"selected": 2}},
                json.loads((project / "production" / "keyframes.json").read_text()), None,
            ).name, "candidate_02.jpg")

    def test_missing_selected_manifest_record_fails_actionably(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "not marked successful"):
                video_assemble._selected_keyframe(Path(tmp), {"id": "shot_01", "keyframe": {"selected": 1}}, {}, None)

    def test_local_motion_fingerprint_is_separate_from_i2v(self) -> None:
        request = make_request(motion_parameters={"camera": "zoom_in"})
        self.assertNotEqual(fingerprint(request.fingerprint_payload()),
                            fingerprint(make_request(motion_parameters={"camera": "zoom_out"}).fingerprint_payload()))


class KaggleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.image = self.root / "selected.jpg"
        self.image.write_bytes(b"selected keyframe")
        self.request = make_request(image=str(self.image))
        self.calls: list[list[str]] = []

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_missing_credentials_fail_before_cli_use(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "KAGGLE_API_TOKEN, KAGGLE_USERNAME"):
                video_assemble._kaggle_environment()

    def _run_batch(self, *, existing: bool = False, kernel_status: list[str] | None = None,
                   download_failure: bool = False, timeout: int = 60) -> dict[str, object]:
        statuses = list(kernel_status or ["KernelWorkerStatus.RUNNING", "KernelWorkerStatus.COMPLETE"])

        def run_kaggle(args: list[str], env: dict[str, str], timeout: int = 1200) -> str:
            self.calls.append(args)
            if args[:2] == ["kernels", "push"]:
                return "Kernel version 7 successfully pushed"
            if args[:2] == ["kernels", "status"]:
                return statuses.pop(0)
            if args[:2] == ["kernels", "output"]:
                if download_failure:
                    raise RuntimeError("mock output download failure")
                target = Path(args[args.index("-p") + 1]) / "results"
                target.mkdir(parents=True)
                (target / "results.json").write_text(json.dumps({"jobs": [{"shot_id": "shot_01", "status": "completed", "output": "shot_01.mp4"}]}), encoding="utf-8")
                return "downloaded"
            return "ok"

        with patch.object(video_assemble, "_kaggle_environment", return_value={"KAGGLE_USERNAME": "test-user"}), \
             patch.object(video_assemble, "_run_kaggle", side_effect=run_kaggle), \
             patch.object(video_assemble.subprocess, "run", return_value=type("R", (), {"returncode": 0})()), \
             patch.object(video_assemble.time, "sleep", return_value=None):
            if kernel_status and kernel_status[0].endswith("FAILED"):
                with self.assertRaisesRegex(RuntimeError, "Kaggle kernel failed"):
                    video_assemble._kaggle_batch(self.root / "sample", [(self.request, {})], "NvidiaTeslaA100", "1ea34ff", timeout, {})
                return {}
            if download_failure:
                with self.assertRaisesRegex(RuntimeError, "download failure"):
                    video_assemble._kaggle_batch(self.root / "sample", [(self.request, {})], "NvidiaTeslaA100", "1ea34ff", timeout, {})
                return {}
            # Return code controls whether datasets status finds an existing dataset.
            if existing:
                # Already patched to 0 by default.
                pass
            else:
                video_assemble.subprocess.run.return_value.returncode = 1
            return video_assemble._kaggle_batch(self.root / "sample", [(self.request, {})], "NvidiaTeslaA100", "1ea34ff", timeout, {})

    def test_private_dataset_create_and_versioned_kernel_output(self) -> None:
        result = self._run_batch()
        self.assertEqual(result["kernel_version"], 7)
        output = next(call for call in self.calls if call[:2] == ["kernels", "output"])
        self.assertIn("test-user/vid-forge-sample-video-worker/7", output)
        self.assertTrue(any(call[:2] == ["datasets", "create"] for call in self.calls))
        self.assertTrue(any(call[:2] == ["kernels", "push"] and "NvidiaTeslaA100" in call for call in self.calls))

    def test_job_bundle_matches_diffusers_worker_inputs(self) -> None:
        self._run_batch()
        jobs_file = self.root / "sample" / "cache" / "video-jobs" / "dataset" / "jobs" / "jobs.json"
        bundle = json.loads(jobs_file.read_text(encoding="utf-8"))
        self.assertEqual(bundle["model"], Wan22Provider.model_id)
        self.assertEqual(bundle["model_revision"], Wan22Provider.model_revision)
        self.assertEqual(bundle["jobs"][0]["width"], 832)
        self.assertEqual(bundle["jobs"][0]["height"], 480)
        self.assertEqual(bundle["jobs"][0]["fps"], self.request.fps)
        self.assertEqual(bundle["jobs"][0]["frame_count"], self.request.frame_count)

    def test_existing_dataset_is_versioned(self) -> None:
        self._run_batch(existing=True)
        self.assertTrue(any(call[:2] == ["datasets", "version"] for call in self.calls))

    def test_kernel_failure_and_output_download_failure_are_reported(self) -> None:
        self._run_batch(kernel_status=["KernelWorkerStatus.FAILED"])
        self.assertFalse(any(call[:2] == ["kernels", "output"] for call in self.calls))
        self.calls.clear()
        self._run_batch(download_failure=True)

    def test_running_status_is_polled_until_completion(self) -> None:
        result = self._run_batch(kernel_status=["KernelWorkerStatus.RUNNING", "KernelWorkerStatus.COMPLETE"])
        self.assertTrue(result["completed_at"])
        self.assertEqual(sum(call[:2] == ["kernels", "status"] for call in self.calls), 2)

    def test_kernel_timeout_is_reported(self) -> None:
        with patch.object(video_assemble.time, "monotonic", side_effect=[0.0, 1.0, 1001.0]):
            with self.assertRaisesRegex(TimeoutError, "timed out"):
                self._run_batch(kernel_status=["KernelWorkerStatus.RUNNING"], timeout=0)

    def test_interrupted_local_run_can_retrieve_a_completed_kernel_version(self) -> None:
        calls: list[list[str]] = []

        def run(args: list[str], env: dict[str, str], timeout: int = 1200) -> str:
            calls.append(args)
            if args[:2] == ["kernels", "status"]:
                return "KernelWorkerStatus.COMPLETE"
            if args[:2] == ["kernels", "output"]:
                output = Path(args[args.index("-p") + 1]) / "results"
                output.mkdir(parents=True)
                (output / "results.json").write_text('{"jobs": []}', encoding="utf-8")
                return "downloaded"
            raise AssertionError(args)

        previous = {"kernel_id": "test-user/vid-forge-sample-video", "kernel_version": 4,
                    "dataset_id": "test-user/jobs", "accelerator": "NvidiaTeslaA100"}
        with patch.object(video_assemble, "_kaggle_environment", return_value={"KAGGLE_USERNAME": "test-user"}), \
             patch.object(video_assemble, "_run_kaggle", side_effect=run):
            result = video_assemble._resume_kaggle_batch(self.root, previous, 30)
        self.assertEqual(result["kernel_version"], 4)
        self.assertIn("test-user/vid-forge-sample-video/4", calls[-1])


class KenBurnsTests(unittest.TestCase):
    def test_filter_uses_subpixel_perspective_not_integer_zoompan(self) -> None:
        for camera in ("zoom_in", "zoom_out", "pan_left", "pan_right"):
            graph = video_assemble._kenburns_filter(camera, 120, 24)
            self.assertIn("perspective=", graph)
            self.assertIn("eval=frame", graph)
            self.assertIn("interpolation=cubic", graph)
            self.assertNotIn("zoompan", graph)

    def test_unknown_camera_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            video_assemble._kenburns_filter("spin", 120, 24)

    def test_filter_version_was_bumped_so_old_shaky_clips_are_not_reused(self) -> None:
        self.assertNotEqual(video_assemble.KEN_BURNS_FILTER_VERSION, "ffmpeg-filter-v2-subtle-eased")

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg is not installed")
    def test_render_produces_expected_frame_count_and_size(self) -> None:
        import subprocess
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            image = root / "still.png"
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=size=1408x768:rate=1",
                            "-frames:v", "1", str(image)], check=True)
            for camera in ("zoom_in", "pan_right"):
                out = root / f"{camera}.mp4"
                video_assemble._render_kenburns(image, out, {"id": "shot_t", "duration_s": 1.0,
                                                            "motion": {"camera": camera}}, 24)
                probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_frames",
                                        "-show_entries", "stream=width,height,nb_read_frames", "-of", "json", str(out)],
                                       capture_output=True, text=True, check=True)
                stream = json.loads(probe.stdout)["streams"][0]
                self.assertEqual((stream["width"], stream["height"]), (1280, 720))
                self.assertEqual(int(stream["nb_read_frames"]), 24)


if __name__ == "__main__":
    unittest.main()
