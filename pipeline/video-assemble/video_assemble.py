"""Generate one independent video clip per storyboard shot."""
from __future__ import annotations

import hashlib
import json
import importlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from typing import Any
import uuid

try:
    from dotenv import load_dotenv
except ImportError:  # Keep cache tests and stage inspection usable before optional project deps are installed.
    def load_dotenv(path: Path) -> None:
        if not path.is_file():
            return
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, value = line.split("=", 1)
            os.environ.setdefault(name.strip(), value.strip().strip("\"'"))
from providers import HunyuanVideo15Provider, VideoRequest, Wan22Provider
from video_cache import FileLock, VideoCache, fingerprint, probe_video, sha256_file, write_json_atomic

PIPELINE_VERSION = "1.0"
MANIFEST_SCHEMA = 1
DEFAULT_STEPS = 12
DEFAULT_GUIDANCE = 5.0
OUTPUT_WIDTH, OUTPUT_HEIGHT = 1280, 720
KEN_BURNS_FILTER_VERSION = "ffmpeg-filter-v2-subtle-eased"
STORYBOARD_PATH = Path("production") / "storyboard.json"
SELECTION_MANIFEST = Path("production") / "keyframes.json"


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _append_run(path: Path, item: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(item, separators=(",", ":"), ensure_ascii=False) + "\n")


def _load_json(path: Path, fallback: Any) -> Any:
    if not path.is_file():
        return fallback
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in {path}: {exc}") from exc


def _selected_keyframe(project_dir: Path, shot: dict[str, Any], index: dict[str, Any], override: Path | None) -> Path:
    shot_id = shot["id"]
    selected = shot.get("keyframe", {}).get("selected")
    if not isinstance(selected, int) or selected < 1:
        raise ValueError(f"{shot_id}: choose a candidate by setting keyframe.selected in storyboard.json.")
    entry = index.get("shots", {}).get(shot_id, {}).get("candidates", {}).get(str(selected))
    if not isinstance(entry, dict) or entry.get("success") is not True or not entry.get("path"):
        raise ValueError(f"{shot_id}: selected candidate {selected} is not marked successful in production/keyframes.json; rerun keyframes or repair its manifest.")
    manifest_path = Path(entry["path"])
    if override:
        image = override / shot_id / manifest_path.name
    elif manifest_path.is_absolute():
        image = manifest_path
    else:
        # The existing keyframes index stores paths relative to repo root (projects/<name>/...).
        parts = manifest_path.parts
        image = project_dir.joinpath(*parts[2:]) if len(parts) > 1 and parts[0] == "projects" else project_dir / manifest_path
    if not image.is_file():
        raise FileNotFoundError(f"{shot_id}: selected keyframe file is missing: {image}. Generate keyframes first.")
    return image.resolve()


def _prompt(shot: dict[str, Any], storyboard: dict[str, Any]) -> str:
    motion = shot.get("motion", {})
    instruction = str(motion.get("prompt", "")).strip()
    if motion.get("type") == "kenburns":
        return ""
    if not instruction:
        raise ValueError(f"{shot['id']}: i2v motion requires a non-empty motion.prompt.")
    parts = [storyboard.get("style", {}).get("prefix", "").strip()]
    # Only add character style when the prompt does not already include the character's name.
    prompt_lower = instruction.casefold()
    for character in shot.get("characters", []):
        description = storyboard.get("style", {}).get("characters", {}).get(character, "").strip()
        if description and character.casefold() not in prompt_lower:
            parts.append(f"{character}: {description}")
    parts.append(instruction)
    return ". ".join(part for part in parts if part)


def _frame_count(seconds: float, fps: int) -> int:
    # Wan TI2V takes 4n+1 frames; choose the nearest supported count to duration*fps+1.
    requested = max(5, round(seconds * fps) + 1)
    return max(5, 4 * round((requested - 1) / 4) + 1)


def _request(project_dir: Path, storyboard: dict[str, Any], shot: dict[str, Any], image: Path, provider: Any,
             steps: int = DEFAULT_STEPS, guidance: float = DEFAULT_GUIDANCE) -> VideoRequest:
    fps = int(storyboard.get("meta", {}).get("fps", 24))
    motion = shot["motion"]
    frame_count = _frame_count(float(shot["duration_s"]), fps)
    request = VideoRequest(
        shot_id=shot["id"], image=str(image), prompt=_prompt(shot, storyboard),
        negative_prompt=str(storyboard.get("style", {}).get("negative", "")).strip(),
        seed=int(motion.get("seed", shot.get("keyframe", {}).get("seed", 0))),
        duration_s=float(shot["duration_s"]), fps=fps,
        width=OUTPUT_WIDTH, height=OUTPUT_HEIGHT, frame_count=frame_count,
        steps=int(motion.get("steps", steps)), guidance=float(motion.get("guidance", guidance)),
        sampling_solver="unipc", sampling_shift=5.0,
        provider=provider.provider_id, model=provider.model_id, model_revision=provider.model_revision,
        implementation_revision=provider.implementation_revision,
        image_sha256=sha256_file(image), pipeline_version=PIPELINE_VERSION,
        preprocessing_version="wan-ti2v-rgb-best-output-size-v1",
        motion_parameters={k: v for k, v in motion.items() if k not in {"prompt", "seed", "type"}},
    )
    provider.validate(request)
    return request


