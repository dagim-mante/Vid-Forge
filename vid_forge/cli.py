"""Command-line interface for vid-forge.

Supported commands:
    init name=<project_name>
    smoke_test [project=<project_name>]
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
import re
import sys
from types import ModuleType
from typing import Sequence

ROOT_DIR = Path(__file__).resolve().parent.parent
PROJECTS_DIR = ROOT_DIR / "projects"
SMOKE_TEST_SCRIPT = ROOT_DIR / "pipeline" / "smoke-tests" / "smoke_test.py"
PROJECT_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_]+$")

USAGE = """Usage:
  vid-forge init name=<project_name>
  vid-forge smoke_test [project=<project_name>]

Commands:
  init        Create a new project folder under projects/<project_name>.
  smoke_test  Run smoke tests (writes under projects/<project_name>/ if given)."""


def _validate_project_name(name: str) -> str:
    """Validate that a project name contains only ASCII word characters and digits."""
    if not name or not PROJECT_NAME_PATTERN.fullmatch(name):
        raise ValueError(
            f"Invalid project name {name!r}. Name must contain only letters, numbers, and underscores."
        )
    return name


def _parse_kv_args(args: Sequence[str], expected_key: str) -> str | None:
    """Parse key=value (or --key value / --key=value) arguments."""
    value: str | None = None
    index = 0
    while index < len(args):
        token = args[index]
        if token.startswith(f"{expected_key}="):
            value = token.split("=", 1)[1]
        elif token.startswith(f"--{expected_key}="):
            value = token.split("=", 1)[1]
        elif token == f"--{expected_key}":
            if index + 1 >= len(args):
                raise ValueError(f"Missing value after --{expected_key}.")
            value = args[index + 1]
            index += 1
        elif token in ("-h", "--help"):
            raise SystemExit(0)
        else:
            raise ValueError(f"Unexpected argument: {token!r}")
        index += 1
    return value


def _load_smoke_test_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("vid_forge_smoke_test", SMOKE_TEST_SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load smoke test module at {SMOKE_TEST_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def cmd_init(args: Sequence[str]) -> int:
    """Handle `init name=<project_name>`."""
    try:
        name = _parse_kv_args(args, "name")
    except SystemExit:
        print("Usage: vid-forge init name=<project_name>")
        return 0
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    if name is None:
        print("Error: missing required argument 'name=<project_name>'.", file=sys.stderr)
        return 2

    try:
        valid_name = _validate_project_name(name)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    project_dir = PROJECTS_DIR / valid_name
    if project_dir.exists():
        print(
            f"Error: project '{valid_name}' already exists at {project_dir.relative_to(ROOT_DIR).as_posix()}.",
            file=sys.stderr,
        )
        return 1

    project_dir.mkdir(parents=True)
    print(f"Created project '{valid_name}' at {project_dir.relative_to(ROOT_DIR).as_posix()}")
    return 0


def cmd_smoke_test(args: Sequence[str]) -> int:
    """Handle `smoke_test [project=<project_name>]`."""
    try:
        project_name = _parse_kv_args(args, "project")
    except SystemExit:
        print("Usage: vid-forge smoke_test [project=<project_name>]")
        return 0
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    smoke_module = _load_smoke_test_module()

    if project_name is not None:
        try:
            valid_name = _validate_project_name(project_name)
        except ValueError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 2

        project_dir = PROJECTS_DIR / valid_name
        if not project_dir.is_dir():
            print(
                f"Error: project '{valid_name}' does not exist under projects/. "
                f"Create it first with: init name={valid_name}",
                file=sys.stderr,
            )
            return 1

        output_dir = project_dir / "outputs" / "smoke"
        runs_file = project_dir / "runs.jsonl"
        return int(
            smoke_module.run_smoke_tests(
                output_dir=output_dir,
                runs_file=runs_file,
                env_file=ROOT_DIR / ".env",
                base_dir=ROOT_DIR,
            )
        )

    return int(smoke_module.run_smoke_tests())


def main(argv: Sequence[str] | None = None) -> int:
    """Dispatch CLI subcommands."""
    raw_args = list(sys.argv[1:] if argv is None else argv)
    if not raw_args or raw_args[0] in ("-h", "--help"):
        print(USAGE)
        return 0

    command, rest = raw_args[0], raw_args[1:]
    if command == "init":
        return cmd_init(rest)
    if command == "smoke_test":
        return cmd_smoke_test(rest)

    print(f"Error: unknown command {command!r}.\n\n{USAGE}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
