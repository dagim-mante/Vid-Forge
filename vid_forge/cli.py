"""Command-line interface for vid-forge.

Supported commands:
    init name=<project_name>
    storyboard project=<project_name> [--force]
    keyframes project=<project_name> [--shot shot_01] [--candidate-count <n>] [--force]
    validate project=<project_name>
    narration project=<project_name> [line=<line_id>] [--force] [--no-assemble]
    video project=<project_name> [--shot shot_01] [--provider wan22|hunyuan15] [--force]
    assemble project=<project_name> [--transition cut|crossfade] [--transition-duration <seconds>]
             [--output <filename>] [--dry-run] [--force]
    film project=<project_name> [--candidate-count <n>] [--steps <n>] [--accelerator <name>]
         [--transition cut|crossfade] [--transition-duration <seconds>] [--dry-run]
    radio_cut project=<project_name>
    smoke_test [project=<project_name>]
"""

from __future__ import annotations

import importlib.util
import os
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
SCRIPT_DIR = PIPELINE_DIR / "script"
KEYFRAMES_DIR = PIPELINE_DIR / "keyframes"
STORYBOARD_SCRIPT = SCRIPT_DIR / "storyboard.py"
KEYFRAMES_SCRIPT = KEYFRAMES_DIR / "keyframes.py"
VALIDATE_SCRIPT = SCRIPT_DIR / "validate_storyboard.py"
NARRATION_DIR = PIPELINE_DIR / "narration"
NARRATION_SCRIPT = NARRATION_DIR / "narration.py"
VIDEO_ASSEMBLE_SCRIPT = PIPELINE_DIR / "video-assemble" / "video_assemble.py"
FILM_ASSEMBLY_SCRIPT = PIPELINE_DIR / "assembly" / "assemble.py"
RADIO_CUT_SCRIPT = NARRATION_DIR / "radio_cut.py"
PROJECT_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_]+$")
TRUE_VALUES = {"1", "true", "yes", "y"}
FALSE_VALUES = {"0", "false", "no", "n"}

