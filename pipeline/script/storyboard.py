"""Storyboard stage: turn projects/<name>/input/brief.md into production/storyboard.json.

Flow:
    read brief and reference images -> ask Gemini for JSON -> fill pipeline-owned
    fields -> validate against schema/storybook.schema.json -> write storyboard.json

Nothing is written to production/storyboard.json unless validation passes. If the model's
output is invalid, the errors are sent back to the model for a repair attempt
(up to ``max_attempts`` total). Every raw attempt is kept under
``outputs/storyboard/`` and every model call is logged to ``runs.jsonl``.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import random
import re
import shutil
import sys
import time
from typing import Any

from dotenv import load_dotenv
from google import genai
from google.genai import errors as genai_errors
from google.genai import types

from reference_images import ReferenceFile, scan_references
from validate_storyboard import ROOT_DIR, ValidationReport, load_schema, storyboard_path, validate_storyboard

ENV_FILE = ROOT_DIR / ".env"
DEFAULT_MODEL = "gemini-3.8-flash"
SCHEMA_VERSION = "1.0"
MAX_ATTEMPTS = 3  # model answers (first try + repair rounds) before giving up
MAX_TRANSIENT_RETRIES = 3  # per model call, for rate limits and server errors
TRANSIENT_CODES = {429, 500, 502, 503, 504}
DEFAULT_CANDIDATES = 3
DEFAULT_CROP_X = 0.5
KEY_ORDER = ("schema_version", "meta", "style", "references", "narration", "shots", "music", "end_card")

SYSTEM_PROMPT = """You are the storyboard writer for vid-forge, a pipeline that makes a 60-90 second AI short film.
Turn the user's brief into ONE JSON object that conforms exactly to the JSON Schema below.

Output rules
- Return only the JSON object. No markdown fences, no commentary.
- Do not add properties the schema does not define.
- Omit these fields; the pipeline fills them in: schema_version, meta.generation, keyframe.seed, keyframe.candidates, keyframe.selected, motion.seed, crop_x, references[].kind, references[].path, references[].sha256.

Film rules
- Total of all shot duration_s plus end_card.duration_s must be between 60 and 90 seconds. Aim for 12 to 16 shots of 3 to 6 seconds.
- One idea and one simple motion per shot. Every shot needs a purpose that serves meta.intent.
- Ids: shots shot_01, shot_02, ...; narration line_01, line_02, ...; music cues cue_01, cue_02, ...
- Narration: write text_en first (about 2.5 words per second of shot time, at most 25 words per line), then text in meta.narration_language (identical to text_en when that language is English). Each narration line must be used by at least one shot via narration_ref, in order. Use null for silent shots.
- keyframe.prompt describes the scene only. Do NOT repeat style.prefix or character descriptions: the keyframes stage prepends them. List the character keys that appear in each shot in shot.characters (empty array if none).
- Compose keyframes with the subject in the central third of the frame so a 9:16 crop works, and set framing accordingly.
- motion.type is "i2v" for generated movement (give a short motion prompt describing movement only: one subject action plus one slow camera move) or "kenburns" for a still with a slow camera (give camera). Prefer i2v for about two thirds of the shots.
- Avoid what image and video models do badly: legible text in the image, close-up hands, crowds, fast action, close-up faces, real people, brands, copyrighted characters.
- Music: instrumental, cues cover the whole film in order with no overlap, and each cue has a prompt of genre, instruments, mood and tempo written in English.
- end_card.text must say the film was made with AI.
- Use "am"-style BCP-47 codes for narration_language. subtitle_language is always "en".

Reference images
- The user may attach reference images, each labelled with an id such as character_girl, scene_village_well or style_papercut. Study them.
- Return a top-level "references" array with one object {"id", "description"} per attached image: say what it shows, in your own words, in 10 to 300 characters.
- character_<name>: the identity of a recurring character. Define the key <name> in style.characters with a description that matches the image (age, hair, clothing, colours). Where the brief and the image disagree, follow the image.
- scene_<name>: a place or an object. List its id in shot.references for each shot set there.
- style_<name>: the overall look. Use it to write style.prefix and style.palette. Never list style ids in shot.references.
- Do not list character ids in shot.references; the pipeline adds them from shot.characters. At most 4 ids per shot.
- With no attached images, return "references": [].

