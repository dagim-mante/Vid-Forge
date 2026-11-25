"""Radio cut: the narration over placeholder music on a black screen.

This is the cheap prototype used for the first audience test. It needs no GPU
and no image or video models: the music is a synthetic pad made by FFmpeg, the
picture is black, and the voice comes from the narration stage.

Reads  outputs/narration/timing.json and outputs/narration/voice_track.wav
Writes outputs/radio_cut/placeholder_music.wav and outputs/radio_cut/radio_cut.mp4
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from typing import Any

from stage_io import append_run_record, narration_dir, radio_cut_dir, rel, utc_now

VIDEO_SIZE = "1280x720"
FPS = 24
# A warm A-major pad: A2, A3, C#4, E4, A4.
CHORD_HZ = (110.0, 220.0, 277.18, 329.63, 440.0)
MUSIC_GAIN = 0.6  # before ducking
DUCK_SETTINGS = "threshold=0.03:ratio=10:attack=20:release=600"
LOUDNESS_TARGET = "I=-16:TP=-1.5:LRA=11"
FFMPEG_TIMEOUT_SECONDS = 600


def find_ffmpeg() -> str:
    path = shutil.which("ffmpeg")
    if path is None:
        raise RuntimeError("FFmpeg not found. Install it and make sure `ffmpeg` is on your PATH.")
    return path


def _has_encoder(ffmpeg: str, name: str) -> bool:
    result = subprocess.run(
        [ffmpeg, "-hide_banner", "-encoders"], capture_output=True, text=True, check=False
    )
    return any(line.split()[1:2] == [name] for line in result.stdout.splitlines())


def _run(command: list[str]) -> None:
    result = subprocess.run(
        command, capture_output=True, text=True, timeout=FFMPEG_TIMEOUT_SECONDS, check=False
    )
    if result.returncode != 0:
        tail = (result.stderr or result.stdout).strip()[-800:]
        raise RuntimeError(f"FFmpeg failed (exit {result.returncode}): {tail}")


def make_placeholder_music(ffmpeg: str, seconds: float, output: Path) -> None:
    """A slow, quiet synthetic pad that fades in and out. Deterministic, so the cut is reproducible."""
    length = f"{seconds:.3f}"
    command = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error"]
    for frequency in CHORD_HZ:
        command += ["-f", "lavfi", "-i", f"sine=frequency={frequency}:sample_rate=48000:duration={length}"]
    inputs = "".join(f"[{index}:a]" for index in range(len(CHORD_HZ)))
    graph = (
        f"{inputs}amix=inputs={len(CHORD_HZ)}:duration=longest,"
        "tremolo=f=0.2:d=0.5,lowpass=f=1200,"
        f"afade=t=in:st=0:d=3,afade=t=out:st={max(seconds - 4, 0):.3f}:d=4,"
        "aformat=sample_fmts=s16:channel_layouts=stereo[music]"
    )
    command += ["-filter_complex", graph, "-map", "[music]", str(output)]
    output.parent.mkdir(parents=True, exist_ok=True)
    _run(command)


def _mix_graph(voice_index: int, music_index: int) -> str:
    """Voice plus music ducked under the voice. Ends at the pad named [mix]."""
    return (
        f"[{voice_index}:a]aresample=48000,aformat=channel_layouts=stereo,asplit=2[voice][sidechain];"
        f"[{music_index}:a]aresample=48000,aformat=channel_layouts=stereo,volume={MUSIC_GAIN}[music];"
        f"[music][sidechain]sidechaincompress={DUCK_SETTINGS}[bed];"
        "[voice][bed]amix=inputs=2:duration=longest[mix]"
    )


def measure_loudness(ffmpeg: str, voice: Path, music: Path) -> dict[str, str] | None:
    """First loudnorm pass: measure the mix. Returns None if the numbers cannot be read."""
    graph = _mix_graph(0, 1) + f";[mix]loudnorm={LOUDNESS_TARGET}:print_format=json[measured]"
    result = subprocess.run(
        [ffmpeg, "-hide_banner", "-nostats", "-i", str(voice), "-i", str(music),
         "-filter_complex", graph, "-map", "[measured]", "-f", "null", "-"],
        capture_output=True, text=True, timeout=FFMPEG_TIMEOUT_SECONDS, check=False,
    )
    match = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", result.stderr, flags=re.DOTALL)
    if result.returncode != 0 or match is None:
        return None
    try:
        values = json.loads(match.group(0))
        return {key: str(values[key]) for key in ("input_i", "input_tp", "input_lra", "input_thresh", "target_offset")}
    except (json.JSONDecodeError, KeyError):
        return None


def render_radio_cut(ffmpeg: str, voice: Path, music: Path, seconds: float, output: Path) -> dict[str, Any]:
    """Black video + voice + music ducked under the voice, loudness normalised in two passes.

    Single-pass loudnorm undershoots badly on a mix that is mostly music, so the mix is
    measured first and the second pass applies one fixed gain. Returns {"warnings", "measured_lufs"}.
    """
    warnings: list[str] = []
    length = f"{seconds:.3f}"

    measured = measure_loudness(ffmpeg, voice, music)
    if measured is not None:
        loudnorm = (
            f"loudnorm={LOUDNESS_TARGET}:measured_I={measured['input_i']}:measured_TP={measured['input_tp']}"
            f":measured_LRA={measured['input_lra']}:measured_thresh={measured['input_thresh']}"
            f":offset={measured['target_offset']}:linear=true"
        )
    else:
        loudnorm = f"loudnorm={LOUDNESS_TARGET}"
        warnings.append("could not measure loudness; used single-pass normalisation, so the level may be low")
    graph = _mix_graph(1, 2) + f";[mix]{loudnorm}[audio]"

    if _has_encoder(ffmpeg, "libx264"):
        video_codec = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "30", "-tune", "stillimage"]
    else:
        video_codec = ["-c:v", "mpeg4", "-q:v", "5"]
        warnings.append("libx264 not available in this FFmpeg build; used mpeg4 for the video")
    command = [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", f"color=c=black:s={VIDEO_SIZE}:r={FPS}:d={length}",
        "-i", str(voice),
        "-i", str(music),
        "-filter_complex", graph,
        "-map", "0:v", "-map", "[audio]",
        *video_codec, "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "160k",
        "-t", length, "-movflags", "+faststart",
        str(output),
    ]
    output.parent.mkdir(parents=True, exist_ok=True)
    _run(command)
    return {"warnings": warnings, "measured_lufs": float(measured["input_i"]) if measured else None}


def run_radio_cut(project_dir: Path, *, base_dir: Path) -> int:
    """Build outputs/radio_cut/radio_cut.mp4 from the narration outputs. Returns an exit code."""
    narration_folder = narration_dir(project_dir)
    timing_path = narration_folder / "timing.json"
    voice_path = narration_folder / "voice_track.wav"
    out_dir = radio_cut_dir(project_dir)
    music_path = out_dir / "placeholder_music.wav"
    output = out_dir / "radio_cut.mp4"
    runs_file = project_dir / "runs.jsonl"

    if not timing_path.is_file() or not voice_path.is_file():
        print(
            f"Error: narration outputs not found in {rel(narration_folder, base_dir)}/. "
            f"Run: narration project={project_dir.name}",
            file=sys.stderr,
        )
        return 1

    timing: dict[str, Any] = json.loads(timing_path.read_text(encoding="utf-8"))
    seconds = float(timing["total_s"])

    record: dict[str, Any] = {
        "ts": utc_now(),
        "stage": "radio_cut",
        "provider": "ffmpeg",
        "operation": "radio_cut_render",
        "model": "ffmpeg",
        "params": {
            "video": VIDEO_SIZE,
            "fps": FPS,
            "music": "placeholder_pad",
            "music_gain": MUSIC_GAIN,
            "duck": DUCK_SETTINGS,
            "loudness_target": LOUDNESS_TARGET,
        },
        "total_s": seconds,
    }
    start = time.perf_counter()
    try:
        ffmpeg = find_ffmpeg()
        make_placeholder_music(ffmpeg, seconds, music_path)
        result = render_radio_cut(ffmpeg, voice_path, music_path, seconds, output)
    except Exception as exc:  # noqa: BLE001 - log any failure
        record.update(success=False, latency_ms=round((time.perf_counter() - start) * 1000), error=str(exc)[:500])
        append_run_record(runs_file, record)
        print(f"[FAIL] radio cut: {exc}", file=sys.stderr)
        return 1

    latency_ms = round((time.perf_counter() - start) * 1000)
    warnings = result["warnings"]
    record.update(
        success=True,
        latency_ms=latency_ms,
        output=rel(output, base_dir),
        measured_lufs_before_normalising=result["measured_lufs"],
    )
    append_run_record(runs_file, record)
    print(f"[PASS] radio cut rendered in {latency_ms}ms")
    for message in warnings:
        print(f"  warning: {message}")
    print(f"\nWrote {rel(output, base_dir)} ({seconds:g}s, narration over placeholder music, black screen)")
    return 0