def _manifest_path(project_dir: Path) -> Path:
    return project_dir / "production" / "video_manifest.json"


def _manifest(project_dir: Path) -> dict[str, Any]:
    data = _load_json(_manifest_path(project_dir), {"schema_version": MANIFEST_SCHEMA, "shots": {}})
    data.setdefault("schema_version", MANIFEST_SCHEMA)
    data.setdefault("shots", {})
    return data


def _validate_existing_storyboard(storyboard_file: Path, project_dir: Path) -> None:
    script_dir = Path(__file__).resolve().parents[1] / "script"
    if str(script_dir) not in sys.path:
        sys.path.insert(0, str(script_dir))
    try:
        validator = importlib.import_module("validate_storyboard")
    except ModuleNotFoundError as exc:
        if exc.name != "jsonschema":
            raise
        # Keep isolated cache tools usable in a minimal Python install; the project CLI
        # dependency installs jsonschema and uses the full schema validator.
        data = _load_json(storyboard_file, {})
        if not isinstance(data.get("shots"), list) or not isinstance(data.get("meta", {}).get("fps"), int):
            raise ValueError("storyboard requires a shots list and meta.fps (install project dependencies for schema validation)") from exc
        return
    report = validator.validate_file(storyboard_file, project_dir=project_dir)
    if not report.ok:
        raise ValueError("storyboard validation failed:\n" + report.format())


def _record_attempt(manifest: dict[str, Any], shot_id: str, attempt: dict[str, Any], path: Path) -> None:
    entry = manifest["shots"].setdefault(shot_id, {"attempts": []})
    entry.setdefault("attempts", []).append(attempt)
    entry.update({k: v for k, v in attempt.items() if k != "error"})
    write_json_atomic(path, manifest)


def _update_attempt(manifest: dict[str, Any], _shot_id: str, _fingerprint_key: str,
                    _status: str, path: Path, **details: Any) -> None:
    attempts = manifest["shots"].setdefault(_shot_id, {"attempts": []}).setdefault("attempts", [])
    for attempt in reversed(attempts):
        if attempt.get("fingerprint") == _fingerprint_key:
            attempt.update({"status": _status, **details})
            manifest["shots"][_shot_id].update({"status": _status, **details})
            write_json_atomic(path, manifest)
            return
    raise ValueError(f"No video manifest attempt for {_shot_id} / {_fingerprint_key}")


def _ffmpeg_path() -> str:
    path = shutil.which("ffmpeg")
    if not path:
        raise RuntimeError("FFmpeg was not found. Install FFmpeg and ffprobe and add both to PATH.")
    return path


def _ffprobe_path() -> str:
    path = shutil.which("ffprobe")
    if not path:
        raise RuntimeError("ffprobe was not found. Install FFmpeg (including ffprobe) and add it to PATH.")
    return path