JSON Schema:
<<SCHEMA>>"""


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _rel(path: Path, base_dir: Path) -> str:
    try:
        return path.resolve().relative_to(base_dir.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _append_run_record(runs_file: Path, record: dict[str, Any]) -> None:
    runs_file.parent.mkdir(parents=True, exist_ok=True)
    with runs_file.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, separators=(",", ":"), ensure_ascii=False) + "\n")


def _write_json_atomic(path: Path, data: Any) -> None:
    """Write via a temp file so a crash never leaves a half-written storyboard."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(temp, path)


def _unfinished_lines(brief: str) -> list[int]:
    """Line numbers that still contain the TODO placeholder (HTML comments ignored)."""
    without_comments = re.sub(
        r"<!--.*?-->", lambda match: "\n" * match.group(0).count("\n"), brief, flags=re.DOTALL
    )
    return [
        number
        for number, line in enumerate(without_comments.splitlines(), start=1)
        if re.search(r"\bTODO\b", line)
    ]


def _derive_seed(project: str, shot_id: str, kind: str) -> int:
    """Deterministic 32-bit seed, so regenerating the storyboard keeps seeds stable."""
    digest = hashlib.sha256(f"{project}:{shot_id}:{kind}".encode("utf-8")).hexdigest()
    return int(digest[:8], 16)


def _finalise(
    data: Any,
    *,
    project: str,
    model: str,
    brief_sha256: str,
    refs: list[ReferenceFile],
) -> Any:
    """Fill the fields the pipeline owns. Leaves anything malformed for the validator to report."""
    if not isinstance(data, dict):
        return data

    meta = data.get("meta")
    if isinstance(meta, dict):
        meta["generation"] = {
            "model": model,
            "generated_at": _utc_now(),
            "brief_sha256": brief_sha256,
        }

    # The registry comes from the files on disk. The model only supplies descriptions,
    # so kind, path and hash can never be invented or mistyped.
    described: dict[str, str] = {}
    model_refs = data.get("references")
    if isinstance(model_refs, list):
        for item in model_refs:
            if isinstance(item, dict) and isinstance(item.get("id"), str) and isinstance(item.get("description"), str):
                described[item["id"]] = item["description"].strip()
    data["references"] = [
        {
            "id": ref.id,
            "kind": ref.kind,
            "path": ref.rel_path,
            "description": described.get(ref.id, ""),
            "sha256": ref.sha256,
        }
        for ref in refs
    ]
    character_refs = {ref.name: ref.id for ref in refs if ref.kind == "character"}

    shots = data.get("shots")
    if isinstance(shots, list):
        for shot in shots:
            if not isinstance(shot, dict):
                continue
            shot_id = str(shot.get("id", ""))
            shot.setdefault("crop_x", DEFAULT_CROP_X)

            shot_refs = shot.get("references", [])
            characters = shot.get("characters", [])
            if isinstance(shot_refs, list) and isinstance(characters, list):
                for name in characters:
                    ref_id = character_refs.get(name) if isinstance(name, str) else None
                    if ref_id and ref_id not in shot_refs:
                        shot_refs.append(ref_id)
                shot["references"] = shot_refs

            keyframe = shot.get("keyframe")
            if isinstance(keyframe, dict):
                keyframe.setdefault("seed", _derive_seed(project, shot_id, "keyframe"))
                keyframe.setdefault("candidates", DEFAULT_CANDIDATES)
                keyframe.setdefault("selected", None)
            motion = shot.get("motion")
            if isinstance(motion, dict) and motion.get("type") == "i2v":
                motion.setdefault("seed", _derive_seed(project, shot_id, "motion"))

    data["schema_version"] = SCHEMA_VERSION
    ordered = {key: data[key] for key in KEY_ORDER if key in data}
    ordered.update({key: value for key, value in data.items() if key not in ordered})
    return ordered


def _extract_json(text: str) -> Any:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned)
    return json.loads(cleaned)


def _generate_with_retry(
    client: genai.Client,
    model: str,
    contents: list[types.Content],
    config: types.GenerateContentConfig,
) -> Any:
    """Call the model, retrying only transient errors (rate limit, server) with backoff."""
    delay = 2.0
    for attempt in range(1, MAX_TRANSIENT_RETRIES + 1):
        try:
            return client.models.generate_content(model=model, contents=contents, config=config)
        except genai_errors.APIError as exc:
            if exc.code in TRANSIENT_CODES and attempt < MAX_TRANSIENT_RETRIES:
                wait = delay + random.random()
                print(f"  transient error {exc.code}; retrying in {wait:.1f}s")
                time.sleep(wait)
                delay *= 2
                continue
            raise
    raise RuntimeError("unreachable")


