"""Smoke tests for configured AI service providers.

Tests each configured provider once, writes generated artifacts under
``outputs/smoke/``, and appends one JSONL record per test (including
failures) to ``runs.jsonl``.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
import json
import os
from pathlib import Path
import sys
import time
from typing import Any, Callable

from dotenv import load_dotenv
from google import genai
import requests

OUTPUT_DIR = Path("outputs/smoke")
RUNS_FILE = Path("runs.jsonl")
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
    run: Callable[[str], str]


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


def _run_gemini(model: str) -> str:
    api_key = _env_first("GEMINI_API_KEY")
    client = genai.Client(api_key=api_key)
    response = client.models.generate_content(
        model=model,
        contents="Reply with one short sentence confirming the Gemini API is operational.",
    )
    text = (response.text or "").strip()
    if not text:
        raise RuntimeError("Gemini returned an empty text response.")

    output_path = OUTPUT_DIR / "gemini.txt"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(text + "\n", encoding="utf-8")
    return output_path.as_posix()


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


def _run_cloudflare(model: str) -> str:
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

    output_path = OUTPUT_DIR / "cloudflare.png"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(image_bytes)
    return output_path.as_posix()


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
)


def _append_run_record(record: dict[str, Any]) -> None:
    with RUNS_FILE.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, separators=(",", ":")) + "\n")


def main() -> int:
    """Run smoke tests for every configured provider and append results."""
    load_dotenv()

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
            output_path = provider.run(model)
            latency_ms = round((time.perf_counter() - start) * 1000)
            record: dict[str, Any] = {
                "provider": provider.name,
                "operation": provider.operation,
                "model": model,
                "success": True,
                "latency_ms": latency_ms,
                "output": output_path,
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

        _append_run_record(record)

    print(f"\nResults appended to {RUNS_FILE.as_posix()}")
    return 1 if any_failed else 0


if __name__ == "__main__":
    sys.exit(main())
