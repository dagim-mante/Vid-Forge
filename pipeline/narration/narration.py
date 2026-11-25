"""Narration stage: storyboard narration lines -> Gemini TTS -> timing -> radio cut.

For each line in production/storyboard.json this stage:
    1. synthesises the line with Gemini TTS (cached, retried, logged),
    2. measures it and works out when every line starts on the film's timeline,
    3. writes a full-length voice track and English subtitles,
    4. assembles the radio cut: narration over placeholder music on black.

Writes under projects/<name>/outputs/narration/:
    line_01.wav ...        one file per narration line, plus line_01.wav.meta.json (cache key)
    timing.json            derived timings; the contract itself is never modified
    voice_track.wav        every line placed on the timeline, silence elsewhere
    narration_16x9.srt     English subtitles (also narration_9x16.srt, wrapped narrower)
Every API call is logged to projects/<name>/runs.jsonl.
"""

from __future__ import annotations

from array import array
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import random
import re
import sys
import textwrap
import time
import wave
import base64
from typing import Any

from dotenv import load_dotenv
from google import genai
from google.genai import errors as genai_errors
from google.genai import types

# The storyboard contract and its validator live in the script stage.
_SCRIPT_DIR = Path(__file__).resolve().parents[1] / "script"
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

import radio_cut  # noqa: E402
from stage_io import (  # noqa: E402
    append_run_record,
    narration_dir,
    rel,
    utc_now,
    write_json_atomic,
    write_text_atomic,
)
from validate_storyboard import ROOT_DIR, storyboard_path, validate_file  # noqa: E402

ENV_FILE = ROOT_DIR / ".env"
# The model id is a configuration value: set NARRATION_MODEL in .env to the id shown in AI Studio.
DEFAULT_TTS_MODEL = "gemini-3.8-flash-lite-tts"
DEFAULT_VOICE = "Kore"
DEFAULT_SAMPLE_RATE = 24000  # Gemini TTS returns 16-bit mono PCM at 24 kHz
STYLE_TEMPLATE = "Say in a {style} voice Only include the text between <start> and <end> in the output audio: <start>{text}<end>"

LEAD_S = 0.3  # silence before a line starts inside its shot
TAIL_PAD_S = 0.4  # breathing room after a line ends
MIN_AUDIO_S = 0.3
MAX_AUDIO_S = 60.0

MAX_ATTEMPTS = 5  # API calls per line, for rate limits, server errors and empty audio
BACKOFF_START_S = 4.0
BACKOFF_MAX_S = 60.0
TRANSIENT_CODES = {429, 500, 502, 503, 504}
SUBTITLE_WIDTHS = {"16x9": 42, "9x16": 28}  # characters per line, two lines per cue


class BadAudioError(RuntimeError):
    """The API answered but the audio is missing, silent or implausible. Worth retrying."""


# --------------------------------------------------------------------------- audio helpers