def _text_content(role: str, text: str) -> types.Content:
    return types.Content(role=role, parts=[types.Part.from_text(text=text)])


def _first_message(brief: str, refs: list[ReferenceFile]) -> types.Content:
    """The brief, followed by each reference image preceded by a label giving its id."""
    parts = [types.Part.from_text(text=f"Brief:\n\n{brief}")]
    if refs:
        parts.append(types.Part.from_text(text="Reference images follow. Each one is labelled with its id."))
        for ref in refs:
            parts.append(types.Part.from_text(text=f"Reference image id: {ref.id} (kind: {ref.kind})"))
            parts.append(types.Part.from_bytes(data=ref.read_bytes(), mime_type=ref.mime_type))
    return types.Content(role="user", parts=parts)


def _repair_message(report: ValidationReport) -> str:
    shown = report.errors[:25]
    extra = len(report.errors) - len(shown)
    lines = "\n".join(f"- {message}" for message in shown)
    if extra > 0:
        lines += f"\n- ...and {extra} more"
    return (
        "The JSON you returned failed validation. Fix every problem below and return the "
        "complete corrected JSON object (not a diff), following all earlier rules.\n\n"
        f"Problems:\n{lines}"
    )


def _summary(data: dict[str, Any]) -> str:
    total = sum(shot["duration_s"] for shot in data["shots"]) + data["end_card"]["duration_s"]
    text = (
        f"{len(data['shots'])} shots, {len(data['narration'])} narration lines, "
        f"{len(data['music']['cues'])} music cues, {total:g}s total"
    )
    if data.get("references"):
        text += f", {len(data['references'])} reference image(s)"
    return text