USAGE = """Usage:
  vid-forge init name=<project_name>
  vid-forge storyboard project=<project_name> [--force]
  vid-forge keyframes project=<project_name> [--shot shot_01] [--candidate-count <n>] [--force]
  vid-forge validate project=<project_name>
  vid-forge narration project=<project_name> [line=<line_id>] [--force] [--no-assemble]
  vid-forge radio_cut project=<project_name>
  vid-forge assemble project=<project_name> [--transition cut|crossfade]
                      [--transition-duration <seconds>] [--output <filename>]
                      [--dry-run] [--force]
  vid-forge film project=<project_name> [--candidate-count <n>] [--steps <n>]
                   [--accelerator <name>] [--transition cut|crossfade]
                   [--transition-duration <seconds>] [--dry-run]
  vid-forge smoke_test [project=<project_name>]
  vid-forge video project=<project_name> [--shot shot_01] [--provider wan22|hunyuan15]
              [--force] [--dry-run] [--validate-only] [--accelerator <name>]
              [--keyframes-dir <path>] [--steps <n>] [--guidance <value>]

Commands:
  init        Create projects/<project_name>/ with input/brief.md to fill in, an
              input/references/ folder for optional reference images, and production/.
  storyboard  Generate projects/<project_name>/production/storyboard.json from input/brief.md.
              The output is validated against schema/storyboard.schema.json first.
              Refuses to overwrite an existing storyboard unless --force is given.
  keyframes   Generate candidate images for every shot or one shot; candidate 1 is selected by default
              when successful, while any existing selection is preserved.
              --candidate-count updates keyframe.candidates in the storyboard for the selected scope (1 to 6).
  validate    Validate an existing storyboard.json (for example after hand edits).
  narration   Generate the narration with Gemini TTS (one audio file per line, cached),
              work out the timing, write English subtitles, then assemble the radio cut:
              narration over placeholder music on a black screen. line=<id> regenerates
              one line only; --no-assemble skips the radio cut.
  radio_cut   Rebuild the radio cut from existing narration audio (no API calls).
  video       Generate selected-keyframe clips, using local Ken Burns and Kaggle Wan i2v.
  assemble    Combine per-shot videos with timed narration, music, and burned-in subtitles.
              Writes 1280x720 and 720x1280 finals; supports hard cuts and crossfades.
  film        Run storyboard (if missing), validation, keyframes, narration, video,
              and final assembly in order; stops on the first failed stage.
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
    references_dir = input_dir / "references"
    references_dir.mkdir(parents=True)
    (references_dir / ".gitkeep").touch()
    (project_dir / "production").mkdir()
    brief_path = input_dir / "brief.md"
    brief_path.touch()

    print(f"Created project '{valid_name}' at {project_dir.relative_to(ROOT_DIR).as_posix()}")
    if brief_path.is_file():
        print(f"Next: fill in {brief_path.relative_to(ROOT_DIR).as_posix()}, then run: storyboard project={valid_name}")
        print(
            f"Optional: add reference images to {references_dir.relative_to(ROOT_DIR).as_posix()}/ "
            "named character_<name>.png, scene_<name>.jpg or style_<name>.webp"
        )
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

    module = _load_module(VALIDATE_SCRIPT, "validate_storyboard")
    storyboard_file = module.storyboard_path(project_dir)
    if not storyboard_file.is_file():
        print(
            f"Error: {storyboard_file.relative_to(ROOT_DIR).as_posix()} not found. "
            f"Create it with: storyboard project={project_dir.name}",
            file=sys.stderr,
        )
        return 1

    report = module.validate_file(storyboard_file, project_dir=project_dir)
    print(report.format())
    return 0 if report.ok else 1


def cmd_keyframes(args: Sequence[str]) -> int:
    """Handle `keyframes project=<name> [--shot <shot_id>] [--candidate-count <n>] [--force]`."""
    try:
        options = _parse_options(args, value_keys=("project", "shot", "candidate-count"), flag_keys=("force",))
    except SystemExit:
        print("Usage: vid-forge keyframes project=<project_name> [--shot shot_01] [--candidate-count <n>] [--force]")
        return 0
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    project_dir, code = _resolve_project(options.get("project"))  # type: ignore[arg-type]
    if project_dir is None:
        return code
    try:
        candidate_count = int(str(options["candidate-count"])) if options.get("candidate-count") is not None else None
    except ValueError:
        print("Error: --candidate-count must be an integer between 1 and 6.", file=sys.stderr)
        return 2
    module = _load_module(KEYFRAMES_SCRIPT, "keyframes")
    return int(module.run_keyframes(
        project_dir,
        shot_id=options.get("shot"),  # type: ignore[arg-type]
        candidate_count=candidate_count,
        force=bool(options.get("force", False)),
        env_file=ROOT_DIR / ".env",
        base_dir=ROOT_DIR,
    ))


def cmd_narration(args: Sequence[str]) -> int:
    """Handle `narration project=<name> [line=<id>] [--force] [--no-assemble]`."""
    try:
        options = _parse_options(args, value_keys=("project", "line"), flag_keys=("force", "no-assemble"))
    except SystemExit:
        print("Usage: vid-forge narration project=<project_name> [line=<line_id>] [--force] [--no-assemble]")
        return 0
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    project_dir, code = _resolve_project(options.get("project"))  # type: ignore[arg-type]
    if project_dir is None:
        return code

    module = _load_module(NARRATION_SCRIPT, "narration")
    return int(
        module.run_narration(
            project_dir,
            force=bool(options.get("force", False)),
            only_line=options.get("line"),  # type: ignore[arg-type]
            assemble=not bool(options.get("no-assemble", False)),
            env_file=ROOT_DIR / ".env",
            base_dir=ROOT_DIR,
        )
    )


def cmd_radio_cut(args: Sequence[str]) -> int:
    """Handle `radio_cut project=<name>`."""
    try:
        options = _parse_options(args, value_keys=("project",))
    except SystemExit:
        print("Usage: vid-forge radio_cut project=<project_name>")
        return 0
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    project_dir, code = _resolve_project(options.get("project"))  # type: ignore[arg-type]
    if project_dir is None:
        return code

    module = _load_module(RADIO_CUT_SCRIPT, "radio_cut")
    return int(module.run_radio_cut(project_dir, base_dir=ROOT_DIR))


def cmd_video(args: Sequence[str]) -> int:
    """Handle the video assembly stage (individual shot clips only)."""
    try:
        options = _parse_options(
            args,
            value_keys=("project", "shot", "provider", "accelerator", "keyframes-dir", "steps", "guidance"),
            flag_keys=("force", "dry-run", "validate-only"),
        )
    except SystemExit:
        print("Usage: vid-forge video project=<project_name> [--shot shot_01] [--provider wan22|hunyuan15] [--force] [--dry-run] [--validate-only]")
        return 0
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    project_dir, code = _resolve_project(options.get("project"))  # type: ignore[arg-type]
    if project_dir is None:
        return code
    module = _load_module(VIDEO_ASSEMBLE_SCRIPT, "video_assemble")
    override = options.get("keyframes-dir")
    return int(module.run_video(
        project_dir,
        provider_name=str(options.get("provider", "wan22")),
        shot_id=options.get("shot"),  # type: ignore[arg-type]
        force=bool(options.get("force", False)),
        dry_run=bool(options.get("dry-run", False)),
        validate_only=bool(options.get("validate-only", False)),
        accelerator=options.get("accelerator"),  # type: ignore[arg-type]
        keyframes_dir=Path(str(override)) if override else None,
        generation_steps=int(str(options["steps"])) if options.get("steps") is not None else 12,
        guidance=float(str(options["guidance"])) if options.get("guidance") is not None else 5.0,
        env_file=ROOT_DIR / ".env",
    ))


def cmd_assemble(args: Sequence[str]) -> int:
    """Handle `assemble project=<project_name>` for final film assembly."""
    try:
        options = _parse_options(
            args,
            value_keys=("project", "transition", "transition-duration", "output"),
            flag_keys=("dry-run", "force"),
        )
    except SystemExit:
        print(
            "Usage: vid-forge assemble project=<project_name> [--transition cut|crossfade] "
            "[--transition-duration <seconds>] [--output <filename>] [--dry-run] [--force]"
        )
        return 0
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    project_dir, code = _resolve_project(options.get("project"))  # type: ignore[arg-type]
    if project_dir is None:
        return code
    try:
        transition_duration = (
            float(str(options["transition-duration"]))
            if options.get("transition-duration") is not None else 0.5
        )
    except ValueError:
        print("Error: --transition-duration must be a positive number of seconds.", file=sys.stderr)
        return 2
    module = _load_module(FILM_ASSEMBLY_SCRIPT, "film_assembly")
    return int(module.run_assembly(
        project_dir,
        transition=str(options.get("transition", "cut")),
        transition_duration=transition_duration,
        output_name=str(options["output"]) if options.get("output") is not None else None,
        dry_run=bool(options.get("dry-run", False)),
        force=bool(options.get("force", False)),
    ))


def cmd_film(args: Sequence[str]) -> int:
    """Run every production stage in dependency order for one project."""
    try:
        options = _parse_options(
            args,
            value_keys=("project", "candidate-count", "steps", "accelerator", "transition", "transition-duration"),
            flag_keys=("dry-run",),
        )
    except SystemExit:
        print("Usage: vid-forge film project=<project_name> [--candidate-count <n>] [--steps <n>] "
              "[--accelerator <name>] [--transition cut|crossfade] [--transition-duration <seconds>] [--dry-run]")
        return 0
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    project_dir, code = _resolve_project(options.get("project"))  # type: ignore[arg-type]
    if project_dir is None:
        return code
    try:
        candidate_count = int(str(options.get("candidate-count", "2")))
        steps = int(str(options.get("steps", "12")))
        transition_duration = float(str(options.get("transition-duration", "0.5")))
    except ValueError as exc:
        print(f"Error: invalid numeric film option: {exc}", file=sys.stderr)
        return 2
    if not 1 <= candidate_count <= 6:
        print("Error: --candidate-count must be between 1 and 6.", file=sys.stderr)
        return 2
    if steps < 1:
        print("Error: --steps must be a positive integer.", file=sys.stderr)
        return 2
    if transition_duration <= 0:
        print("Error: --transition-duration must be a positive number of seconds.", file=sys.stderr)
        return 2

    transition = str(options.get("transition", "cut"))
    if transition not in {"cut", "crossfade"}:
        print("Error: --transition must be cut or crossfade.", file=sys.stderr)
        return 2

    brief = project_dir / "input" / "brief.md"
    storyboard = project_dir / "production" / "storyboard.json"
    if not brief.is_file() or not brief.read_text(encoding="utf-8").strip():
        print(f"Error: {brief} is missing or empty; add the film brief before running the pipeline.", file=sys.stderr)
        return 1

    generate_storyboard = not storyboard.is_file()
    accelerator = options.get("accelerator")
    print(f"Film pipeline for {project_dir.name}:")
    print(f"  1. {'generate storyboard from brief' if generate_storyboard else 'reuse checked-in storyboard'}")
    print("  2. validate storyboard")
    print(f"  3. generate keyframes ({candidate_count} candidate(s) per shot)")
    print("  4. generate narration, subtitles, and radio-cut music bed")
    print(f"  5. generate shot videos (Wan 2.2, {steps} steps, accelerator={accelerator or 'KAGGLE_ACCELERATOR/default'})")
    print(f"  6. assemble 16:9 and 9:16 films (transition={transition})")
    if options.get("dry-run", False):
        print("Dry run only; no API calls, Kaggle jobs, or output files were created.")
        print("Full runs need Gemini, Cloudflare Workers AI, Kaggle credentials, ffmpeg, and ffprobe.")
        return 0

    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT_DIR / ".env")
    except ImportError:
        pass
    required_credentials = {
        "GEMINI_API_KEY": bool(os.getenv("GEMINI_API_KEY", "").strip()),
        "CLOUDFLARE_WORKERS_AI_ACCOUNT_ID": bool(
            (os.getenv("CLOUDFLARE_WORKERS_AI_ACCOUNT_ID", "") or
             os.getenv("CLOULDFLARE_WORKERS_AI_ACCOUNT_ID", "") or
             os.getenv("CLOUDFLARE_ACCOUNT_ID", "")).strip()
        ),
        "CLOUDFLARE_WORKERS_AI_API_KEY": bool(
            (os.getenv("CLOUDFLARE_WORKERS_AI_API_KEY", "") or
             os.getenv("CLOULDFLARE_WORKERS_AI_API_KEY", "") or
             os.getenv("CLOUDFLARE_API_TOKEN", "") or
             os.getenv("CLOUDFLARE_API_KEY", "")).strip()
        ),
        "KAGGLE_API_TOKEN": bool(os.getenv("KAGGLE_API_TOKEN", "").strip()),
        "KAGGLE_USERNAME": bool(os.getenv("KAGGLE_USERNAME", "").strip()),
    }
    missing_credentials = [name for name, present in required_credentials.items() if not present]
    missing_tools = [name for name in ("ffmpeg", "ffprobe") if shutil.which(name) is None]
    if missing_credentials or missing_tools:
        if missing_credentials:
            print("Error: set these values in the private .env file (copy .env.example): "
                  + ", ".join(missing_credentials), file=sys.stderr)
        if missing_tools:
            print("Error: install and add these executables to PATH: " + ", ".join(missing_tools), file=sys.stderr)
        return 1

    if generate_storyboard:
        print("\n[1/6] Generating storyboard")
        result = cmd_storyboard([f"project={project_dir.name}"])
        if result:
            return result
    else:
        print("\n[1/6] Reusing the existing storyboard")

    stages: list[tuple[str, list[str]]] = [
        ("[2/6] Validating storyboard", ["validate", f"project={project_dir.name}"]),
        ("[3/6] Generating keyframes", ["keyframes", f"project={project_dir.name}", "--candidate-count", str(candidate_count)]),
        ("[4/6] Generating narration and radio cut", ["narration", f"project={project_dir.name}"]),
    ]
    video_args = ["video", f"project={project_dir.name}", "--provider", "wan22", "--steps", str(steps)]
    if accelerator:
        video_args.extend(["--accelerator", str(accelerator)])
    stages.append(("[5/6] Generating shot videos", video_args))
    assembly_args = ["assemble", f"project={project_dir.name}", "--transition", transition,
                     "--transition-duration", str(transition_duration), "--force"]
    stages.append(("[6/6] Assembling final films", assembly_args))

    commands = {
        "validate": cmd_validate,
        "keyframes": cmd_keyframes,
        "narration": cmd_narration,
        "video": cmd_video,
        "assemble": cmd_assemble,
    }
    for label, command in stages:
        print(f"\n{label}", flush=True)
        result = commands[command[0]](command[1:])
        if result:
            print(f"Film pipeline stopped at {label}; fix the reported issue and rerun. Cached work will be reused.",
                  file=sys.stderr)
            return result
    print(f"\nFilm pipeline complete: {project_dir / 'production' / 'final.mp4'} and final_9x16.mp4")
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
    if command == "storyboard":
        return cmd_storyboard(rest)
    if command == "validate":
        return cmd_validate(rest)
    if command == "keyframes":
        return cmd_keyframes(rest)
    if command == "narration":
        return cmd_narration(rest)
    if command == "radio_cut":
        return cmd_radio_cut(rest)
    if command == "video":
        return cmd_video(rest)
    if command == "assemble":
        return cmd_assemble(rest)
    if command == "film":
        return cmd_film(rest)
    if command == "smoke_test":
        return cmd_smoke_test(rest)

    print(f"Error: unknown command {command!r}.\n\n{USAGE}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
