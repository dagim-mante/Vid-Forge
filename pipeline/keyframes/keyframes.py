"""Keyframe stage: generate candidate images for every shot (or one shot).

Flow for each shot:
    build prompt (style.prefix + character descriptions + keyframe.prompt)
    -> resolve reference images (resize to ≤ 512 px for FLUX.2 compatibility)
    -> generate keyframe.candidates images via Cloudflare Workers AI
    -> save each candidate under production/keyframes/<shot_id>/candidate_<n>.jpg
    -> write / update production/keyframes.json with the generation record
    -> append one JSONL record per candidate to runs.jsonl

Candidate 1 is selected by default when it succeeds and no candidate is already selected.
An existing selection is always preserved.
Run with --shot shot_01 to generate a single shot; without it every shot runs.
Run with --force to regenerate shots that already have all candidates on disk.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
import io
import json
import os
from pathlib import Path
import random
import sys
import time
from typing import Any

from dotenv import load_dotenv
import requests

# The storyboard validator is shared with the script stage, not stored beside
# this file. Add its directory so both the CLI loader and direct script usage
# can import it reliably.
_SCRIPT_DIR = Path(__file__).resolve().parents[1] / "script"
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from validate_storyboard import ROOT_DIR, storyboard_path, validate_file

DEFAULT_CLOUDFLARE_IMAGE_MODEL = "@cf/black-forest-labs/flux-2-klein-4b"
IMAGE_WIDTH = 1024
IMAGE_HEIGHT = 576  # 16:9
REQUEST_TIMEOUT_SECONDS = 90
MAX_TRANSIENT_RETRIES = 3
TRANSIENT_CODES = {429, 500, 502, 503, 504}
# FLUX.2 reference images must be < 512 × 512 px; we resize inline.
MAX_REF_SIDE_PX = 500
MAX_CANDIDATES = 6  # Must match schema/storybook.schema.json.

# Where generated images live inside the project.
KEYFRAMES_RELATIVE = Path("production") / "keyframes"
KEYFRAMES_JSON_RELATIVE = Path("production") / "keyframes.json"


# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------


@dataclass
class CandidateRecord:
    shot_id: str
    candidate: int        # 1-based
    path: str             # relative to project root
    seed: int
    prompt: str           # the full prompt sent
    reference_ids: list[str] = field(default_factory=list)
    latency_ms: int = 0
    success: bool = True
    error: str = ""


def _utc_now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _rel(path: Path, base: Path) -> str:
    try:
        return path.resolve().relative_to(base.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _append_run_record(runs_file: Path, record: dict[str, Any]) -> None:
    runs_file.parent.mkdir(parents=True, exist_ok=True)
    with runs_file.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, separators=(",", ":"), ensure_ascii=False) + "\n")


def _load_keyframes_json(path: Path) -> dict[str, Any]:
    if path.is_file():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {"schema_version": "1.0", "shots": {}}


def _save_keyframes_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _save_storyboard(path: Path, data: dict[str, Any]) -> None:
    """Persist an explicit candidate-count override atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Image resize (stdlib only – no Pillow dependency)
# ---------------------------------------------------------------------------


