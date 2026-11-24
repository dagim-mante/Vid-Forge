"""Smoke tests for configured AI service providers.

Tests each configured provider once, writes generated artifacts under an
output directory (default ``outputs/smoke/``), and appends one JSONL record
per test (including failures) to ``runs.jsonl``.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from typing import Any, Callable

from dotenv import load_dotenv
from google import genai
import requests

ROOT_DIR = Path(__file__).resolve().parents[2]
OUTPUT_DIR = ROOT_DIR / "outputs" / "smoke"
RUNS_FILE = ROOT_DIR / "runs.jsonl"
ENV_FILE = ROOT_DIR / ".env"
DEFAULT_GEMINI_MODEL = "gemini-3.8-flash"
DEFAULT_CLOUDFLARE_MODEL = "@cf/black-forest-labs/flux-2-klein-9b"
REQUEST_TIMEOUT_SECONDS = 60


@dataclass(frozen=True)
class ProviderSpec:
    """Definition of a smoke-tested provider."""

    name: str
    operation: str
    get_model: Callable[[], str]
    is_configured: Callable[[], bool]
    run: Callable[[str, Path], Path]


def _env_first(*names: str) -> str:
    """Return the first non-empty stripped environment variable value."""
    for name in names:
        value = os.getenv(name, "").strip()
        if value:
            return value
    return ""


def _gemini_configured() -> bool:
    return bool(_env_first("GEMINI_API_KEY"))


def _gemini_model() -> str:
    return _env_first("GEMINI_MODEL") or DEFAULT_GEMINI_MODEL


def _run_gemini(model: str, output_dir: Path) -> Path:
    api_key = _env_first("GEMINI_API_KEY")
    client = genai.Client(api_key=api_key)
    response = client.models.generate_content(
        model=model,
        contents="Reply with one short sentence confirming the Gemini API is operational.",
    )
    text = (response.text or "").strip()
    if not text:
        raise RuntimeError("Gemini returned an empty text response.")

    output_path = output_dir / "gemini.txt"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(text + "\n", encoding="utf-8")
    return output_path


def _cloudflare_credentials() -> tuple[str, str]:
    account_id = _env_first(
        "CLOUDFLARE_WORKERS_AI_ACCOUNT_ID",
        "CLOULDFLARE_WORKERS_AI_ACCOUNT_ID",
        "CLOUDFLARE_ACCOUNT_ID",
    )
    api_key = _env_first(
        "CLOUDFLARE_WORKERS_AI_API_KEY",
        "CLOULDFLARE_WORKERS_AI_API_KEY",
        "CLOUDFLARE_API_TOKEN",
        "CLOUDFLARE_API_KEY",
    )
    return account_id, api_key


def _cloudflare_configured() -> bool:
    account_id, api_key = _cloudflare_credentials()
    return bool(account_id and api_key)


def _cloudflare_model() -> str:
    return _env_first("CLOUDFLARE_MODEL") or DEFAULT_CLOUDFLARE_MODEL


def _run_cloudflare(model: str, output_dir: Path) -> Path:
    account_id, api_key = _cloudflare_credentials()
    url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/run/{model}"
    headers = {"Authorization": f"Bearer {api_key}"}
    prompt = "A minimal futuristic geometric cube glowing softly in a dark studio"

    # FLUX.2 models on Workers AI require multipart/form-data; older models use JSON.
    if "flux-2" in model.lower():
        response = requests.post(
            url,
            headers=headers,
            files={
                "prompt": (None, prompt),
                "width": (None, "512"),
                "height": (None, "512"),
            },
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    else:
        response = requests.post(
            url,
            headers=headers,
            json={"prompt": prompt},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )

    if not response.ok:
        raise RuntimeError(f"HTTP {response.status_code}: {response.text[:300]}")

    content_type = response.headers.get("Content-Type", "").lower()
    if "application/json" in content_type:
        payload = response.json()
        if not payload.get("success", True):
            raise RuntimeError(f"Cloudflare API error: {payload.get('errors')}")
        image_b64 = (payload.get("result") or {}).get("image")
        if not image_b64:
            raise RuntimeError("Cloudflare JSON response missing result.image.")
        image_bytes = base64.b64decode(image_b64)
    else:
        image_bytes = response.content

    if not image_bytes:
        raise RuntimeError("Cloudflare returned empty image bytes.")

    output_path = output_dir / "cloudflare.png"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(image_bytes)
    return output_path


def _kaggle_configured() -> bool:
    return bool(_env_first("KAGGLE_API_TOKEN"))


def _kaggle_model() -> str:
    return "kaggle-cli"


def _run_kaggle(_model: str, output_dir: Path) -> Path:
    # Check both PATH and the active Python environment's Scripts/bin directory.
    scripts_dir = str(Path(sys.executable).parent)
    executable = shutil.which("kaggle") or shutil.which("kaggle", path=scripts_dir)
    if not executable:
        raise RuntimeError("Kaggle CLI not found. Run: pip install kaggle")

    if not _env_first("KAGGLE_API_TOKEN"):
        raise RuntimeError("KAGGLE_API_TOKEN is missing from .env")

    result = subprocess.run(
        [executable, "datasets", "list", "-s", "mnist"],
        capture_output=True,
        text=True,
        timeout=REQUEST_TIMEOUT_SECONDS,
        check=False,
    )
    if result.returncode != 0:
        details = (result.stderr or result.stdout).strip()[:500]
        raise RuntimeError(f"Kaggle CLI failed: {details}")

    output_path = output_dir / "kaggle.txt"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(result.stdout, encoding="utf-8")
    return output_path


PROVIDERS: tuple[ProviderSpec, ...] = (
    ProviderSpec(
        name="gemini",
        operation="text_generation",
        get_model=_gemini_model,
        is_configured=_gemini_configured,
        run=_run_gemini,
    ),
    ProviderSpec(
        name="cloudflare",
        operation="image_generation",
        get_model=_cloudflare_model,
        is_configured=_cloudflare_configured,
        run=_run_cloudflare,
    ),
    ProviderSpec(
        name="kaggle",
        operation="api_authentication",
        get_model=_kaggle_model,
        is_configured=_kaggle_configured,
        run=_run_kaggle,
    ),
)


def _format_path(path: Path, base_dir: Path) -> str:
    try:
        return path.resolve().relative_to(base_dir.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _append_run_record(runs_file: Path, record: dict[str, Any]) -> None:
    runs_file.parent.mkdir(parents=True, exist_ok=True)
    with runs_file.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, separators=(",", ":")) + "\n")


def run_smoke_tests(
    output_dir: Path = OUTPUT_DIR,
    runs_file: Path = RUNS_FILE,
    env_file: Path = ENV_FILE,
    base_dir: Path = ROOT_DIR,
) -> int:
    """Run smoke tests for every configured provider and append results."""
    load_dotenv(env_file)

    configured = [provider for provider in PROVIDERS if provider.is_configured()]
    if not configured:
        print("No configured providers found in environment.")
        return 1

    print("Starting vid-forge smoke tests...\n")

    any_failed = False
    for provider in configured:
        model = provider.get_model()
        start = time.perf_counter()
        try:
            output_path = provider.run(model, output_dir)
            latency_ms = round((time.perf_counter() - start) * 1000)
            record: dict[str, Any] = {
                "provider": provider.name,
                "operation": provider.operation,
                "model": model,
                "success": True,
                "latency_ms": latency_ms,
                "output": _format_path(output_path, base_dir),
            }
            print(f"[PASS] {provider.name}: {latency_ms}ms")
        except Exception as exc:  # noqa: BLE001 - record any provider failure
            latency_ms = round((time.perf_counter() - start) * 1000)
            any_failed = True
            record = {
                "provider": provider.name,
                "operation": provider.operation,
                "model": model,
                "success": False,
                "latency_ms": latency_ms,
                "error": str(exc),
            }
            print(f"[FAIL] {provider.name}: {latency_ms}ms - {exc}")

        _append_run_record(runs_file, record)

    print(f"\nResults appended to {_format_path(runs_file, base_dir)}")
    return 1 if any_failed else 0


def main() -> int:
    """CLI entrypoint for standalone smoke test execution."""
    return run_smoke_tests()


if __name__ == "__main__":
    sys.exit(main())