def _samples_from_pcm(pcm: bytes) -> array:
    samples = array("h")
    samples.frombytes(pcm[: len(pcm) // 2 * 2])
    if sys.byteorder == "big":
        samples.byteswap()
    return samples


def _write_wav(path: Path, samples: array, rate: int) -> None:
    """Write 16-bit mono WAV atomically."""
    data = array("h", samples)
    if sys.byteorder == "big":
        data.byteswap()
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    with wave.open(str(temp), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(data.tobytes())
    os.replace(temp, path)


def _read_wav(path: Path) -> tuple[array, int]:
    with wave.open(str(path), "rb") as handle:
        if handle.getnchannels() != 1 or handle.getsampwidth() != 2:
            raise RuntimeError(f"{path.name}: expected 16-bit mono audio")
        return _samples_from_pcm(handle.readframes(handle.getnframes())), handle.getframerate()


def _wav_seconds(path: Path) -> float:
    with wave.open(str(path), "rb") as handle:
        return handle.getnframes() / handle.getframerate()


def _extract_audio(response: Any) -> tuple[bytes, str]:
    for candidate in getattr(response, "candidates", None) or []:
        content = getattr(candidate, "content", None)
        for part in getattr(content, "parts", None) or []:
            inline = getattr(part, "inline_data", None)
            if inline is not None and getattr(inline, "data", None):
                return inline.data, getattr(inline, "mime_type", "") or ""
    raise BadAudioError("the model returned no audio (it may have answered with text)")


def _to_pcm(data: bytes, mime_type: str) -> tuple[bytes, int]:
    """Return raw 16-bit mono PCM and its sample rate, whether the API sent PCM or a WAV."""
    if data[:4] == b"RIFF":
        with wave.open(io.BytesIO(data), "rb") as handle:
            if handle.getnchannels() != 1 or handle.getsampwidth() != 2:
                raise BadAudioError("the audio is not 16-bit mono")
            return handle.readframes(handle.getnframes()), handle.getframerate()
    match = re.search(r"rate=(\d+)", mime_type)
    return data, int(match.group(1)) if match else DEFAULT_SAMPLE_RATE


def _check_audio(pcm: bytes, rate: int) -> None:
    samples = _samples_from_pcm(pcm)
    seconds = len(samples) / rate
    if seconds < MIN_AUDIO_S:
        raise BadAudioError(f"audio is only {seconds:.2f}s long")
    if seconds > MAX_AUDIO_S:
        raise BadAudioError(f"audio is {seconds:.0f}s long, which is implausible for one line")
    if max(max(samples), -min(samples)) == 0:
        raise BadAudioError("audio is silent")


# --------------------------------------------------------------------------- Gemini TTS


def _build_prompt(text: str, style: str) -> str:
    style = (style or "").strip()
    return STYLE_TEMPLATE.format(style=style, text=text) if style else text


def _cache_key(model: str, voice: str, prompt: str) -> str:
    payload = {"stage": "narration", "model": model, "voice": voice, "prompt": prompt}
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _is_fresh(wav: Path, meta: Path, key: str) -> bool:
    if not (wav.is_file() and meta.is_file()):
        return False
    try:
        return json.loads(meta.read_text(encoding="utf-8")).get("key") == key
    except (OSError, json.JSONDecodeError):
        return False


def _call_tts(client: genai.Client, model: str, voice: str, prompt: str) -> tuple[bytes, int, Any]:
    """One API call. Returns (pcm, sample_rate, response) or raises."""
    # Use the Interactions API which is the correct endpoint for Gemini TTS models
    interaction = client.interactions.create(
        model=model,
        input=[{
            "type": "user_input",
            "content": [{
                "type": "text",
                "text": prompt,
            }],
        }],
        response_format={"type": "audio"},
        generation_config={
            "speech_config": [
                {"voice": voice},
            ]
        },
    )
    
    # The interactions API returns base64-encoded audio data
    audio_data = base64.b64decode(interaction.output_audio.data)
    mime_type = getattr(interaction.output_audio, "mime_type", "audio/wav")
    
    # Reuse your existing _to_pcm helper which already handles WAV headers correctly
    pcm, rate = _to_pcm(audio_data, mime_type)
    _check_audio(pcm, rate)
    
    # Return interaction instead of response; _synthesize_line safely uses getattr 
    # for usage_metadata, so it will gracefully handle the different response object
    return pcm, rate, interaction


def _synthesize_line(
    client: genai.Client,
    *,
    line_id: str,
    prompt: str,
    key: str,
    model: str,
    voice: str,
    style: str,
    text_chars: int,
    wav: Path,
    meta: Path,
    runs_file: Path,
    base_dir: Path,
) -> float:
    """Synthesise one line with retries. Logs every attempt. Returns the audio length in seconds."""
    delay = BACKOFF_START_S
    for attempt in range(1, MAX_ATTEMPTS + 1):
        record: dict[str, Any] = {
            "ts": utc_now(),
            "stage": "narration",
            "provider": "gemini",
            "operation": "tts",
            "model": model,
            "line_id": line_id,
            "params": {"voice": voice, "style": style, "text_chars": text_chars},
            "input_hash": key,
            "attempt": attempt,
        }
        start = time.perf_counter()
        try:
            pcm, rate, response = _call_tts(client, model, voice, prompt)
        except (genai_errors.APIError, BadAudioError) as exc:
            record.update(
                success=False,
                latency_ms=round((time.perf_counter() - start) * 1000),
                error=str(exc)[:400],
            )
            append_run_record(runs_file, record)
            retryable = isinstance(exc, BadAudioError) or getattr(exc, "code", None) in TRANSIENT_CODES
            if not retryable or attempt == MAX_ATTEMPTS:
                raise
            wait = min(delay, BACKOFF_MAX_S) + random.random()
            print(f"    attempt {attempt}/{MAX_ATTEMPTS} failed ({str(exc)[:90]}); retrying in {wait:.0f}s")
            time.sleep(wait)
            delay *= 2
            continue
        except Exception as exc:  # noqa: BLE001 - log, then let the caller decide
            record.update(
                success=False,
                latency_ms=round((time.perf_counter() - start) * 1000),
                error=str(exc)[:400],
            )
            append_run_record(runs_file, record)
            raise

        latency_ms = round((time.perf_counter() - start) * 1000)
        samples = _samples_from_pcm(pcm)
        seconds = len(samples) / rate
        _write_wav(wav, samples, rate)
        write_json_atomic(
            meta,
            {
                "key": key,
                "model": model,
                "voice": voice,
                "sample_rate": rate,
                "duration_s": round(seconds, 3),
                "created_at": utc_now(),
            },
        )
        record.update(
            success=True,
            latency_ms=latency_ms,
            audio_seconds=round(seconds, 3),
            output=rel(wav, base_dir),
        )
        usage = getattr(response, "usage_metadata", None)
        if usage is not None:
            record["tokens"] = {
                "prompt": getattr(usage, "prompt_token_count", None),
                "output": getattr(usage, "candidates_token_count", None),
            }
        append_run_record(runs_file, record)
        return seconds
    raise RuntimeError("unreachable")


# --------------------------------------------------------------------------- timeline


def compute_timeline(data: dict[str, Any], durations: dict[str, float]) -> dict[str, Any]:
    """Place every narration line on the film's timeline.

    A shot is lengthened when its narration does not fit: shot length becomes
    max(planned, line + lead + tail). The storyboard is left untouched; the
    adjusted lengths are written to timing.json for the assembly stage.
    """
    shots: list[dict[str, Any]] = data["shots"]
    groups: dict[str, list[int]] = {}
    for index, shot in enumerate(shots):
        ref = shot["narration_ref"]
        if ref is not None:
            groups.setdefault(ref, []).append(index)

    planned = [float(shot["duration_s"]) for shot in shots]
    final = list(planned)
    for ref, indexes in groups.items():
        needed = durations[ref] + LEAD_S + TAIL_PAD_S
        available = sum(final[i] for i in indexes)
        if needed > available:
            final[indexes[-1]] += needed - available

    starts: list[float] = []
    cursor = 0.0
    for length in final:
        starts.append(cursor)
        cursor += length
    total = cursor + float(data["end_card"]["duration_s"])

    warnings: list[str] = []
    lines: list[dict[str, Any]] = []
    for line in data["narration"]:
        indexes = groups.get(line["id"], [])
        if not indexes:
            continue
        start = starts[indexes[0]] + LEAD_S
        lines.append(
            {
                "id": line["id"],
                "start_s": round(start, 3),
                "end_s": round(start + durations[line["id"]], 3),
                "duration_s": round(durations[line["id"]], 3),
            }
        )
    ordered = sorted(lines, key=lambda item: item["start_s"])
    for previous, current in zip(ordered, ordered[1:]):
        if current["start_s"] < previous["end_s"] - 0.01:
            warnings.append(
                f"{current['id']} starts before {previous['id']} has finished; "
                "check the narration_ref of the shots between them"
            )

    return {
        "total_s": round(total, 3),
        "end_card_s": float(data["end_card"]["duration_s"]),
        "lines": lines,
        "shots": [
            {
                "id": shot["id"],
                "planned_s": round(planned[i], 3),
                "duration_s": round(final[i], 3),
                "start_s": round(starts[i], 3),
                "extended_by_s": round(final[i] - planned[i], 3),
            }
            for i, shot in enumerate(shots)
        ],
        "warnings": warnings,
    }


def build_voice_track(output: Path, timeline: dict[str, Any], wavs: dict[str, Path]) -> int:
    """Place every line at its start time over silence. Returns the sample rate."""
    clips: dict[str, array] = {}
    rates: set[int] = set()
    for item in timeline["lines"]:
        samples, rate = _read_wav(wavs[item["id"]])
        clips[item["id"]] = samples
        rates.add(rate)
    if len(rates) != 1:
        raise RuntimeError(f"narration files have different sample rates: {sorted(rates)}")
    rate = rates.pop()

    total = int(round(timeline["total_s"] * rate))
    track = array("h", bytes(2 * total))
    for item in timeline["lines"]:
        samples = clips[item["id"]]
        offset = int(round(item["start_s"] * rate))
        end = min(offset + len(samples), total)
        if end <= offset:
            continue
        segment = samples[: end - offset]
        if any(track[offset:end]):  # overlapping lines: add, with clipping
            for i, value in enumerate(segment):
                track[offset + i] = max(-32768, min(32767, track[offset + i] + value))
        else:
            track[offset:end] = segment
    _write_wav(output, track, rate)
    return rate


# --------------------------------------------------------------------------- subtitles


def _srt_time(seconds: float) -> str:
    millis = int(round(seconds * 1000))
    hours, rest = divmod(millis, 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    secs, millis = divmod(rest, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def _subtitle_cues(text: str, start: float, end: float, width: int) -> list[tuple[float, float, str]]:
    """Wrap to two lines per cue and share the line's time between cues by length."""
    wrapped = textwrap.wrap(" ".join(text.split()), width=width)
    if not wrapped:
        return []
    chunks = [wrapped[i : i + 2] for i in range(0, len(wrapped), 2)]
    weights = [sum(len(part) for part in chunk) for chunk in chunks]
    cues: list[tuple[float, float, str]] = []
    cursor = start
    for chunk, weight in zip(chunks, weights):
        length = (end - start) * weight / sum(weights)
        cues.append((cursor, cursor + length, "\n".join(chunk)))
        cursor += length
    return cues


def write_srt(output: Path, timeline: dict[str, Any], text_en: dict[str, str], width: int) -> int:
    blocks: list[str] = []
    for item in timeline["lines"]:
        for start, end, text in _subtitle_cues(text_en[item["id"]], item["start_s"], item["end_s"], width):
            blocks.append(f"{len(blocks) + 1}\n{_srt_time(start)} --> {_srt_time(end)}\n{text}\n")
    write_text_atomic(output, "\n".join(blocks))
    return len(blocks)


# --------------------------------------------------------------------------- stage entry point


def run_narration(
    project_dir: Path,
    *,
    force: bool = False,
    only_line: str | None = None,
    assemble: bool = True,
    env_file: Path = ENV_FILE,
    base_dir: Path = ROOT_DIR,
    model: str | None = None,
) -> int:
    """Run the narration stage for one project. Returns a process exit code."""
    load_dotenv(env_file)

    project = project_dir.name
    sb_file = storyboard_path(project_dir)
    if not sb_file.is_file():
        print(
            f"Error: {rel(sb_file, base_dir)} not found. Create it with: storyboard project={project}",
            file=sys.stderr,
        )
        return 1
    report = validate_file(sb_file, project_dir=project_dir)
    if not report.ok:
        print(f"Error: {rel(sb_file, base_dir)} is not valid:", file=sys.stderr)
        print(report.format(), file=sys.stderr)
        return 1
    data: dict[str, Any] = json.loads(sb_file.read_text(encoding="utf-8"))

    lines: list[dict[str, Any]] = data["narration"]
    line_ids = [line["id"] for line in lines]
    if only_line is not None and only_line not in line_ids:
        print(f"Error: no narration line {only_line!r}. Lines: {', '.join(line_ids)}", file=sys.stderr)
        return 1

    style = data["meta"].get("voice_style", "")
    model = (
        model
        or os.getenv("NARRATION_MODEL", "").strip()
        or os.getenv("GEMINI_TTS_MODEL", "").strip()
        or DEFAULT_TTS_MODEL
    )
    voice = os.getenv("NARRATION_VOICE", "").strip() or DEFAULT_VOICE
    out_dir = narration_dir(project_dir)
    runs_file = project_dir / "runs.jsonl"

    # Decide what each line needs.
    plan: list[dict[str, Any]] = []
    for line in lines:
        prompt = _build_prompt(line["text"], style)
        key = _cache_key(model, voice, prompt)
        wav = out_dir / f"{line['id']}.wav"
        meta = out_dir / f"{line['id']}.wav.meta.json"
        fresh = _is_fresh(wav, meta, key)
        selected = only_line in (None, line["id"])
        if selected and (force or not fresh):
            status = "generate"
        elif fresh:
            status = "cached"
        elif wav.is_file():
            status = "stale"  # exists, but text or voice changed and line= excluded it
        else:
            status = "missing"
        plan.append({"line": line, "prompt": prompt, "key": key, "wav": wav, "meta": meta, "status": status})

    pending = [item for item in plan if item["status"] == "generate"]
    client: genai.Client | None = None
    if pending:
        api_key = os.getenv("GEMINI_API_KEY", "").strip()
        if not api_key:
            print("Error: GEMINI_API_KEY is missing from .env", file=sys.stderr)
            return 1
        
        # Add http_options to target the v1beta endpoint required for Gemini TTS models
        client = genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(api_version="v1beta"),
        )

    print(f"Narration for '{project}': {len(lines)} line(s), model {model}, voice {voice}")
    if style:
        print(f"Style: {style}")
    print()

    failures: dict[str, str] = {}
    for number, item in enumerate(plan, start=1):
        line_id = item["line"]["id"]
        label = f"[{number}/{len(plan)}] {line_id}"
        if item["status"] != "generate":
            print(f"{label}: {item['status']}")
            continue
        assert client is not None
        print(f"{label}: generating")
        start = time.perf_counter()
        try:
            seconds = _synthesize_line(
                client,
                line_id=line_id,
                prompt=item["prompt"],
                key=item["key"],
                model=model,
                voice=voice,
                style=style,
                text_chars=len(item["line"]["text"]),
                wav=item["wav"],
                meta=item["meta"],
                runs_file=runs_file,
                base_dir=base_dir,
            )
        except Exception as exc:  # noqa: BLE001 - one failed line must not stop the others
            failures[line_id] = str(exc)
            print(f"    [FAIL] {exc}", file=sys.stderr)
            continue
        print(f"    [PASS] {seconds:.1f}s of audio in {time.perf_counter() - start:.1f}s")

    missing = [item["line"]["id"] for item in plan if item["status"] == "missing"]
    stale = [item["line"]["id"] for item in plan if item["status"] == "stale"]
    if failures or missing:
        print(file=sys.stderr)
        if failures:
            print(
                f"Error: {len(failures)} line(s) failed: {', '.join(failures)}. "
                f"Check NARRATION_MODEL (currently {model}) and your quota, then re-run; "
                "finished lines are cached.",
                file=sys.stderr,
            )
        if missing:
            print(
                f"Error: no audio yet for {', '.join(missing)}; "
                "run without line= to generate every line.",
                file=sys.stderr,
            )
        print("The timeline and radio cut need every line, so nothing more was written.", file=sys.stderr)
        return 1
    if stale:
        print(f"\nWarning: audio for {', '.join(stale)} is out of date (text or voice changed) and was kept.")

    # Timeline, voice track, subtitles.
    wavs = {item["line"]["id"]: item["wav"] for item in plan}
    durations = {line_id: _wav_seconds(path) for line_id, path in wavs.items()}
    timeline = compute_timeline(data, durations)
    for item in timeline["lines"]:
        item["audio"] = rel(wavs[item["id"]], project_dir)

    voice_path = out_dir / "voice_track.wav"
    rate = build_voice_track(voice_path, timeline, wavs)
    text_en = {line["id"]: line["text_en"] for line in lines}
    cue_counts = {
        name: write_srt(out_dir / f"narration_{name}.srt", timeline, text_en, width)
        for name, width in SUBTITLE_WIDTHS.items()
    }

    timing = {
        "stage": "narration",
        "generated_at": utc_now(),
        "model": model,
        "voice": voice,
        "sample_rate": rate,
        **timeline,
    }
    write_json_atomic(out_dir / "timing.json", timing)

    extended = [shot for shot in timeline["shots"] if shot["extended_by_s"] > 0]
    print(f"\nWrote {rel(out_dir / 'timing.json', base_dir)} ({len(lines)} lines, {timeline['total_s']:g}s total)")
    print(f"Wrote {rel(voice_path, base_dir)} and {len(cue_counts)} subtitle files in {rel(out_dir, base_dir)}/")
    if extended:
        extra = sum(shot["extended_by_s"] for shot in extended)
        names = ", ".join(f"{shot['id']} (+{shot['extended_by_s']:.1f}s)" for shot in extended[:6])
        more = f" and {len(extended) - 6} more" if len(extended) > 6 else ""
        print(f"{len(extended)} shot(s) lengthened to fit their narration, +{extra:.1f}s in all: {names}{more}")
    if timeline["total_s"] > 90:
        print(f"Warning: the film now runs {timeline['total_s']:g}s, over the 90 second limit; shorten some narration.")
    for message in timeline["warnings"]:
        print(f"Warning: {message}")
    print(f"Log: {rel(runs_file, base_dir)}\n")

    if not assemble:
        return 0
    return radio_cut.run_radio_cut(project_dir, base_dir=base_dir)


def main(argv: list[str] | None = None) -> int:
    """Standalone usage: python narration.py <project_name> [--force] [--line line_03] [--no-assemble]"""
    parser = argparse.ArgumentParser(description="Generate narration and the radio cut.")
    parser.add_argument("project")
    parser.add_argument("--force", action="store_true", help="regenerate even if cached")
    parser.add_argument("--line", help="only (re)generate this narration line id")
    parser.add_argument("--no-assemble", action="store_true", help="skip the radio cut")
    args = parser.parse_args(argv)
    project_dir = ROOT_DIR / "projects" / args.project
    if not project_dir.is_dir():
        print(f"Error: project '{args.project}' does not exist.", file=sys.stderr)
        return 1
    return run_narration(
        project_dir, force=args.force, only_line=args.line, assemble=not args.no_assemble
    )


if __name__ == "__main__":
    sys.exit(main())