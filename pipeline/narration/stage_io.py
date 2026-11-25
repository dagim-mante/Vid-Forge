"""Small helpers shared by the narration stage and the radio cut.

Same conventions as the script stage: one JSON line per call in
projects/<name>/runs.jsonl, files written atomically, paths printed relative
to the repository root.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
from typing import Any

NARRATION_DIR_NAME = "narration"
RADIO_CUT_DIR_NAME = "radio_cut"


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def rel(path: Path, base_dir: Path) -> str:
    """Path relative to base_dir with forward slashes, for printing and logging."""
    try:
        return path.resolve().relative_to(base_dir.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def append_run_record(runs_file: Path, record: dict[str, Any]) -> None:
    runs_file.parent.mkdir(parents=True, exist_ok=True)
    with runs_file.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, separators=(",", ":"), ensure_ascii=False) + "\n")


def write_json_atomic(path: Path, data: Any) -> None:
    """Write via a temp file so a crash never leaves a half-written file that looks valid."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(temp, path)


def write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(text, encoding="utf-8")
    os.replace(temp, path)


def narration_dir(project_dir: Path) -> Path:
    return project_dir / "outputs" / NARRATION_DIR_NAME


def radio_cut_dir(project_dir: Path) -> Path:
    return project_dir / "outputs" / RADIO_CUT_DIR_NAME