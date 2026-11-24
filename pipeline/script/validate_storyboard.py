"""Validate a storybook against schema/storybook.schema.json.

Two layers:

1. JSON Schema (structure, types, ranges, id formats).
2. Semantic checks the schema cannot express (unique ids, references that
   resolve, total duration, music timing, narration pace).

Schema errors and semantic errors both block. Warnings never block.

Reusable by every pipeline stage: call ``validate_file`` at the top of a run.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
import sys
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from reference_images import sha256_file

SCHEMA_RELATIVE = Path("schema") / "storybook.schema.json"


def _find_root() -> Path:
    """Walk up from this file to the repository root (the folder that holds schema/)."""
    for parent in Path(__file__).resolve().parents:
        if (parent / SCHEMA_RELATIVE).is_file():
            return parent
    raise RuntimeError(f"Could not find {SCHEMA_RELATIVE.as_posix()} in any folder above {__file__}")


ROOT_DIR = _find_root()
SCHEMA_PATH = ROOT_DIR / SCHEMA_RELATIVE

# Where a project keeps its storyboard. Every stage should use storyboard_path().
STORYBOARD_RELATIVE = Path("production") / "storyboard.json"

MIN_TOTAL_SECONDS = 60.0
MAX_TOTAL_SECONDS = 90.0
MAX_WORDS_PER_SECOND = 3.0  # English narration faster than this is hard to follow
LONG_I2V_SECONDS = 6.0  # image-to-video clips are typically about five seconds
MAX_MESSAGE_CHARS = 300


@dataclass
class ValidationReport:
    """Result of validating one storyboard."""

    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def format(self) -> str:
        lines: list[str] = []
        if self.errors:
            lines.append(f"{len(self.errors)} error(s):")
            lines.extend(f"  - {message}" for message in self.errors)
        if self.warnings:
            lines.append(f"{len(self.warnings)} warning(s):")
            lines.extend(f"  - {message}" for message in self.warnings)
        if not lines:
            lines.append("Storyboard is valid.")
        return "\n".join(lines)


def storyboard_path(project_dir: Path) -> Path:
    """projects/<name>/production/storyboard.json"""
    return project_dir / STORYBOARD_RELATIVE


def load_schema(path: Path = SCHEMA_PATH) -> dict[str, Any]:
    """Load the schema and confirm that the schema itself is well formed."""
    schema = json.loads(path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return schema


def _format_path(path: Any) -> str:
    out = ""
    for part in path:
        if isinstance(part, int):
            out += f"[{part}]"
        else:
            out += f".{part}" if out else str(part)
    return out or "(root)"


def _schema_errors(data: Any, schema: dict[str, Any]) -> list[str]:
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    found = sorted(
        validator.iter_errors(data),
        key=lambda err: [str(part) for part in err.absolute_path],
    )
    messages: list[str] = []
    for err in found:
        message = err.message
        if len(message) > MAX_MESSAGE_CHARS:
            message = message[:MAX_MESSAGE_CHARS] + "..."
        line = f"{_format_path(err.absolute_path)}: {message}"
        if line not in messages:
            messages.append(line)
    return messages


def _duplicates(values: list[str]) -> list[str]:
    seen: set[str] = set()
    dupes: list[str] = []
    for value in values:
        if value in seen and value not in dupes:
            dupes.append(value)
        seen.add(value)
    return dupes


def _semantic_checks(data: dict[str, Any], project_dir: Path | None) -> tuple[list[str], list[str]]:
    """Checks that need the whole document. Assumes the schema already passed.

    With ``project_dir`` the reference image files are checked on disk too.
    """
    errors: list[str] = []
    warnings: list[str] = []

    shots: list[dict[str, Any]] = data["shots"]
    lines: list[dict[str, Any]] = data["narration"]
    cues: list[dict[str, Any]] = data["music"]["cues"]
    characters: dict[str, str] = data["style"]["characters"]

    # Unique ids.
    for label, items in (("shots", shots), ("narration", lines), ("music.cues", cues)):
        for dupe in _duplicates([item["id"] for item in items]):
            errors.append(f"{label}: duplicate id {dupe!r}")

    # Reference images.
    references: list[dict[str, Any]] = data.get("references", [])
    ref_by_id = {ref["id"]: ref for ref in references}
    character_refs: dict[str, str] = {}  # character key -> reference id
    for dupe in _duplicates([ref["id"] for ref in references]):
        errors.append(f"references: duplicate id {dupe!r}")
    for index, ref in enumerate(references):
        where = f"references[{index}] ({ref['id']})"
        prefix, _, name = ref["id"].partition("_")
        if ref["kind"] != prefix:
            errors.append(f"{where}.kind: {ref['kind']!r} does not match the id prefix {prefix!r}")
        if Path(ref["path"]).stem != ref["id"]:
            errors.append(f"{where}.path: the file name must be {ref['id']}.<extension>")
        if ref["kind"] == "character":
            character_refs[name] = ref["id"]
            if name not in characters:
                errors.append(f"{where}: no character {name!r} in style.characters")
        if project_dir is not None:
            image = project_dir / ref["path"]
            if not image.is_file():
                errors.append(f"{where}.path: file not found ({ref['path']})")
            elif sha256_file(image) != ref["sha256"]:
                warnings.append(
                    f"{where}: the image changed after the storyboard was written; "
                    "shots that use it will regenerate"
                )
    if sum(1 for ref in references if ref["kind"] == "style") > 1:
        warnings.append("references: more than one style reference; the keyframes stage will have to blend them")
    used_refs: set[str] = set()

    # References resolve.
    line_ids = [line["id"] for line in lines]
    line_index = {line_id: index for index, line_id in enumerate(line_ids)}
    referenced: set[str] = set()
    last_index = -1
    for index, shot in enumerate(shots):
        where = f"shots[{index}] ({shot['id']})"
        ref = shot["narration_ref"]
        if ref is not None:
            if ref not in line_index:
                errors.append(f"{where}.narration_ref: {ref!r} is not a narration line id")
            else:
                referenced.add(ref)
                if line_index[ref] < last_index:
                    warnings.append(f"{where}: narration lines are out of order ({ref})")
                last_index = max(last_index, line_index[ref])
        for character in shot["characters"]:
            if character not in characters:
                errors.append(f"{where}.characters: {character!r} is not defined in style.characters")
        shot_refs: list[str] = shot.get("references", [])
        for ref_id in shot_refs:
            ref = ref_by_id.get(ref_id)
            if ref is None:
                errors.append(f"{where}.references: {ref_id!r} is not in the references list")
                continue
            used_refs.add(ref_id)
            if ref["kind"] == "style":
                errors.append(
                    f"{where}.references: {ref_id!r} is a style reference; "
                    "style references apply to every shot, so do not list them"
                )
        for character in shot["characters"]:
            ref_id = character_refs.get(character)
            if ref_id is not None and ref_id not in shot_refs:
                warnings.append(
                    f"{where}: character {character!r} has a reference image ({ref_id}) "
                    "that this shot does not list"
                )
        selected = shot["keyframe"]["selected"]
        if selected is not None and selected > shot["keyframe"]["candidates"]:
            errors.append(
                f"{where}.keyframe.selected: {selected} is greater than candidates "
                f"({shot['keyframe']['candidates']})"
            )
        if shot["motion"]["type"] == "i2v" and shot["duration_s"] > LONG_I2V_SECONDS:
            warnings.append(
                f"{where}: {shot['duration_s']}s is long for an image-to-video clip; "
                "consider a shorter shot or a Ken Burns move"
            )
    for ref in references:
        if ref["kind"] != "style" and ref["id"] not in used_refs:
            warnings.append(f"references: {ref['id']} is not used by any shot")
    for line_id in line_ids:
        if line_id not in referenced:
            errors.append(f"narration: {line_id} is not used by any shot")

    # Total duration.
    total = round(sum(shot["duration_s"] for shot in shots) + data["end_card"]["duration_s"], 2)
    if not MIN_TOTAL_SECONDS <= total <= MAX_TOTAL_SECONDS:
        errors.append(
            f"total duration is {total}s (shots + end card); it must be between "
            f"{MIN_TOTAL_SECONDS:.0f} and {MAX_TOTAL_SECONDS:.0f} seconds"
        )

    # Music cues.
    ordered = sorted(cues, key=lambda cue: cue["start_s"])
    previous_end = 0.0
    for cue in ordered:
        where = f"music.cues ({cue['id']})"
        if cue["end_s"] <= cue["start_s"]:
            errors.append(f"{where}: end_s must be greater than start_s")
        if cue["end_s"] > total + 0.5:
            errors.append(f"{where}: ends at {cue['end_s']}s, after the film ends ({total}s)")
        if cue["start_s"] < previous_end - 0.01:
            errors.append(f"{where}: overlaps the previous cue")
        elif cue["start_s"] > previous_end + 0.5:
            warnings.append(f"{where}: gap of {cue['start_s'] - previous_end:.1f}s before this cue")
        previous_end = max(previous_end, cue["end_s"])
    if ordered and ordered[0]["start_s"] > 0.5:
        warnings.append("music.cues: the first cue does not start at 0s")
    if previous_end < total - 1.0:
        warnings.append(f"music.cues: music ends at {previous_end:.1f}s but the film runs {total}s")

    # Narration pace (only when a line has a shot to itself).
    uses = {ref: 0 for ref in line_ids}
    for shot in shots:
        if shot["narration_ref"] in uses:
            uses[shot["narration_ref"]] += 1
    for index, shot in enumerate(shots):
        ref = shot["narration_ref"]
        if ref in line_index and uses[ref] == 1:
            words = len(lines[line_index[ref]]["text_en"].split())
            pace = words / shot["duration_s"]
            if pace > MAX_WORDS_PER_SECOND:
                warnings.append(
                    f"shots[{index}] ({shot['id']}): {words} words in {shot['duration_s']}s "
                    f"({pace:.1f} words/s) is too fast to follow"
                )

    return errors, warnings


def validate_storyboard(
    data: Any,
    schema: dict[str, Any] | None = None,
    project_dir: Path | None = None,
) -> ValidationReport:
    """Validate an already-parsed storyboard. Pass project_dir to check reference files on disk."""
    schema = schema or load_schema()
    errors = _schema_errors(data, schema)
    if errors:
        # Semantic checks assume a structurally valid document.
        return ValidationReport(errors=errors)
    semantic_errors, warnings = _semantic_checks(data, project_dir)
    return ValidationReport(errors=semantic_errors, warnings=warnings)


def validate_file(
    path: Path,
    schema: dict[str, Any] | None = None,
    project_dir: Path | None = None,
) -> ValidationReport:
    """Validate a storyboard.json file on disk.

    Reference image paths in the storyboard are relative to the project folder,
    not to the folder holding storyboard.json. Pass project_dir, or leave it out
    when the file sits at <project>/production/storyboard.json and it is inferred.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return ValidationReport(errors=[f"file not found: {path}"])
    except json.JSONDecodeError as exc:
        return ValidationReport(errors=[f"{path.name} is not valid JSON: {exc}"])
    if project_dir is None:
        in_production = path.parent.name == STORYBOARD_RELATIVE.parent.name
        project_dir = path.parent.parent if in_production else path.parent
    return validate_storyboard(data, schema, project_dir=project_dir)


def main(argv: list[str] | None = None) -> int:
    """Standalone usage: python validate_storyboard.py path/to/storyboard.json"""
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print("Usage: python validate_storyboard.py <storyboard.json>", file=sys.stderr)
        return 2
    report = validate_file(Path(args[0]))
    print(report.format())
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())