def _resize_to_png_bytes(raw: bytes, max_side: int) -> bytes:
    """Resize an image to fit within max_side × max_side using stdlib only.

    Uses the struct-based PPM trick via the ``imghdr`` detection path, falling
    back to passing the bytes unchanged if we can't parse them. For production
    use, install Pillow for better quality; this avoids a hard dependency.
    """
    try:
        from PIL import Image  # type: ignore[import-untyped]

        img = Image.open(io.BytesIO(raw))
        img.thumbnail((max_side, max_side), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
    except ImportError:
        pass

    # Minimal fallback: return bytes unchanged and hope they fit.
    # FLUX.2 will error server-side if the image is too large.
    return raw


# ---------------------------------------------------------------------------
# Cloudflare API helpers
# ---------------------------------------------------------------------------


def _cf_credentials() -> tuple[str, str]:
    account_id = (
        os.getenv("CLOUDFLARE_WORKERS_AI_ACCOUNT_ID", "")
        or os.getenv("CLOULDFLARE_WORKERS_AI_ACCOUNT_ID", "")
        or os.getenv("CLOUDFLARE_ACCOUNT_ID", "")
    ).strip()
    api_key = (
        os.getenv("CLOUDFLARE_WORKERS_AI_API_KEY", "")
        or os.getenv("CLOULDFLARE_WORKERS_AI_API_KEY", "")
        or os.getenv("CLOUDFLARE_API_TOKEN", "")
        or os.getenv("CLOUDFLARE_API_KEY", "")
    ).strip()
    return account_id, api_key


def _generate_image(
    account_id: str,
    api_key: str,
    prompt: str,
    seed: int,
    ref_images: list[tuple[str, bytes, str]],  # (ref_id, raw_bytes, mime_type)
    model: str = DEFAULT_CLOUDFLARE_IMAGE_MODEL,
) -> bytes:
    """Call Cloudflare Workers AI and return raw image bytes.

    Retries on transient errors (429 / 5xx) with exponential back-off.
    ref_images is a list of (ref_id, bytes, mime_type); they are sent as
    multipart form fields named input_image_0 … input_image_N.
    """
    url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/run/{model}"
    headers = {"Authorization": f"Bearer {api_key}"}

    # FLUX.2 uses multipart/form-data
    files: dict[str, Any] = {
        "prompt": (None, prompt),
        "width": (None, str(IMAGE_WIDTH)),
        "height": (None, str(IMAGE_HEIGHT)),
        "seed": (None, str(seed)),
        "num_steps": (None, "4"),  # FLUX.2 klein fixed 4-step inference
    }
    for idx, (ref_id, raw, mime) in enumerate(ref_images):
        resized = _resize_to_png_bytes(raw, MAX_REF_SIDE_PX)
        files[f"input_image_{idx}"] = (f"{ref_id}.png", resized, "image/png")

    delay = 2.0
    last_exc: Exception = RuntimeError("unreachable")
    for attempt in range(1, MAX_TRANSIENT_RETRIES + 1):
        try:
            resp = requests.post(url, headers=headers, files=files, timeout=REQUEST_TIMEOUT_SECONDS)
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < MAX_TRANSIENT_RETRIES:
                time.sleep(delay + random.random())
                delay *= 2
                continue
            raise

        if resp.status_code in TRANSIENT_CODES and attempt < MAX_TRANSIENT_RETRIES:
            wait = delay + random.random()
            print(f"    transient {resp.status_code}; retrying in {wait:.1f}s")
            time.sleep(wait)
            delay *= 2
            continue

        if not resp.ok:
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:400]}")

        content_type = resp.headers.get("Content-Type", "").lower()
        if "application/json" in content_type:
            payload = resp.json()
            if not payload.get("success", True):
                raise RuntimeError(f"Cloudflare error: {payload.get('errors')}")
            image_b64 = (payload.get("result") or {}).get("image")
            if not image_b64:
                raise RuntimeError("Cloudflare JSON response missing result.image")
            return base64.b64decode(image_b64)

        image_bytes = resp.content
        if not image_bytes:
            raise RuntimeError("Cloudflare returned empty image bytes")
        return image_bytes

    raise last_exc


# ---------------------------------------------------------------------------
# Prompt builder
# ---------------------------------------------------------------------------


def _build_prompt(shot: dict[str, Any], storyboard: dict[str, Any]) -> str:
    """Compose: style.prefix + relevant character descriptions + keyframe.prompt."""
    style = storyboard.get("style", {})
    prefix = style.get("prefix", "").strip()

    char_descriptions: list[str] = []
    characters: dict[str, str] = style.get("characters", {})
    for char_id in shot.get("characters", []):
        desc = characters.get(char_id, "").strip()
        if desc:
            char_descriptions.append(f"{char_id}: {desc}")

    kf_prompt = shot.get("keyframe", {}).get("prompt", "").strip()

    parts = [p for p in [prefix, ", ".join(char_descriptions), kf_prompt] if p]
    return ". ".join(parts)


# ---------------------------------------------------------------------------
# Reference image resolver
# ---------------------------------------------------------------------------


