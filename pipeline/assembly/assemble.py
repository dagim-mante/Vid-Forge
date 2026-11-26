"""Assemble storyboard-ordered shot clips into one validated film."""
from __future__ import annotations

import hashlib
import importlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from fractions import Fraction
from typing import Any

OUTPUT_WIDTH, OUTPUT_HEIGHT = 1280, 720
OUTPUT_CODEC = "h264"
OUTPUT_PIXEL_FORMAT = "yuv420p"
OUTPUT_CONTAINER = "mp4"
DEFAULT_OUTPUT_NAME = "final.mp4"
DEFAULT_TRANSITION = "cut"
DEFAULT_TRANSITION_DURATION = 0.5
TRANSITION_FILTERS = {"crossfade": "fade"}
OUTPUT_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*\.mp4$")
SHOT_ID_PATTERN = re.compile(r"^shot_[0-9]{2,3}$")
FFPROBE_TIMEOUT_SECONDS = 120
FFMPEG_TIMEOUT_SECONDS = 3 * 60 * 60


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_json_atomic(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(data, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def ffmpeg_path() -> str:
    executable = shutil.which("ffmpeg")
    if executable is None:
        raise RuntimeError("FFmpeg was not found. Install FFmpeg and make sure `ffmpeg` is on your PATH.")
    return executable


def ffprobe_path() -> str:
    executable = shutil.which("ffprobe")
    if executable is None:
        raise RuntimeError("ffprobe was not found. Install FFmpeg (including ffprobe) and add it to PATH.")
    return executable


def _parse_rate(value: object) -> float:
    if not isinstance(value, str) or not value:
        raise ValueError("frame rate is missing")
    try:
        rate = float(Fraction(value))
    except (ValueError, ZeroDivisionError, TypeError) as exc:
        raise ValueError(f"invalid frame rate {value!r}") from exc
    if not math.isfinite(rate) or rate <= 0:
        raise ValueError(f"invalid frame rate {value!r}")
    return rate


def _probe(path: Path, executable: str) -> dict[str, Any]:
    if not path.is_file() or path.stat().st_size == 0:
        raise ValueError("file is missing or empty")
    try:
        result = subprocess.run(
            [executable, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
            capture_output=True, text=True, timeout=FFPROBE_TIMEOUT_SECONDS, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ValueError(f"ffprobe timed out inspecting {path.name}") from exc
    if result.returncode:
        raise ValueError(f"ffprobe could not read clip: {(result.stderr or result.stdout).strip()[-1000:]}")
    try:
        metadata = json.loads(result.stdout)
        streams = metadata["streams"]
        video_streams = [stream for stream in streams if stream.get("codec_type") == "video"]
        audio_streams = [stream for stream in streams if stream.get("codec_type") == "audio"]
        if not video_streams:
            raise ValueError("no readable video stream")
        stream = video_streams[0]
        format_duration = metadata.get("format", {}).get("duration")
        duration_value = stream.get("duration") or format_duration
        duration = float(duration_value)
        width, height = int(stream["width"]), int(stream["height"])
        rate = _parse_rate(stream.get("avg_frame_rate") or stream.get("r_frame_rate"))
        codec = str(stream.get("codec_name") or "")
        pixel_format = str(stream.get("pix_fmt") or "")
    except (KeyError, IndexError, TypeError, ValueError, ZeroDivisionError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid video stream metadata: {exc}") from exc
    if not math.isfinite(duration) or duration <= 0 or width <= 0 or height <= 0:
        raise ValueError("duration and frame dimensions must be positive")
    return {
        "duration_s": duration,
        "width": width,
        "height": height,
        "fps": rate,
        "codec": codec,
        "pixel_format": pixel_format,
        "audio_stream_count": len(audio_streams),
        "file_size": path.stat().st_size,
    }


def _decode(path: Path, ffmpeg: str) -> None:
    try:
        result = subprocess.run(
            [ffmpeg, "-hide_banner", "-v", "error", "-i", str(path), "-f", "null", "-"],
            capture_output=True, text=True, timeout=FFMPEG_TIMEOUT_SECONDS, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ValueError(f"full decode timed out for {path.name}") from exc
    if result.returncode:
        raise ValueError(f"full decode failed: {(result.stderr or result.stdout).strip()[-1000:]}")


def inspect_clip(path: Path, ffprobe: str, ffmpeg: str, *, allow_audio: bool = False) -> dict[str, Any]:
    """Probe and fully decode a clip; source shots are silent, final films may have audio."""
    info = _probe(path, ffprobe)
    if info["audio_stream_count"] and not allow_audio:
        raise ValueError("contains an audio stream; assembly currently accepts the silent per-shot video outputs only")
    _decode(path, ffmpeg)
    return info


def _validate_storyboard_file(path: Path, project_dir: Path) -> None:
    """Use the shared storyboard schema and semantic validator when available."""
    script_dir = Path(__file__).resolve().parents[1] / "script"
    if str(script_dir) not in sys.path:
        sys.path.insert(0, str(script_dir))
    try:
        validator = importlib.import_module("validate_storyboard")
    except ModuleNotFoundError as exc:
        if exc.name != "jsonschema":
            raise
        # Keep isolated assembly tools usable in minimal Python installs.
        return
    report = validator.validate_file(path, project_dir=project_dir)
    if not report.ok:
        raise ValueError("storyboard validation failed:\n" + report.format())


def _narration_inputs(project_dir: Path, shots: list[dict[str, Any]]) -> dict[str, Any]:
    narration_dir = project_dir / "outputs" / "narration"
    timing_path = narration_dir / "timing.json"
    voice_path = narration_dir / "voice_track.wav"
    subtitles_path = narration_dir / "narration_16x9.srt"
    subtitles_portrait_path = narration_dir / "narration_9x16.srt"
    music_path = project_dir / "outputs" / "radio_cut" / "placeholder_music.wav"
    missing = [path for path in (timing_path, voice_path, subtitles_path, subtitles_portrait_path) if not path.is_file()]
    if missing:
        paths = ", ".join(str(path) for path in missing)
        raise ValueError(f"Narration outputs are incomplete ({paths}); run `vid-forge narration project={project_dir.name}` first")
    try:
        timing = json.loads(timing_path.read_text(encoding="utf-8"))
        shot_timings = timing["shots"]
        total_s = float(timing["total_s"])
        durations = {item["id"]: float(item["duration_s"]) for item in shot_timings}
        timing_ids = [item["id"] for item in shot_timings]
        end_card_s = float(timing["end_card_s"])
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"Invalid narration timing file {timing_path}: {exc}") from exc
    storyboard_ids = [shot["id"] for shot in shots]
    if timing_ids != storyboard_ids:
        raise ValueError("narration timing shot order does not match storyboard; rerun the narration stage")
    if any(not math.isfinite(duration) or duration <= 0 for duration in durations.values()):
        raise ValueError("narration timing has a non-positive or invalid shot duration")
    if not math.isfinite(total_s) or total_s <= 0 or not math.isfinite(end_card_s) or end_card_s < 0:
        raise ValueError("narration timing has an invalid total or end-card duration")
    calculated = sum(durations.values()) + end_card_s
    if abs(calculated - total_s) > 0.05:
        raise ValueError(f"narration timing total ({total_s:.3f}s) does not match shot and end-card durations ({calculated:.3f}s)")
    return {
        "timing": timing, "timing_path": timing_path, "shot_durations": durations,
        "total_s": total_s, "end_card_s": end_card_s, "voice_path": voice_path,
        "subtitles_path": subtitles_path, "subtitles_portrait_path": subtitles_portrait_path,
        "music_path": music_path,
    }


def _storyboard(project_dir: Path) -> tuple[dict[str, Any], list[dict[str, Any]], int]:
    path = project_dir / "production" / "storyboard.json"
    if not path.is_file():
        raise ValueError(f"Storyboard not found: {path}")
    _validate_storyboard_file(path, project_dir)
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not read storyboard {path}: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("shots"), list) or not data["shots"]:
        raise ValueError("storyboard.shots must be a non-empty array")
    fps = data.get("meta", {}).get("fps") if isinstance(data.get("meta"), dict) else None
    if isinstance(fps, bool) or not isinstance(fps, int) or fps <= 0:
        raise ValueError("storyboard.meta.fps must be a positive integer")
    shots: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, shot in enumerate(data["shots"], 1):
        if not isinstance(shot, dict):
            raise ValueError(f"storyboard.shots[{index - 1}] must be an object")
        shot_id = shot.get("id")
        if not isinstance(shot_id, str) or not SHOT_ID_PATTERN.fullmatch(shot_id):
            raise ValueError(f"storyboard.shots[{index - 1}].id must match shot_NN or shot_NNN")
        if shot_id in seen:
            raise ValueError(f"duplicate shot id in storyboard: {shot_id}")
        seen.add(shot_id)
        shots.append(shot)
    return data, shots, fps


def _output_path(project_dir: Path, output_name: str | None) -> Path:
    name = output_name or DEFAULT_OUTPUT_NAME
    if not OUTPUT_NAME_PATTERN.fullmatch(name):
        raise ValueError("--output must be a simple .mp4 filename (for example, final-v2.mp4)")
    return project_dir / "production" / name


def _output_paths(project_dir: Path, output_name: str | None) -> dict[str, Path]:
    landscape = _output_path(project_dir, output_name)
    portrait = landscape.with_name(f"{landscape.stem}_9x16{landscape.suffix}")
    return {"16x9": landscape, "9x16": portrait}


def calculate_duration(durations: list[float], transition: str, transition_duration: float) -> float:
    if transition == "cut":
        return sum(durations)
    if transition == "crossfade":
        return sum(durations) - max(0, len(durations) - 1) * transition_duration
    raise ValueError(f"unsupported transition {transition!r}")


def _validate_transition(transition: str, transition_duration: float, durations: list[float]) -> None:
    if transition not in ("cut", *TRANSITION_FILTERS):
        raise ValueError(f"unsupported transition {transition!r}; choose cut or crossfade")
    if not math.isfinite(transition_duration) or transition_duration <= 0:
        raise ValueError("transition duration must be a positive finite number")
    if transition == "crossfade":
        for left, right in zip(durations, durations[1:]):
            if transition_duration >= min(left, right):
                raise ValueError(
                    f"crossfade duration {transition_duration:.3f}s must be shorter than each adjacent clip; "
                    f"found adjacent durations {left:.3f}s and {right:.3f}s"
                )


def build_filter_graph(
    clip_count: int,
    fps: int,
    durations: list[float],
    transition: str,
    transition_duration: float,
    end_card_duration: float = 0.0,
    width: int = OUTPUT_WIDTH,
    height: int = OUTPUT_HEIGHT,
) -> tuple[str, str]:
    if clip_count < 1 or len(durations) != clip_count:
        raise ValueError("filter graph needs one duration for each input clip")
    _validate_transition(transition, transition_duration, durations)
    filters: list[str] = []
    for index in range(clip_count):
        duration = durations[index]
        filters.append(
            f"[{index}:v:0]scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},fps={fps},setsar=1,format={OUTPUT_PIXEL_FORMAT},"
            f"tpad=stop_mode=clone:stop_duration={duration:.6f},trim=duration={duration:.6f},"
            f"settb=AVTB,setpts=PTS-STARTPTS[v{index}]"
        )
        if transition == "crossfade" and index < clip_count - 1:
            filters[-1] = filters[-1].replace(
                f"[v{index}]", f"[v{index}_base]", 1
            ) + f";[v{index}_base]tpad=stop_mode=clone:stop_duration={transition_duration:.6f}[v{index}]"
    if transition == "cut":
        inputs = "".join(f"[v{index}]" for index in range(clip_count))
        filters.append(f"{inputs}concat=n={clip_count}:v=1:a=0[vshots]")
        current = "vshots"
    else:
        current = "v0"
        for index in range(1, clip_count):
            output_label = f"xfade{index}"
            # Each previous input is extended by the transition duration, so
            # the xfade begins at the storyboard boundary without shortening
            # the authored narration timeline.
            offset = sum(durations[:index])
            filters.append(
                f"[{current}][v{index}]xfade=transition={TRANSITION_FILTERS[transition]}:"
                f"duration={transition_duration:.6f}:offset={offset:.6f}[{output_label}]"
            )
            current = output_label
        filters.append(f"[{current}]null[vshots]")

    if end_card_duration > 0:
        filters.append(
            f"color=c=black:s={width}x{height}:r={fps}:d={end_card_duration:.6f},"
            f"format={OUTPUT_PIXEL_FORMAT},setsar=1[vendcard]"
        )
        filters.append("[vshots][vendcard]concat=n=2:v=1:a=0[vout]")
    else:
        filters.append("[vshots]null[vout]")
    return ";".join(filters), "vout"


def _run_ffmpeg(command: list[str], *, timeout: int = FFMPEG_TIMEOUT_SECONDS) -> None:
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"FFmpeg assembly timed out after {timeout} seconds") from exc
    if result.returncode:
        detail = (result.stderr or result.stdout or "no FFmpeg output").strip()[-2000:]
        raise RuntimeError(f"FFmpeg assembly failed (exit {result.returncode}): {detail}")


def _radio_cut_module():
    narration_scripts = Path(__file__).resolve().parents[1] / "narration"
    if str(narration_scripts) not in sys.path:
        sys.path.insert(0, str(narration_scripts))
    return importlib.import_module("radio_cut")


def _subtitle_filter_path(path: Path) -> str:
    return str(path.resolve()).replace("\\", "/").replace(":", r"\:").replace("'", r"\'")


def _validate_final(
    path: Path, ffprobe: str, ffmpeg: str, fps: int, estimated_duration: float,
    width: int = OUTPUT_WIDTH, height: int = OUTPUT_HEIGHT,
) -> dict[str, Any]:
    info = inspect_clip(path, ffprobe, ffmpeg, allow_audio=True)
    if not info["audio_stream_count"]:
        raise ValueError("final output has no audio stream")
    if (info["width"], info["height"]) != (width, height):
        raise ValueError(f"final resolution is {info['width']}x{info['height']}, expected {width}x{height}")
    if info["codec"] != OUTPUT_CODEC:
        raise ValueError(f"final codec is {info['codec']!r}, expected {OUTPUT_CODEC!r}")
    if info["pixel_format"] != OUTPUT_PIXEL_FORMAT:
        raise ValueError(f"final pixel format is {info['pixel_format']!r}, expected {OUTPUT_PIXEL_FORMAT!r}")
    if abs(info["fps"] - fps) > 0.01:
        raise ValueError(f"final FPS is {info['fps']:.3f}, expected {fps}")
    tolerance = max(1.0 / fps, 0.05)
    if abs(info["duration_s"] - estimated_duration) > tolerance:
        raise ValueError(
            f"final duration is {info['duration_s']:.3f}s, expected about {estimated_duration:.3f}s "
            f"(tolerance {tolerance:.3f}s)"
        )
    return info


def assemble_project(
    project_dir: Path,
    *,
    transition: str = DEFAULT_TRANSITION,
    transition_duration: float = DEFAULT_TRANSITION_DURATION,
    output_name: str | None = None,
    dry_run: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    """Validate inputs and optionally render a final film; raises on any failure."""
    if not project_dir.is_dir():
        raise ValueError(f"Project directory does not exist: {project_dir}")
    ffmpeg, ffprobe = ffmpeg_path(), ffprobe_path()
    _, shots, fps = _storyboard(project_dir)
    narration = _narration_inputs(project_dir, shots)
    output_paths = _output_paths(project_dir, output_name)
    output = output_paths["16x9"]
    existing_outputs = [path for path in output_paths.values() if path.exists()]
    shot_dir = project_dir / "outputs" / "shots"
    missing: list[str] = []
    invalid: list[dict[str, str]] = []
    clips: list[dict[str, Any]] = []
    for shot in shots:
        shot_id = shot["id"]
        path = shot_dir / f"{shot_id}.mp4"
        if not path.is_file():
            missing.append(shot_id)
            continue
        try:
            info = inspect_clip(path, ffprobe, ffmpeg)
            clips.append({"shot_id": shot_id, "path": path, **info})
        except (OSError, ValueError, RuntimeError) as exc:
            invalid.append({"shot_id": shot_id, "path": str(path), "error": str(exc)})

    timeline_durations = [narration["shot_durations"][shot["id"]] for shot in shots]
    _validate_transition(transition, transition_duration, timeline_durations if not missing and not invalid else [])
    estimated = narration["total_s"]
    report: dict[str, Any] = {
        "project": project_dir.name,
        "shot_ids": [shot["id"] for shot in shots],
        "storyboard_shot_count": len(shots),
        "available_clip_count": len(shots) - len(missing),
        "valid_clip_count": len(clips),
        "missing_shots": missing,
        "invalid_shots": invalid,
        "clips": clips,
        "targets": {
            "16x9": {"width": OUTPUT_WIDTH, "height": OUTPUT_HEIGHT, "fps": fps,
                     "codec": OUTPUT_CODEC, "pixel_format": OUTPUT_PIXEL_FORMAT, "container": OUTPUT_CONTAINER},
            "9x16": {"width": OUTPUT_HEIGHT, "height": OUTPUT_WIDTH, "fps": fps,
                     "codec": OUTPUT_CODEC, "pixel_format": OUTPUT_PIXEL_FORMAT, "container": OUTPUT_CONTAINER},
        },
        "transition": transition,
        "transition_duration_s": transition_duration,
        "estimated_final_duration_s": estimated if not missing and not invalid else None,
        "narration_timing_path": str(narration["timing_path"]),
        "voice_track_path": str(narration["voice_path"]),
        "subtitles_path": str(narration["subtitles_path"]),
        "portrait_subtitles_path": str(narration["subtitles_portrait_path"]),
        "placeholder_music_path": str(narration["music_path"]),
        "end_card_duration_s": narration["end_card_s"],
        "output_path": str(output),
        "portrait_output_path": str(output_paths["9x16"]),
        "output_paths": {aspect: str(path) for aspect, path in output_paths.items()},
        "existing_outputs": [str(path) for path in existing_outputs],
        "force": force,
        "dry_run": dry_run,
    }
    print(f"Assembly plan for {project_dir.name}: {len(shots)} storyboard shot(s), {len(clips)} available clip(s).")
    print(f"Targets: {OUTPUT_WIDTH}x{OUTPUT_HEIGHT} (16:9) and {OUTPUT_HEIGHT}x{OUTPUT_WIDTH} (9:16) at {fps} fps, H.264/{OUTPUT_PIXEL_FORMAT}; transition={transition}.")
    print(f"Narration timeline: {narration['total_s']:.3f}s; subtitles will be burned in; end card: {narration['end_card_s']:.3f}s.")
    music_state = "present" if narration["music_path"].is_file() else "will be generated during render"
    print(
        f"Audio: {narration['voice_path']} + {narration['music_path']} ({music_state}); "
        f"subtitles: {narration['subtitles_path']} (16:9), {narration['subtitles_portrait_path']} (9:16)"
    )
    print(f"Transition duration: {transition_duration:.3f}s; outputs: {output} and {output_paths['9x16']}")
    if missing:
        print("Missing clips: " + ", ".join(missing), file=sys.stderr)
    for item in invalid:
        print(f"Invalid clip {item['shot_id']} ({item['path']}): {item['error']}", file=sys.stderr)
    for clip in clips:
        print(f"  {clip['shot_id']}: {clip['duration_s']:.3f}s, {clip['width']}x{clip['height']} at {clip['fps']:.3f} fps")
    if not missing and not invalid:
        print(f"Estimated final duration: {estimated:.3f}s")
    else:
        print("Estimated final duration: unavailable until every storyboard clip is valid.")
    if existing_outputs and not force:
        print("Output(s) already exist; pass --force to replace both: " + ", ".join(str(path) for path in existing_outputs), file=sys.stderr)
    if missing or invalid:
        raise ValueError("assembly stopped because one or more storyboard shots are missing or invalid")
    if existing_outputs and not force and not dry_run:
        raise FileExistsError("Refusing to overwrite existing output(s) without --force: " + ", ".join(str(path) for path in existing_outputs))
    if dry_run:
        print("Dry run complete; no video or manifest was changed.")
        return report

    durations = [narration["shot_durations"][clip["shot_id"]] for clip in clips]
    radio_cut = _radio_cut_module()
    music_path = narration["music_path"]
    if not music_path.is_file():
        radio_cut.make_placeholder_music(ffmpeg_path(), narration["total_s"], music_path)
    loudnorm = f"loudnorm={radio_cut.LOUDNESS_TARGET}"
    voice_index = len(clips)
    music_index = voice_index + 1
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary_paths = {
        aspect: path.with_name(f".{path.stem}.{uuid.uuid4().hex}.tmp.mp4")
        for aspect, path in output_paths.items()
    }
    commands: dict[str, list[str]] = {}
    filter_graphs: dict[str, str] = {}
    for aspect, (width, height, subtitles_path) in {
        "16x9": (OUTPUT_WIDTH, OUTPUT_HEIGHT, narration["subtitles_path"]),
        "9x16": (OUTPUT_HEIGHT, OUTPUT_WIDTH, narration["subtitles_portrait_path"]),
    }.items():
        filter_graph, _ = build_filter_graph(
            len(clips), fps, durations, transition, transition_duration,
            narration["end_card_s"], width=width, height=height,
        )
        escaped_subtitles = _subtitle_filter_path(subtitles_path)
        filter_graph += (
            f";[vout]subtitles='{escaped_subtitles}':force_style='Fontsize=24,Fontname=Arial,"
            "PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,Outline=2,Shadow=1'[vfinal]"
            f";{radio_cut._mix_graph(voice_index, music_index)};[mix]{loudnorm},atrim=duration={estimated:.6f},"
            "asetpts=PTS-STARTPTS[afinal]"
        )
        command = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error"]
        for clip in clips:
            command += ["-i", str(clip["path"])]
        command += ["-i", str(narration["voice_path"]), "-stream_loop", "-1", "-i", str(music_path)]
        command += [
            "-filter_complex", filter_graph,
            "-map", "[vfinal]", "-map", "[afinal]",
            "-c:v", "libx264", "-preset", "medium", "-crf", "18",
            "-c:a", "aac", "-b:a", "160k",
            "-pix_fmt", OUTPUT_PIXEL_FORMAT, "-r", str(fps),
            "-t", f"{estimated:.6f}",
            "-movflags", "+faststart", str(temporary_paths[aspect]),
        ]
        commands[aspect] = command
        filter_graphs[aspect] = filter_graph

    validations: dict[str, dict[str, Any]] = {}
    output_hashes: dict[str, str] = {}
    try:
        for aspect, command in commands.items():
            _run_ffmpeg(command)
            width = OUTPUT_WIDTH if aspect == "16x9" else OUTPUT_HEIGHT
            height = OUTPUT_HEIGHT if aspect == "16x9" else OUTPUT_WIDTH
            validations[aspect] = _validate_final(
                temporary_paths[aspect], ffprobe, ffmpeg, fps, estimated, width, height,
            )
            output_hashes[aspect] = sha256_file(temporary_paths[aspect])
        # Both encodes are complete and validated before either final is replaced.
        for aspect, destination in output_paths.items():
            os.replace(temporary_paths[aspect], destination)
        manifest = {
            "schema_version": 1,
            "project": project_dir.name,
            "created_at": utc_now(),
            "status": "completed",
            "ordered_shot_ids": [clip["shot_id"] for clip in clips],
            "input_clips": [
                {"shot_id": clip["shot_id"], "path": clip["path"].relative_to(project_dir).as_posix(),
                 "sha256": sha256_file(clip["path"]), "duration_s": clip["duration_s"],
                 "width": clip["width"], "height": clip["height"], "fps": clip["fps"],
                 "codec": clip["codec"], "pixel_format": clip["pixel_format"]}
                for clip in clips
            ],
            "transition": {"type": transition, "requested_duration_s": transition_duration,
                           "overlap_count": max(0, len(clips) - 1) if transition == "crossfade" else 0,
                           "overlap_duration_s": transition_duration if transition == "crossfade" else 0.0},
            "targets": report["targets"],
            "narration": {
                "timing_path": narration["timing_path"].relative_to(project_dir).as_posix(),
                "timing_sha256": sha256_file(narration["timing_path"]),
                "voice_track_path": narration["voice_path"].relative_to(project_dir).as_posix(),
                "voice_track_sha256": sha256_file(narration["voice_path"]),
                "subtitles_path": narration["subtitles_path"].relative_to(project_dir).as_posix(),
                "subtitles_sha256": sha256_file(narration["subtitles_path"]),
                "portrait_subtitles_path": narration["subtitles_portrait_path"].relative_to(project_dir).as_posix(),
                "portrait_subtitles_sha256": sha256_file(narration["subtitles_portrait_path"]),
                "music_path": music_path.relative_to(project_dir).as_posix(),
                "music_sha256": sha256_file(music_path),
                "duration_s": narration["total_s"],
                "subtitles_burned_in": True,
                "music_gain": radio_cut.MUSIC_GAIN,
                "duck_settings": radio_cut.DUCK_SETTINGS,
                "loudness_target": radio_cut.LOUDNESS_TARGET,
            },
            "outputs": {
                aspect: {
                    "path": output_paths[aspect].relative_to(project_dir).as_posix(),
                    "sha256": output_hashes[aspect],
                    "duration_s": validations[aspect]["duration_s"],
                    "width": validations[aspect]["width"],
                    "height": validations[aspect]["height"],
                    "fps": validations[aspect]["fps"],
                    "codec": validations[aspect]["codec"],
                    "pixel_format": validations[aspect]["pixel_format"],
                    "file_size": validations[aspect]["file_size"],
                }
                for aspect in output_paths
            },
            "estimated_duration_s": estimated,
            "ffmpeg": {"executable": ffmpeg, "filter_graphs": filter_graphs, "commands": commands},
            "validation": {"ffprobe": True, "full_decode": True,
                           "expected_duration_tolerance_s": max(1.0 / fps, 0.05)},
        }
        write_json_atomic(project_dir / "production" / "assembly_manifest.json", manifest)
    finally:
        for temporary in temporary_paths.values():
            temporary.unlink(missing_ok=True)

    print(f"Assembled {len(clips)} shots: {output_paths['16x9']} and {output_paths['9x16']}")
    print(f"Actual final duration: 16:9 {validations['16x9']['duration_s']:.3f}s; 9:16 {validations['9x16']['duration_s']:.3f}s")
    report["actual_final_duration_s"] = validations["16x9"]["duration_s"]
    report["output_sha256"] = output_hashes["16x9"]
    report["output_hashes"] = output_hashes
    return report


def run_assembly(
    project_dir: Path,
    *,
    transition: str = DEFAULT_TRANSITION,
    transition_duration: float = DEFAULT_TRANSITION_DURATION,
    output_name: str | None = None,
    dry_run: bool = False,
    force: bool = False,
) -> int:
    try:
        assemble_project(
            project_dir, transition=transition, transition_duration=transition_duration,
            output_name=output_name, dry_run=dry_run, force=force,
        )
        return 0
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project")
    parser.add_argument("--transition", choices=("cut", "crossfade"), default=DEFAULT_TRANSITION)
    parser.add_argument("--transition-duration", type=float, default=DEFAULT_TRANSITION_DURATION)
    parser.add_argument("--output")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    sys.exit(run_assembly(
        Path("projects") / args.project,
        transition=args.transition,
        transition_duration=args.transition_duration,
        output_name=args.output,
        dry_run=args.dry_run,
        force=args.force,
    ))
