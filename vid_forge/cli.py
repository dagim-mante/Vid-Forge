"""Command-line interface for vid-forge.

Supported commands:
    init name=<project_name>
    storyboard project=<project_name> [--force]
    validate project=<project_name>
    smoke_test [project=<project_name>]
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
import re
import shutil
import sys
from types import ModuleType
from typing import Sequence

ROOT_DIR = Path(__file__).resolve().parent.parent
PROJECTS_DIR = ROOT_DIR / "projects"
PIPELINE_DIR = ROOT_DIR / "pipeline"
SMOKE_TEST_SCRIPT = PIPELINE_DIR / "smoke-tests" / "smoke_test.py"
STORYBOARD_SCRIPT = PIPELINE_DIR / "script" / "storyboard.py"
VALIDATE_SCRIPT = PIPELINE_DIR / "script" / "validate_storyboard.py"
BRIEF_TEMPLATE = ROOT_DIR / "templates" / "brief.md"
PROJECT_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_]+$")
TRUE_VALUES = {"1", "true", "yes", "y"}
FALSE_VALUES = {"0", "false", "no", "n"}

USAGE = """Usage:
  vid-forge init name=<project_name>
  vid-forge storyboard project=<project_name> [--force]
  vid-forge validate project=<project_name>
  vid-forge smoke_test [project=<project_name>]

Commands:
  init        Create projects/<project_name>/ with input/brief.md to fill in.
  storyboard  Generate projects/<project_name>/production/storybook.json from input/brief.md.
              The output is validated against schema/storybook.schema.json first.
              Refuses to overwrite an existing storybook unless --force is given.
  validate    Validate an existing production/storybook.json (for example after hand edits).
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


def _parse_options(
    args: Sequence[str],
    value_keys: Sequence[str],
    flag_keys: Sequence[str] = (),
) -> dict[str, str | bool]:
    """Parse several options. Values: key=v, --key=v or --key v. Flags: force, --force or force=true."""
    options: dict[str, str | bool] = {}
    index = 0
    while index < len(args):
        token = args[index]
        if token in ("-h", "--help"):
            raise SystemExit(0)
        name, has_equals, inline = token.lstrip("-").partition("=")
        if name in value_keys:
            if has_equals:
                options[name] = inline
            elif token.startswith("--"):
                if index + 1 >= len(args):
                    raise ValueError(f"Missing value after --{name}.")
                options[name] = args[index + 1]
                index += 1
            else:
                raise ValueError(f"Expected {name}=<value>, got {token!r}.")
        elif name in flag_keys:
            if not has_equals:
                options[name] = True
            elif inline.lower() in TRUE_VALUES:
                options[name] = True
            elif inline.lower() in FALSE_VALUES:
                options[name] = False
            else:
                raise ValueError(f"Invalid value for {name}: {inline!r} (use true or false).")
        else:
            raise ValueError(f"Unexpected argument: {token!r}")
        index += 1
    return options


def _load_module(script: Path, name: str) -> ModuleType:
    """Load a pipeline script by path. Its folder goes on sys.path so sibling imports work."""
    spec = importlib.util.spec_from_file_location(name, script)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load module at {script}")
    script_dir = str(script.parent)
    if script_dir not in sys.path:
        sys.path.insert(0, script_dir)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_smoke_test_module() -> ModuleType:
    return _load_module(SMOKE_TEST_SCRIPT, "vid_forge_smoke_test")


def _resolve_project(name: str | None) -> tuple[Path | None, int]:
    """Validate a project name and make sure the folder exists. Returns (path, exit_code)."""
    if name is None:
        print("Error: missing required argument 'project=<project_name>'.", file=sys.stderr)
        return None, 2
    try:
        valid_name = _validate_project_name(name)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return None, 2
    project_dir = PROJECTS_DIR / valid_name
    if not project_dir.is_dir():
        print(
            f"Error: project '{valid_name}' does not exist under projects/. "
            f"Create it first with: init name={valid_name}",
            file=sys.stderr,
        )
        return None, 1
    return project_dir, 0


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

    input_dir = project_dir / "input"
    input_dir.mkdir(parents=True)
    brief_path = input_dir / "brief.md"
    if BRIEF_TEMPLATE.is_file():
        shutil.copyfile(BRIEF_TEMPLATE, brief_path)
    else:
        print(
            f"Warning: template not found at {BRIEF_TEMPLATE.relative_to(ROOT_DIR).as_posix()}; "
            "create input/brief.md yourself.",
            file=sys.stderr,
        )

    print(f"Created project '{valid_name}' at {project_dir.relative_to(ROOT_DIR).as_posix()}")
    if brief_path.is_file():
        print(f"Next: fill in {brief_path.relative_to(ROOT_DIR).as_posix()}, then run: storyboard project={valid_name}")
    return 0


def cmd_storyboard(args: Sequence[str]) -> int:
    """Handle `storyboard project=<project_name> [--force]`."""
    try:
        options = _parse_options(args, value_keys=("project",), flag_keys=("force",))
    except SystemExit:
        print("Usage: vid-forge storyboard project=<project_name> [--force]")
        return 0
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    project_dir, code = _resolve_project(options.get("project"))  # type: ignore[arg-type]
    if project_dir is None:
        return code

    module = _load_module(STORYBOARD_SCRIPT, "storyboard")
    return int(
        module.run_storyboard(
            project_dir,
            force=bool(options.get("force", False)),
            env_file=ROOT_DIR / ".env",
            base_dir=ROOT_DIR,
        )
    )


def cmd_validate(args: Sequence[str]) -> int:
    """Handle `validate project=<project_name>`."""
    try:
        options = _parse_options(args, value_keys=("project",))
    except SystemExit:
        print("Usage: vid-forge validate project=<project_name>")
        return 0
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    project_dir, code = _resolve_project(options.get("project"))  # type: ignore[arg-type]
    if project_dir is None:
        return code

    storyboard_path = project_dir / "production" / "storybook.json"
    if not storyboard_path.is_file():
        print(
            f"Error: {storyboard_path.relative_to(ROOT_DIR).as_posix()} not found. "
            f"Create it with: storyboard project={project_dir.name}",
            file=sys.stderr,
        )
        return 1

    module = _load_module(VALIDATE_SCRIPT, "validate_storyboard")
    report = module.validate_file(storyboard_path)
    print(report.format())
    return 0 if report.ok else 1


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
    if command == "storyboard":
        return cmd_storyboard(rest)
    if command == "validate":
        return cmd_validate(rest)
    if command == "smoke_test":
        return cmd_smoke_test(rest)

    print(f"Error: unknown command {command!r}.\n\n{USAGE}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())