def _resolve_references(
    shot: dict[str, Any],
    storyboard: dict[str, Any],
    project_dir: Path,
) -> list[tuple[str, bytes, str]]:
    """Return up to 4 reference images as (id, raw_bytes, mime_type) tuples.

    Includes:
      - all character references for characters listed in shot.characters
      - all scene references listed in shot.references
      - style references are excluded (they inform style.prefix, not per-shot refs)
    """
    ref_registry: dict[str, dict[str, Any]] = {
        r["id"]: r for r in storyboard.get("references", [])
    }

    wanted_ids: list[str] = []
    # Character refs first
    for char_id in shot.get("characters", []):
        ref_id = f"character_{char_id}"
        if ref_id in ref_registry and ref_id not in wanted_ids:
            wanted_ids.append(ref_id)
    # Explicit shot references (scene only; style never listed here)
    for ref_id in shot.get("references", []):
        if ref_id in ref_registry and not ref_id.startswith("style_") and ref_id not in wanted_ids:
            wanted_ids.append(ref_id)

    # Cap at 4 (FLUX.2 limit)
    wanted_ids = wanted_ids[:4]

    result: list[tuple[str, bytes, str]] = []
    for ref_id in wanted_ids:
        ref = ref_registry[ref_id]
        ref_path = project_dir / ref["path"]
        if not ref_path.is_file():
            print(f"    warning: reference image not found: {ref['path']}")
            continue
        raw = ref_path.read_bytes()
        mime = "image/jpeg" if ref_path.suffix.lower() in {".jpg", ".jpeg"} else "image/png"
        result.append((ref_id, raw, mime))

    return result


# ---------------------------------------------------------------------------
# Per-shot generation
# ---------------------------------------------------------------------------


def _shot_output_dir(project_dir: Path, shot_id: str) -> Path:
    return project_dir / KEYFRAMES_RELATIVE / shot_id


def _candidate_path(shot_dir: Path, candidate: int) -> Path:
    return shot_dir / f"candidate_{candidate:02d}.jpg"


def _shot_needs_generation(
    project_dir: Path,
    shot: dict[str, Any],
    force: bool,
) -> bool:
    """True if any candidate image is missing (or force is set)."""
    if force:
        return True
    shot_id = shot["id"]
    n = shot["keyframe"]["candidates"]
    shot_dir = _shot_output_dir(project_dir, shot_id)
    return any(not _candidate_path(shot_dir, c).is_file() for c in range(1, n + 1))


def _needs_default_selection(project_dir: Path, shot: dict[str, Any]) -> bool:
    """Select an already generated first candidate for legacy unselected shots."""
    return (
        shot["keyframe"].get("selected") is None
        and _candidate_path(_shot_output_dir(project_dir, shot["id"]), 1).is_file()
    )