def _render_kenburns(image: Path, output: Path, shot: dict[str, Any], fps: int) -> dict[str, Any]:
    ffmpeg = _ffmpeg_path()
    _ffprobe_path()
    motion = shot["motion"]
    camera = motion.get("camera")
    if camera not in {"zoom_in", "zoom_out", "pan_left", "pan_right"}:
        raise ValueError(f"{shot['id']}: unsupported Ken Burns camera {camera!r}; use zoom_in, zoom_out, pan_left, or pan_right.")
    duration = float(shot["duration_s"])
    frames = max(1, round(duration * fps))
    # Keep the total camera travel restrained and ease it in/out so movement
    # does not feel like a sudden digital camera adjustment.
    zoom = 1.04
    progress = f"(0.5-0.5*cos(PI*on/{max(frames-1,1)}))"
    if camera in {"zoom_in", "zoom_out"}:
        initial, final = (1.0, zoom) if camera == "zoom_in" else (zoom, 1.0)
        z = f"{initial}+({final}-{initial})*{progress}"
        x, y = "(iw-iw/zoom)/2", "(ih-ih/zoom)/2"
    else:
        z = str(zoom)
        x = f"(iw-iw/zoom)*{progress}" if camera == "pan_right" else f"(iw-iw/zoom)*(1-{progress})"
        y = "(ih-ih/zoom)/2"
    output.parent.mkdir(parents=True, exist_ok=True)
    filter_graph = (f"scale={OUTPUT_WIDTH}:{OUTPUT_HEIGHT}:force_original_aspect_ratio=increase,"
                    f"crop={OUTPUT_WIDTH}:{OUTPUT_HEIGHT},zoompan=z='{z}':x='{x}':y='{y}':d=1:s={OUTPUT_WIDTH}x{OUTPUT_HEIGHT}:fps={fps},"
                    "setsar=1,format=yuv420p")
    result = subprocess.run([ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-loop", "1", "-i", str(image),
                             "-vf", filter_graph, "-frames:v", str(frames), "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                             "-r", str(fps), "-movflags", "+faststart", str(output)], capture_output=True, text=True, check=False)
    if result.returncode:
        raise RuntimeError(f"FFmpeg Ken Burns render failed: {result.stderr[-700:]}")
    return probe_video(output)


def _normalize(source: Path, output: Path, fps: int) -> dict[str, Any]:
    ffmpeg = _ffmpeg_path()
    _ffprobe_path()
    output.parent.mkdir(parents=True, exist_ok=True)
    vf = f"scale={OUTPUT_WIDTH}:{OUTPUT_HEIGHT}:force_original_aspect_ratio=increase,crop={OUTPUT_WIDTH}:{OUTPUT_HEIGHT},fps={fps},setsar=1,format=yuv420p"
    result = subprocess.run([ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(source), "-map", "0:v:0", "-an",
                             "-vf", vf, "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-r", str(fps),
                             "-movflags", "+faststart", str(output)], capture_output=True, text=True, check=False)
    if result.returncode:
        raise RuntimeError(f"FFmpeg normalization failed: {result.stderr[-700:]}")
    return probe_video(output)


def _kaggle_environment() -> dict[str, str]:
    token = os.getenv("KAGGLE_API_TOKEN", "").strip()
    username = os.getenv("KAGGLE_USERNAME", "").strip()
    if not token or not username:
        missing = ", ".join(name for name, value in (("KAGGLE_API_TOKEN", token), ("KAGGLE_USERNAME", username)) if not value)
        raise RuntimeError(f"Missing {missing}; configure these in the repository .env before Kaggle upload/submission.")
    env = os.environ.copy()
    env.update({"KAGGLE_API_TOKEN": token, "KAGGLE_USERNAME": username})
    return env


def _run_kaggle(args: list[str], env: dict[str, str], timeout: int = 1200) -> str:
    retry_safe = args[:2] in (["kernels", "status"], ["kernels", "output"], ["datasets", "list"], ["datasets", "status"])
    for attempt in range(3 if retry_safe else 1):
        try:
            result = subprocess.run(["kaggle", *args], capture_output=True, text=True, env=env, timeout=timeout, check=False)
        except subprocess.TimeoutExpired as exc:
            if retry_safe and attempt < 2:
                time.sleep(2**attempt)
                continue
            raise RuntimeError(f"Kaggle CLI command timed out: {' '.join(args[:2])}") from exc
        if result.returncode == 0:
            return result.stdout + result.stderr
        details = (result.stderr or result.stdout)[-1200:]
        token = env.get("KAGGLE_API_TOKEN", "")
        if token:
            details = details.replace(token, "[REDACTED]")
        transient = any(code in details for code in ("429", "500", "502", "503", "504")) or any(
            phrase in details.casefold() for phrase in ("connection reset", "temporarily unavailable", "timed out", "temporary failure")
        )
        if retry_safe and transient and attempt < 2:
            time.sleep(2**attempt)
            continue
        if args[:2] == ["kernels", "status"] and (
            "kernels.get" in details.casefold() or "permission" in details.casefold() and "denied" in details.casefold()
        ):
            kernel_ref = args[2] if len(args) > 2 else "the submitted kernel"
            raise RuntimeError(
                f"Kaggle denied access to kernel '{kernel_ref}'. Check that KAGGLE_USERNAME matches the "
                "Kaggle account that owns the kernel and API credentials, and that the kernel slug is the "
                "notebook URL slug. Confirm the kernel is visible with `kaggle kernels list --mine`. "
                f"Kaggle CLI details: {details}"
            )
        raise RuntimeError(f"Kaggle CLI failed (exit {result.returncode}): {details}")
    raise RuntimeError("Kaggle CLI retry limit reached")


def _resume_kaggle_batch(project_dir: Path, previous: dict[str, Any], timeout: int) -> dict[str, Any] | None:
    """Fetch outputs from a previously tracked kernel version after a local interruption."""
    kernel_id, version = previous.get("kernel_id"), previous.get("kernel_version")
    if not kernel_id or not version:
        return None
    env = _kaggle_environment()
    status_ref = f"{kernel_id}/{version}"
    print(f"Kaggle: checking previous run {status_ref} before resubmitting.", flush=True)
    deadline = time.monotonic() + timeout
    status_text = ""
    while time.monotonic() < deadline:
        status_text = _run_kaggle(["kernels", "status", status_ref], env, timeout=120)
        lowered = status_text.casefold()
        summary = " ".join(status_text.strip().split())[-240:] or "Kaggle returned no status text"
        print(f"Kaggle: previous run status: {summary}", flush=True)
        if re.search(r"\b(complete|error|failed|cancelled|canceled|cancel_acknowledged)\b", lowered):
            break
        time.sleep(20)
    else:
        raise TimeoutError(f"Previously submitted Kaggle kernel is still running: {status_ref}")
    lowered = status_text.casefold()
    if not re.search(r"\bcomplete\b", lowered) or re.search(
        r"\b(error|failed|cancelled|canceled|cancel_acknowledged)\b", lowered
    ):
        return None
    output_dir = project_dir / "cache" / "video-jobs" / "resumed-download"
    _run_kaggle(["kernels", "output", status_ref, "-p", str(output_dir), "-o"], env, timeout=1800)
    results_file = output_dir / "results" / "results.json"
    if not results_file.is_file():
        raise RuntimeError(f"Completed Kaggle run {status_ref} has no results/results.json")
    result_data = _load_json(results_file, {})
    return {**previous, "kernel_version": int(version), "completed_at": utc_now(),
            "accelerator": previous.get("accelerator", os.getenv("KAGGLE_ACCELERATOR", "NvidiaTeslaA100")),
            "status_text": status_text[-1000:], "output_dir": str(output_dir),
            "jobs": result_data.get("jobs", [])}


def _kaggle_batch(project_dir: Path, requests: list[tuple[VideoRequest, dict[str, Any]]], accelerator: str,
                  worker_revision: str, timeout: int, manifest: dict[str, Any], on_status: Any = None) -> dict[str, Any]:
    """Upload one private dataset and submit one reusable private GPU kernel batch."""
    env = _kaggle_environment()
    print("Kaggle: checking API access.", flush=True)
    try:
        _run_kaggle(["datasets", "list", "--page-size", "1"], env, timeout=60)
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("Kaggle CLI authentication check could not run; install/update the official Kaggle CLI and check .env.") from exc
    username = env["KAGGLE_USERNAME"]
    project_slug = re.sub(r"[^a-z0-9-]+", "-", project_dir.name.lower()).strip("-")[:30]
    dataset_slug = f"vid-forge-{project_slug}-jobs"
    kernel_slug = f"vid-forge-{project_slug}-video-worker"
    root = project_dir / "cache" / "video-jobs"
    bundle = root / "dataset"
    print(f"Kaggle: preparing job bundle for {len(requests)} shot(s).", flush=True)
    if root.exists():
        shutil.rmtree(root)
    jobs_dir, images_dir = bundle / "jobs", bundle / "jobs" / "images"
    images_dir.mkdir(parents=True)
    worker_source = Path(__file__).with_name("video_worker.py")
    shutil.copyfile(worker_source, root / "video_worker.py")
    jobs = []
    for request, payload in requests:
        image_name = f"{request.shot_id}{Path(request.image).suffix.lower() or '.png'}"
        shutil.copyfile(request.image, images_dir / image_name)
        jobs.append({"shot_id": request.shot_id, "image": f"images/{image_name}", "prompt": request.prompt,
                     "seed": request.seed, "negative_prompt": request.negative_prompt,
                     "frame_count": request.frame_count, "steps": request.steps, "fps": request.fps,
                     "guidance": request.guidance, "sampling_solver": request.sampling_solver,
                     "sampling_shift": request.sampling_shift, "width": 832, "height": 480})
    write_json_atomic(jobs_dir / "jobs.json", {"model": requests[0][0].model, "model_revision": requests[0][0].model_revision,
                                                "worker_revision": worker_revision, "jobs": jobs})
    metadata = {"title": f"Vid Forge {project_slug} generation inputs", "id": f"{username}/{dataset_slug}",
                "licenses": [{"name": "unknown"}]}
    write_json_atomic(bundle / "dataset-metadata.json", metadata)
    print(f"Kaggle: checking dataset {username}/{dataset_slug}.", flush=True)
    exists = subprocess.run(["kaggle", "datasets", "status", f"{username}/{dataset_slug}"], capture_output=True,
                            text=True, env=env, timeout=90, check=False).returncode == 0
    if exists:
        print(f"Kaggle: uploading a new version of {username}/{dataset_slug}.", flush=True)
        _run_kaggle(["datasets", "version", "-p", str(bundle), "-m", f"Vid-Forge jobs {utc_now()}", "-t", "-r", "zip"], env)
    else:
        print(f"Kaggle: creating dataset {username}/{dataset_slug}.", flush=True)
        _run_kaggle(["datasets", "create", "-p", str(bundle), "-t", "-r", "zip"], env)
    print(f"Kaggle: waiting for dataset {username}/{dataset_slug} to be ready.", flush=True)
    _run_kaggle(["datasets", "status", f"{username}/{dataset_slug}"], env, timeout=300)
    kernel_dir = root / "kernel"
    kernel_dir.mkdir()
    shutil.copyfile(worker_source, kernel_dir / "video_worker.py")
    kernel_meta = {"id": f"{username}/{kernel_slug}", "title": f"Vid Forge {project_slug} video worker",
                   "code_file": "video_worker.py", "language": "python", "kernel_type": "script",
                   "is_private": True, "enable_gpu": True, "enable_internet": True,
                   "dataset_sources": [f"{username}/{dataset_slug}"], "competition_sources": []}
    write_json_atomic(kernel_dir / "kernel-metadata.json", kernel_meta)
    print(f"Kaggle: submitting {username}/{kernel_slug} on {accelerator}.", flush=True)
    submitted = _run_kaggle(["kernels", "push", "-p", str(kernel_dir), "--accelerator", accelerator, "-t", str(timeout)], env, timeout=300)
    version_match = re.search(r"(?:version\s+|/)(\d+)\b", submitted, re.IGNORECASE)
    version = int(version_match.group(1)) if version_match else None
    kernel_id = f"{username}/{kernel_slug}"
    submission_at = utc_now()
    started_at = utc_now()
    if on_status:
        on_status("submitted", {"submitted_at": submission_at, "dataset_id": f"{username}/{dataset_slug}", "kernel_id": kernel_id, "kernel_version": version})
        on_status("running", {"started_at": started_at})
    print(f"Kaggle: submitted {kernel_id}/{version or 'latest'}; waiting for kernel completion.", flush=True)
    deadline = time.monotonic() + timeout + 900
    status_text = ""
    while time.monotonic() < deadline:
        status_ref = f"{kernel_id}/{version}" if version else kernel_id
        status_text = _run_kaggle(["kernels", "status", status_ref], env, timeout=120)
        lowered = status_text.casefold()
        terminal = re.search(r"\b(complete|error|failed|cancelled|canceled|cancel_acknowledged)\b", lowered)
        if terminal:
            break
        time.sleep(20)
    else:
        raise TimeoutError(f"Kaggle kernel timed out after {timeout}s: {kernel_id}")
    lowered = status_text.casefold()
    if not re.search(r"\bcomplete\b", lowered) or re.search(
        r"\b(error|failed|cancelled|canceled|cancel_acknowledged)\b", lowered
    ):
        raise RuntimeError(f"Kaggle kernel failed: {status_text[-1000:]}")
    if version is None:
        raise RuntimeError("Kaggle CLI did not report the submitted kernel version; refusing to download ambiguous latest output.")
    out_dir = root / "download"
    if on_status:
        on_status("downloading", {"completed_at": utc_now()})
    print(f"Kaggle: downloading results for {kernel_id}/{version}.", flush=True)
    _run_kaggle(["kernels", "output", f"{kernel_id}/{version}", "-p", str(out_dir), "-o"], env, timeout=1800)
    result_file = out_dir / "results" / "results.json"
    if not result_file.is_file():
        raise RuntimeError("Kaggle completed but did not provide results/results.json.")
    batch_results = _load_json(result_file, {})
    return {"dataset_id": f"{username}/{dataset_slug}", "kernel_id": kernel_id,
            "kernel_version": version, "submitted_at": submission_at, "completed_at": utc_now(),
            "started_at": started_at, "accelerator": accelerator, "status_text": status_text[-1000:],
            "output_dir": str(out_dir), "jobs": batch_results.get("jobs", []), "gpu": batch_results.get("gpu")}


def _copy_selected(source: Path, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    temp = output.with_name(output.name + f".{uuid.uuid4().hex}.tmp")
    shutil.copyfile(source, temp)
    os.replace(temp, output)


def run_video(project_dir: Path, *, provider_name: str = "wan22", shot_id: str | None = None,
              force: bool = False, dry_run: bool = False, validate_only: bool = False,
              accelerator: str | None = None, keyframes_dir: Path | None = None,
              generation_steps: int = DEFAULT_STEPS, guidance: float = DEFAULT_GUIDANCE,
              env_file: Path | None = None, timeout: int = 6 * 60 * 60,
              ffprobe: str = "ffprobe", ffmpeg: str = "ffmpeg") -> int:
    root = Path(__file__).resolve().parents[2]
    load_dotenv(env_file or root / ".env")
    storyboard_file = project_dir / STORYBOARD_PATH
    if not storyboard_file.is_file():
        print(f"Error: storyboard not found: {storyboard_file}", file=sys.stderr)
        return 1
    try:
        storyboard = _load_json(storyboard_file, {})
        _validate_existing_storyboard(storyboard_file, project_dir)
        if not isinstance(storyboard.get("shots"), list):
            raise ValueError("storyboard.shots must be a list")
        manifest_kf = _load_json(project_dir / SELECTION_MANIFEST, {})
        shots = storyboard["shots"]
        if shot_id:
            shots = [shot for shot in shots if shot.get("id") == shot_id]
            if not shots:
                raise ValueError(f"shot {shot_id!r} is not in storyboard")
        provider = Wan22Provider() if provider_name == "wan22" else HunyuanVideo15Provider() if provider_name == "hunyuan15" else None
        if provider is None:
            raise ValueError("provider must be wan22 or hunyuan15")
        manifest = _manifest(project_dir)
        cache = VideoCache(project_dir)
        plan: list[dict[str, Any]] = []
        # Resolve/validate shot motion before any remote operation.
        for shot in shots:
            motion = shot.get("motion")
            if not isinstance(motion, dict) or motion.get("type") not in {"i2v", "kenburns"}:
                raise ValueError(f"{shot.get('id', '(unknown shot)')}: motion.type must be i2v or kenburns")
            if motion["type"] == "kenburns":
                if motion.get("camera") not in {"zoom_in", "zoom_out", "pan_left", "pan_right"}:
                    raise ValueError(f"{shot['id']}: unsupported Ken Burns camera {motion.get('camera')!r}")
                selected_image = _selected_keyframe(project_dir, shot, manifest_kf, keyframes_dir)
                request = _request(project_dir, storyboard, shot, selected_image, Wan22Provider(), generation_steps, guidance)
                # Include local motion and the actual requested frames in its content identity.
                request = VideoRequest(**{**request.__dict__, "provider": "ffmpeg-kenburns", "model": "ffmpeg",
                                          "model_revision": KEN_BURNS_FILTER_VERSION,
                                          "implementation_revision": KEN_BURNS_FILTER_VERSION,
                                          "preprocessing_version": "ffmpeg-scale-crop-v1",
                                          "motion_parameters": {"camera": motion["camera"]}})
            else:
                selected_image = _selected_keyframe(project_dir, shot, manifest_kf, keyframes_dir)
                request = _request(project_dir, storyboard, shot, selected_image, provider, generation_steps, guidance)
            key = fingerprint(request.fingerprint_payload())
            fresh, cached_meta, invalid_reason = cache.validate(key, ffprobe=ffprobe, ffmpeg=ffmpeg)
            cache_status = "hit" if fresh and not force else "miss"
            plan.append({"shot": shot, "image": selected_image, "request": request, "fingerprint": key,
                         "cached": cached_meta, "invalid_reason": invalid_reason, "cache_status": cache_status,
                         "motion_type": motion["type"]})
        print(f"Video stage for {project_dir.name}: provider={provider_name}, model={provider.model_id}, accelerator={accelerator or os.getenv('KAGGLE_ACCELERATOR', 'NvidiaTeslaA100')}")
        for item in plan:
            print(f"{item['shot']['id']}: {item['cache_status']} | {item['motion_type']} | {item['image']} | {item['fingerprint']}")
            if item["invalid_reason"]:
                print(f"  invalid cache reason: {item['invalid_reason']}")
        pending = [item for item in plan if item["cache_status"] == "miss"]
        i2v_pending = [item for item in pending if item["motion_type"] == "i2v"]
        local_pending = [item for item in pending if item["motion_type"] == "kenburns"]
        print(f"Jobs: {len(pending)} total, {len(local_pending)} local Ken Burns, {len(i2v_pending)} Kaggle inference")
        if validate_only:
            bad = []
            for item in plan:
                if item["cache_status"] != "hit":
                    bad.append(item)
                    continue
                current_output = project_dir / "outputs" / "shots" / f"{item['shot']['id']}.mp4"
                try:
                    if not current_output.is_file() or sha256_file(current_output) != item["cached"].get("output_sha256"):
                        raise ValueError("selected output is missing or its checksum differs from the cache")
                    probe_video(current_output, ffprobe, ffmpeg)
                except (OSError, ValueError) as exc:
                    item["invalid_reason"] = str(exc)
                    bad.append(item)
            if bad:
                print("Validation failed for: " + ", ".join(item["shot"]["id"] for item in bad), file=sys.stderr)
                return 1
            print("All requested cached clips passed checksum and ffprobe/decode validation.")
            return 0
        if dry_run:
            return 0
        for item in plan:
            if item["cache_status"] == "miss" and item["invalid_reason"] and cache.entry_dir(item["fingerprint"]).parent.exists():
                manifest["shots"].setdefault(item["shot"]["id"], {"attempts": []}).update(
                    {"status": "invalid", "fingerprint": item["fingerprint"], "invalid_reason": item["invalid_reason"],
                     "invalid_at": utc_now()}
                )
        write_json_atomic(_manifest_path(project_dir), manifest)
        # A reusable cache artifact may outlive its convenience copy in outputs/shots/.
        for item in plan:
            if item["cache_status"] == "hit" and not force:
                output = project_dir / "outputs" / "shots" / f"{item['shot']['id']}.mp4"
                _copy_selected(Path(item["cached"]["cache_path"]), output)
                attempts = manifest["shots"].get(item["shot"]["id"], {}).get("attempts", [])
                unfinished = next((attempt for attempt in reversed(attempts)
                                   if attempt.get("fingerprint") == item["fingerprint"]
                                   and attempt.get("status") != "completed"), None)
                if unfinished:
                    _update_attempt(
                        manifest, item["shot"]["id"], item["fingerprint"], "completed", _manifest_path(project_dir),
                        completed_at=utc_now(), cache_path=item["cached"]["cache_path"], output_path=str(output),
                        output_sha256=item["cached"].get("output_sha256"),
                        actual_duration_s=item["cached"].get("actual_duration_s"),
                        validation=item["cached"].get("validation"), recovered_from_cache=True,
                    )
        if not provider.implemented and i2v_pending:
            raise NotImplementedError("HunyuanVideo 1.5 provider boundary is present but its Kaggle inference worker is not implemented; choose --provider wan22.")

        runs_file = project_dir / "runs.jsonl"
        manifest_file = _manifest_path(project_dir)
        if local_pending:
            for item in local_pending:
                request: VideoRequest = item["request"]
                key = item["fingerprint"]
                with cache.lock(key):
                    fresh, cached_meta, why = cache.validate(key, ffprobe=ffprobe, ffmpeg=ffmpeg)
                    if fresh and not force:
                        source_video = Path(cached_meta["cache_path"])
                        _copy_selected(source_video, project_dir / "outputs" / "shots" / f"{item['shot']['id']}.mp4")
                        continue
                    attempt = {"status": "running", "fingerprint": key, "shot_id": request.shot_id,
                               "provider": request.provider, "model": request.model, "created_at": utc_now(),
                               "parameters": request.fingerprint_payload()}
                    _record_attempt(manifest, request.shot_id, attempt, manifest_file)
                    with tempfile.TemporaryDirectory(prefix="vidforge-kb-") as tmp:
                        raw = Path(tmp) / "raw.mp4"
                        try:
                            validation = _render_kenburns(item["image"], raw, item["shot"], request.fps)
                            video, meta = cache.commit(key, request.fingerprint_payload(), raw,
                                {**attempt, "actual_duration_s": validation["duration_s"], "validation": validation}, force=force,
                                ffprobe=ffprobe, ffmpeg=ffmpeg)
                            output = project_dir / "outputs" / "shots" / f"{request.shot_id}.mp4"
                            _copy_selected(video, output)
                            done = {**attempt, "status": "completed", "completed_at": utc_now(), "cache_path": str(video),
                                    "output_path": str(output), "output_sha256": meta["output_sha256"], "actual_duration_s": validation["duration_s"],
                                    "validation": validation}
                            _update_attempt(manifest, request.shot_id, key, "completed", manifest_file, **{k: v for k, v in done.items() if k != "status"})
                            _append_run(runs_file, {"ts": utc_now(), "stage": "video", "shot_id": request.shot_id,
                                "fingerprint": key, "provider": request.provider, "model": request.model, "seed": request.seed,
                                "parameters": request.fingerprint_payload(), "duration_s": validation["duration_s"], "success": True})
                        except Exception as exc:
                            failed = {**attempt, "status": "failed", "completed_at": utc_now(), "error": str(exc)[:1200]}
                            _update_attempt(manifest, request.shot_id, key, "failed", manifest_file, **{k: v for k, v in failed.items() if k != "status"})
                            _append_run(runs_file, {"ts": utc_now(), "stage": "video", "shot_id": request.shot_id,
                                "fingerprint": key, "provider": request.provider, "model": request.model,
                                "seed": request.seed, "success": False, "error": str(exc)[:500]})
                            raise

        failed_shots: list[str] = []
        if i2v_pending:
            # Serialize Kaggle's mutable kernel latest-run/output operations across local processes.
            project_lock = FileLock(str(project_dir / "cache" / "video-kaggle.lock"), timeout=24 * 60 * 60)
            with project_lock:
                jobs: list[tuple[VideoRequest, dict[str, Any]]] = []
                for item in i2v_pending:
                    request = item["request"]
                    with cache.lock(item["fingerprint"]):
                        fresh, cached_meta, why = cache.validate(item["fingerprint"], ffprobe=ffprobe, ffmpeg=ffmpeg)
                        if fresh and not force:
                            _copy_selected(Path(cached_meta["cache_path"]), project_dir / "outputs" / "shots" / f"{request.shot_id}.mp4")
                            continue
                        history = manifest["shots"].get(request.shot_id, {}).get("attempts", [])
                        prior = next((old.get("kaggle") for old in reversed(history)
                                      if old.get("fingerprint") == item["fingerprint"]
                                      and old.get("kaggle", {}).get("kernel_id")
                                      and old.get("kaggle", {}).get("kernel_version")), None)
                        attempt = {"status": "pending", "fingerprint": item["fingerprint"], "shot_id": request.shot_id,
                                   "provider": provider.provider_id, "model": provider.model_id,
                                   "model_revision": provider.model_revision, "seed": request.seed, "created_at": utc_now(),
                                   "parameters": request.fingerprint_payload(), "input_image_sha256": request.image_sha256,
                                   "error": why, "previous_kaggle": prior}
                        _record_attempt(manifest, request.shot_id, attempt, manifest_file)
                        jobs.append((request, attempt))
                if jobs:
                    try:
                        def mark_batch(status: str, details: dict[str, Any]) -> None:
                            for request, attempt in jobs:
                                _update_attempt(manifest, request.shot_id, attempt["fingerprint"], status, manifest_file,
                                                kaggle={**attempt.get("kaggle", {}), **details})

                        previous_runs = [job[1].get("previous_kaggle") for job in jobs]
                        can_resume = bool(previous_runs and previous_runs[0] and all(
                            entry and (entry.get("kernel_id"), entry.get("kernel_version")) ==
                            (previous_runs[0].get("kernel_id"), previous_runs[0].get("kernel_version"))
                            for entry in previous_runs
                        ))
                        batch = _resume_kaggle_batch(project_dir, previous_runs[0], timeout) if can_resume else None
                        if batch is None:
                            batch = _kaggle_batch(project_dir, jobs, accelerator or os.getenv("KAGGLE_ACCELERATOR", "NvidiaTeslaA100"),
                                              jobs[0][0].implementation_revision, timeout, manifest, on_status=mark_batch)
                        for request, attempt in jobs:
                            attempt["kaggle"] = {"dataset_id": batch.get("dataset_id"), "kernel_id": batch.get("kernel_id"),
                                                 "kernel_version": batch.get("kernel_version"), "accelerator": batch.get("accelerator"),
                                                 "submitted_at": batch.get("submitted_at"), "started_at": batch.get("started_at"),
                                                 "completed_at": batch.get("completed_at"), "output_dir": batch.get("output_dir")}
                            result = next((entry for entry in batch["jobs"] if entry.get("shot_id") == request.shot_id), None)
                            if not result or result.get("status") != "completed":
                                failure = str((result or {}).get("error", "result missing or failed"))
                                failed = {**attempt, "status": "failed", "completed_at": utc_now(), "error": failure, "kaggle": batch}
                                _update_attempt(manifest, request.shot_id, attempt["fingerprint"], "failed", manifest_file,
                                                **{k: v for k, v in failed.items() if k not in {"status", "fingerprint"}})
                                failed_shots.append(request.shot_id)
                                _append_run(runs_file, {"ts": utc_now(), "stage": "video", "shot_id": request.shot_id,
                                    "fingerprint": attempt["fingerprint"], "provider": provider.provider_id, "model": provider.model_id,
                                    "seed": request.seed, "parameters": request.fingerprint_payload(), "kaggle": batch,
                                    "success": False, "error": failure[:500]})
                                continue
                            raw = Path(batch["output_dir"]) / "results" / str(result["output"])
                            _update_attempt(manifest, request.shot_id, attempt["fingerprint"], "validating", manifest_file,
                                            kaggle=attempt["kaggle"])
                            with tempfile.TemporaryDirectory(prefix="vidforge-normalize-") as tmp:
                                normalized = Path(tmp) / "normalized.mp4"
                                source_probe = probe_video(raw, ffprobe, ffmpeg)
                                validation = _normalize(raw, normalized, request.fps)
                                cache_path, meta = cache.commit(attempt["fingerprint"], request.fingerprint_payload(), normalized,
                                    {**attempt, "status": "validating", "raw_output_sha256": sha256_file(raw),
                                     "source_validation": source_probe, "actual_duration_s": validation["duration_s"], "kaggle": batch,
                                     "resolution": [validation["width"], validation["height"]], "fps": validation["fps"]},
                                    force=force, ffprobe=ffprobe, ffmpeg=ffmpeg)
                            output = project_dir / "outputs" / "shots" / f"{request.shot_id}.mp4"
                            _copy_selected(cache_path, output)
                            done = {**attempt, "status": "completed", "completed_at": utc_now(), "cache_path": str(cache_path),
                                    "output_path": str(output), "output_sha256": meta["output_sha256"],
                                    "actual_duration_s": validation["duration_s"], "validation": validation, "kaggle": batch}
                            _update_attempt(manifest, request.shot_id, attempt["fingerprint"], "completed", manifest_file,
                                            **{k: v for k, v in done.items() if k not in {"status", "fingerprint"}})
                            _append_run(runs_file, {"ts": utc_now(), "stage": "video", "shot_id": request.shot_id,
                                "fingerprint": attempt["fingerprint"], "provider": provider.provider_id, "model": provider.model_id,
                                "seed": request.seed, "parameters": request.fingerprint_payload(), "kaggle": batch,
                                "duration_s": validation["duration_s"], "success": True})
                    except Exception as exc:
                        for request, attempt in jobs:
                            failed = {**attempt, "status": "failed", "completed_at": utc_now(), "error": str(exc)[:1200]}
                            _update_attempt(manifest, request.shot_id, attempt["fingerprint"], "failed", manifest_file,
                                            **{k: v for k, v in failed.items() if k not in {"status", "fingerprint"}})
                            _append_run(runs_file, {"ts": utc_now(), "stage": "video", "shot_id": request.shot_id,
                                "fingerprint": attempt["fingerprint"], "provider": provider.provider_id,
                                "model": provider.model_id, "seed": request.seed, "parameters": request.fingerprint_payload(),
                                "success": False, "error": str(exc)[:500]})
                        raise
        return 1 if failed_shots else 0
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project")
    parser.add_argument("--provider", choices=("wan22", "hunyuan15"), default="wan22")
    parser.add_argument("--shot")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--accelerator")
    parser.add_argument("--keyframes-dir", type=Path)
    parser.add_argument("--steps", type=int, default=DEFAULT_STEPS)
    parser.add_argument("--guidance", type=float, default=DEFAULT_GUIDANCE)
    args = parser.parse_args()
    sys.exit(run_video(Path("projects") / args.project, provider_name=args.provider, shot_id=args.shot,
                       force=args.force, dry_run=args.dry_run, validate_only=args.validate_only,
                       accelerator=args.accelerator, keyframes_dir=args.keyframes_dir,
                       generation_steps=args.steps, guidance=args.guidance))