def run_storyboard(
    project_dir: Path,
    *,
    force: bool = False,
    env_file: Path = ENV_FILE,
    base_dir: Path = ROOT_DIR,
    model: str | None = None,
    max_attempts: int = MAX_ATTEMPTS,
) -> int:
    """Generate and validate projects/<name>/production/storyboard.json. Returns a process exit code."""
    load_dotenv(env_file)

    project = project_dir.name
    brief_path = project_dir / "input" / "brief.md"
    target = storyboard_path(project_dir)
    raw_dir = project_dir / "outputs" / "storyboard"
    runs_file = project_dir / "runs.jsonl"

    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        print("Error: GEMINI_API_KEY is missing from .env", file=sys.stderr)
        return 1

    if not brief_path.is_file():
        print(f"Error: brief not found at {_rel(brief_path, base_dir)}", file=sys.stderr)
        return 1
    brief = brief_path.read_text(encoding="utf-8")
    if not brief.strip():
        print(f"Error: {_rel(brief_path, base_dir)} is empty.", file=sys.stderr)
        return 1
    unfinished = _unfinished_lines(brief)
    if unfinished:
        numbers = ", ".join(str(number) for number in unfinished)
        print(
            f"Error: {_rel(brief_path, base_dir)} still has TODO placeholders (line {numbers}). "
            "Fill them in first.",
            file=sys.stderr,
        )
        return 1

    if target.exists() and not force:
        print(
            f"Error: {_rel(target, base_dir)} already exists. "
            "Pass --force to regenerate it (the current file is saved as storyboard.json.bak).",
            file=sys.stderr,
        )
        return 1

    refs, ref_errors = scan_references(project_dir)
    if ref_errors:
        print("Error: problem(s) in input/references/:", file=sys.stderr)
        for message in ref_errors:
            print(f"  - {message}", file=sys.stderr)
        return 1

    schema = load_schema()
    model = model or os.getenv("STORYBOARD_MODEL", "").strip() or os.getenv("GEMINI_MODEL", "").strip() or DEFAULT_MODEL
    brief_sha256 = hashlib.sha256(brief.encode("utf-8")).hexdigest()

    client = genai.Client(api_key=api_key)
    config = types.GenerateContentConfig(
        system_instruction=SYSTEM_PROMPT.replace("<<SCHEMA>>", json.dumps(schema, indent=2)),
        response_mime_type="application/json",
    )
    contents: list[types.Content] = [_first_message(brief, refs)]

    print(f"Generating storyboard for '{project}' with {model}...")
    if refs:
        print(f"Using {len(refs)} reference image(s): {', '.join(ref.id for ref in refs)}")
    print()

    last_report = ValidationReport()
    for attempt in range(1, max_attempts + 1):
        print(f"[attempt {attempt}/{max_attempts}] calling {model}")
        record: dict[str, Any] = {
            "ts": _utc_now(),
            "stage": "storyboard",
            "provider": "gemini",
            "operation": "storyboard_generation",
            "model": model,
            "params": {"response_mime_type": "application/json"},
            "attempt": attempt,
            "brief_sha256": brief_sha256,
            "references": [ref.id for ref in refs],
        }
        start = time.perf_counter()
        try:
            response = _generate_with_retry(client, model, contents, config)
            raw = response.text or ""
        except Exception as exc:  # noqa: BLE001 - log any provider failure
            record.update(
                success=False,
                latency_ms=round((time.perf_counter() - start) * 1000),
                error=str(exc)[:500],
            )
            _append_run_record(runs_file, record)
            print(f"[FAIL] model call failed: {exc}", file=sys.stderr)
            return 1
        latency_ms = round((time.perf_counter() - start) * 1000)

        usage = getattr(response, "usage_metadata", None)
        if usage is not None:
            record["tokens"] = {
                "prompt": getattr(usage, "prompt_token_count", None),
                "output": getattr(usage, "candidates_token_count", None),
            }

        raw_path = raw_dir / f"attempt_{attempt}.json"
        raw_dir.mkdir(parents=True, exist_ok=True)
        raw_path.write_text(raw, encoding="utf-8")

        data: Any = None
        try:
            data = _extract_json(raw)
        except json.JSONDecodeError as exc:
            report = ValidationReport(errors=[f"response is not valid JSON: {exc}"])
        else:
            data = _finalise(data, project=project, model=model, brief_sha256=brief_sha256, refs=refs)
            report = validate_storyboard(data, schema, project_dir=project_dir)

        record.update(
            success=report.ok,
            latency_ms=latency_ms,
            validation_errors=len(report.errors),
            raw_output=_rel(raw_path, base_dir),
        )
        if report.ok:
            record["output"] = _rel(target, base_dir)
        else:
            record["error"] = "; ".join(report.errors[:3])[:500]
        _append_run_record(runs_file, record)

        if report.ok:
            if force and target.exists():
                shutil.copy2(target, target.with_name(target.name + ".bak"))
            _write_json_atomic(target, data)
            print(f"[PASS] valid on attempt {attempt}, {latency_ms}ms")
            print(f"\nWrote {_rel(target, base_dir)}")
            print(f"  {_summary(data)}")
            if report.warnings:
                print(f"\n{len(report.warnings)} warning(s):")
                for message in report.warnings:
                    print(f"  - {message}")
            print(f"\nLog: {_rel(runs_file, base_dir)}")
            return 0

        last_report = report
        print(f"[FAIL] {len(report.errors)} validation error(s), {latency_ms}ms")
        for message in report.errors[:10]:
            print(f"  - {message}")
        if len(report.errors) > 10:
            print(f"  ...and {len(report.errors) - 10} more")
        if attempt < max_attempts:
            print("  sending the errors back to the model for repair\n")
            contents.append(_text_content("model", raw))
            contents.append(_text_content("user", _repair_message(report)))

    print(
        f"\nError: no valid storyboard after {max_attempts} attempts; nothing was written.\n"
        f"Raw attempts are in {_rel(raw_dir, base_dir)}/ and the log is {_rel(runs_file, base_dir)}.",
        file=sys.stderr,
    )
    return 1


def main(argv: list[str] | None = None) -> int:
    """Standalone usage: python storyboard.py <project_name> [--force]"""
    args = list(sys.argv[1:] if argv is None else argv)
    force = "--force" in args
    names = [arg for arg in args if not arg.startswith("--")]
    if len(names) != 1:
        print("Usage: python storyboard.py <project_name> [--force]", file=sys.stderr)
        return 2
    project_dir = ROOT_DIR / "projects" / names[0]
    if not project_dir.is_dir():
        print(f"Error: project '{names[0]}' does not exist.", file=sys.stderr)
        return 1
    return run_storyboard(project_dir, force=force)


if __name__ == "__main__":
    sys.exit(main())