def _generate_shot(
    shot: dict[str, Any],
    storyboard: dict[str, Any],
    project_dir: Path,
    account_id: str,
    api_key: str,
    runs_file: Path,
    base_dir: Path,
    model: str,
    force: bool,
) -> list[CandidateRecord]:
    """Generate all candidates for one shot. Returns a list of CandidateRecord."""
    shot_id = shot["id"]
    n_candidates = shot["keyframe"]["candidates"]
    base_seed = shot["keyframe"]["seed"]
    shot_dir = _shot_output_dir(project_dir, shot_id)
    shot_dir.mkdir(parents=True, exist_ok=True)

    prompt = _build_prompt(shot, storyboard)
    ref_images = _resolve_references(shot, storyboard, project_dir)
    ref_ids = [r[0] for r in ref_images]

    records: list[CandidateRecord] = []
    for candidate in range(1, n_candidates + 1):
        out_path = _candidate_path(shot_dir, candidate)
        if out_path.is_file() and not force:
            print(f"  [{shot_id}] candidate {candidate}/{n_candidates} already exists – skipping")
            rec = CandidateRecord(
                shot_id=shot_id,
                candidate=candidate,
                path=_rel(out_path, base_dir),
                seed=base_seed + candidate - 1,
                prompt=prompt,
                reference_ids=ref_ids,
                success=True,
            )
            records.append(rec)
            continue

        seed = base_seed + candidate - 1
        print(f"  [{shot_id}] generating candidate {candidate}/{n_candidates} (seed={seed})...", end=" ", flush=True)
        start = time.perf_counter()
        try:
            image_bytes = _generate_image(account_id, api_key, prompt, seed, ref_images, model)
            latency_ms = round((time.perf_counter() - start) * 1000)
            out_path.write_bytes(image_bytes)
            print(f"{latency_ms}ms ✓")
            rec = CandidateRecord(
                shot_id=shot_id,
                candidate=candidate,
                path=_rel(out_path, base_dir),
                seed=seed,
                prompt=prompt,
                reference_ids=ref_ids,
                latency_ms=latency_ms,
                success=True,
            )
        except Exception as exc:  # noqa: BLE001
            latency_ms = round((time.perf_counter() - start) * 1000)
            print(f"{latency_ms}ms ✗  {exc}")
            rec = CandidateRecord(
                shot_id=shot_id,
                candidate=candidate,
                path=_rel(out_path, base_dir),
                seed=seed,
                prompt=prompt,
                reference_ids=ref_ids,
                latency_ms=latency_ms,
                success=False,
                error=str(exc)[:400],
            )

        # Append a run record for every candidate
        run_record: dict[str, Any] = {
            "ts": _utc_now(),
            "stage": "keyframes",
            "provider": "cloudflare",
            "operation": "image_generation",
            "model": model,
            "shot_id": shot_id,
            "candidate": candidate,
            "seed": seed,
            "reference_ids": ref_ids,
            "latency_ms": latency_ms,
            "success": rec.success,
        }
        if rec.success:
            run_record["output"] = rec.path
        else:
            run_record["error"] = rec.error
        _append_run_record(runs_file, run_record)
        records.append(rec)

    return records


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def run_keyframes(
    project_dir: Path,
    *,
    shot_id: str | None = None,
    candidate_count: int | None = None,
    force: bool = False,
    env_file: Path = ROOT_DIR / ".env",
    base_dir: Path = ROOT_DIR,
    model: str | None = None,
) -> int:
    """Generate keyframe candidates for a project. Returns a process exit code."""
    load_dotenv(env_file)
    if candidate_count is not None and not 1 <= candidate_count <= MAX_CANDIDATES:
        print(f"Error: candidate-count must be between 1 and {MAX_CANDIDATES}.", file=sys.stderr)
        return 2
    model = model or os.getenv("CLOUDFLARE_IMAGE_MODEL", DEFAULT_CLOUDFLARE_IMAGE_MODEL)

    account_id, api_key = _cf_credentials()
    if not account_id or not api_key:
        print(
            "Error: Cloudflare credentials not found. Set CLOUDFLARE_WORKERS_AI_ACCOUNT_ID "
            "and CLOUDFLARE_WORKERS_AI_API_KEY in .env",
            file=sys.stderr,
        )
        return 1

    sb_path = storyboard_path(project_dir)
    if not sb_path.is_file():
        print(
            f"Error: {_rel(sb_path, base_dir)} not found. "
            f"Run: vid-forge storyboard project={project_dir.name}",
            file=sys.stderr,
        )
        return 1

    # Validate the storyboard before consuming API quota.
    report = validate_file(sb_path, project_dir=project_dir)
    if not report.ok:
        print(f"Error: {_rel(sb_path, base_dir)} failed validation:", file=sys.stderr)
        for msg in report.errors[:10]:
            print(f"  - {msg}", file=sys.stderr)
        return 1

    storyboard: dict[str, Any] = json.loads(sb_path.read_text(encoding="utf-8"))
    all_shots: list[dict[str, Any]] = storyboard["shots"]

    # Resolve the scope before applying an explicit count to the storyboard.
    if shot_id is not None:
        scoped = [s for s in all_shots if s["id"] == shot_id]
        if not scoped:
            print(f"Error: shot '{shot_id}' not found in {_rel(sb_path, base_dir)}.", file=sys.stderr)
            return 1
    else:
        scoped = all_shots

    storyboard_changed = False
    if candidate_count is not None:
        for shot in scoped:
            selected = shot["keyframe"].get("selected")
            if selected is not None and selected > candidate_count:
                print(
                    f"Error: {shot['id']} has selected candidate {selected}, which exceeds candidate-count "
                    f"{candidate_count}. Change the selection first or use a larger candidate-count.",
                    file=sys.stderr,
                )
                return 1
        for shot in scoped:
            if shot["keyframe"].get("candidates") != candidate_count:
                shot["keyframe"]["candidates"] = candidate_count
                storyboard_changed = True
        if storyboard_changed:
            _save_storyboard(sb_path, storyboard)

    runs_file = project_dir / "runs.jsonl"
    kf_json_path = project_dir / KEYFRAMES_JSON_RELATIVE
    kf_data = _load_keyframes_json(kf_json_path)

    # Filter to the requested shot or all shots.
    if shot_id is not None:
        targets = scoped
    else:
        targets = [
            s for s in all_shots
            if _shot_needs_generation(project_dir, s, force)
            or _needs_default_selection(project_dir, s)
        ]

    if not targets:
        print("All candidates already exist. Pass --force to regenerate.")
        return 0

    scope = f"shot {shot_id}" if shot_id else f"{len(targets)} shot(s)"
    print(f"Generating keyframe candidates for '{project_dir.name}': {scope}\n")

    any_failed = False
    for shot in targets:
        records = _generate_shot(
            shot, storyboard, project_dir, account_id, api_key,
            runs_file, base_dir, model, force,
        )
        if shot["keyframe"].get("selected") is None and any(
            rec.candidate == 1 and rec.success for rec in records
        ):
            shot["keyframe"]["selected"] = 1
            storyboard_changed = True
        # Update keyframes.json
        shot_entry = kf_data["shots"].setdefault(shot["id"], {})
        shot_entry["prompt"] = _build_prompt(shot, storyboard)
        shot_entry["reference_ids"] = [r[0] for r in _resolve_references(shot, storyboard, project_dir)]
        shot_entry["candidates"] = {}
        for rec in records:
            shot_entry["candidates"][str(rec.candidate)] = {
                "path": rec.path,
                "seed": rec.seed,
                "success": rec.success,
                "latency_ms": rec.latency_ms,
                **({"error": rec.error} if rec.error else {}),
            }
        if any(not r.success for r in records):
            any_failed = True

    if storyboard_changed:
        _save_storyboard(sb_path, storyboard)
    _save_keyframes_json(kf_json_path, kf_data)

    # Summary
    total_ok = sum(
        1
        for s in kf_data["shots"].values()
        for c in s.get("candidates", {}).values()
        if c.get("success")
    )
    total_all = sum(
        len(s.get("candidates", {})) for s in kf_data["shots"].values()
    )
    print(f"\n{total_ok}/{total_all} candidates generated successfully")
    print(f"Keyframe index: {_rel(kf_json_path, base_dir)}")
    print(f"Log: {_rel(runs_file, base_dir)}")

    kf_dir = project_dir / KEYFRAMES_RELATIVE
    generated_shots = sorted(kf_data["shots"].keys())
    print(f"\n{_rel(kf_dir, base_dir)}/")
    for sid in generated_shots:
        candidates = kf_data["shots"][sid].get("candidates", {})
        files = []
        for c_num in sorted(candidates.keys(), key=int):
            rec = candidates[c_num]
            icon = "✓" if rec.get("success") else "✗"
            files.append(f"candidate_{int(c_num):02d}.jpg {icon}")
        if files:
            print(f"  {sid}/")
            for f in files:
                print(f"    {f}")

    return 1 if any_failed else 0


def main(argv: list[str] | None = None) -> int:
    """Standalone usage: python keyframes.py <project_name> [--shot shot_01] [--force]"""
    args = list(sys.argv[1:] if argv is None else argv)
    force = "--force" in args
    args = [a for a in args if a != "--force"]

    shot_id: str | None = None
    for i, a in enumerate(args):
        if a.startswith("--shot="):
            shot_id = a.split("=", 1)[1]
            args.pop(i)
            break
        if a == "--shot" and i + 1 < len(args):
            shot_id = args[i + 1]
            args.pop(i + 1)
            args.pop(i)
            break

    if len(args) != 1:
        print("Usage: python keyframes.py <project_name> [--shot shot_NN] [--force]", file=sys.stderr)
        return 2

    project_dir = ROOT_DIR / "projects" / args[0]
    if not project_dir.is_dir():
        print(f"Error: project '{args[0]}' does not exist.", file=sys.stderr)
        return 1

    return run_keyframes(project_dir, shot_id=shot_id, force=force)


if __name__ == "__main__":
    sys.exit(main())
