"""Standalone live check of the Ollama integration (no FastAPI required).

Requires a locally running Ollama server with the configured model installed.
Run from anywhere, e.g. from the repository root in PowerShell::

    python backend\\scripts\\check_ollama_live.py

Exit code 0 = every step passed, 1 = a step failed.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from pathlib import Path

# Make `import app` work regardless of the current working directory.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import ConfigError, OllamaSettings  # noqa: E402
from app.services import OllamaError, OllamaService  # noqa: E402

TEST_PROMPT = """\
Explain the following Python code line by line.

```python
numbers = [1, 2, 3, 4, 5]

for number in numbers:
    print(number)
```
"""


def step(message: str) -> None:
    print(f"\n==> {message}", flush=True)


def fail(message: str) -> int:
    print(f"[FAIL] {message}", flush=True)
    print("\nRESULT: FAILURE", flush=True)
    return 1


async def run(settings: OllamaSettings) -> int:
    async with OllamaService(settings) as ollama:
        step("1/3 Checking Ollama server health")
        health = await ollama.health_check()
        if not health.healthy:
            return fail(f"{health.detail}  (is `ollama serve` running at {settings.base_url}?)")
        print(f"[ OK ] {health.detail} version={health.version}")

        step(f"2/3 Checking model '{settings.model}' is installed")
        try:
            installed = await ollama.model_available()
        except OllamaError as exc:
            return fail(f"{exc.user_message}  [{exc}]")
        if not installed:
            return fail(
                f"Model '{settings.model}' is not installed. Run `ollama list` to see "
                "installed models. (This script never pulls models.)"
            )
        print(f"[ OK ] Model '{settings.model}' is installed")

        step("3/3 Running inference (first call includes model load time)")
        started = time.perf_counter()
        try:
            text = await ollama.generate(TEST_PROMPT)
        except OllamaError as exc:
            elapsed = time.perf_counter() - started
            return fail(f"{exc.user_message}  [{type(exc).__name__}: {exc}] after {elapsed:.2f}s")
        elapsed = time.perf_counter() - started

        print("----- model response -----")
        print(text.strip())
        print("--------------------------")
        print(f"[ OK ] Received {len(text)} characters in {elapsed:.2f}s")

    print("\nRESULT: SUCCESS", flush=True)
    return 0


def main() -> int:
    # The Windows console often defaults to a legacy code page; model output
    # routinely contains characters (arrows, quotes, emoji) it cannot encode.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    logging.basicConfig(level=logging.WARNING, format="[%(levelname)s] %(name)s: %(message)s")

    try:
        settings = OllamaSettings.from_env()
    except ConfigError as exc:
        return fail(f"Invalid configuration: {exc}")

    print("Sift - Ollama live inference check")
    print(
        f"  base_url={settings.base_url}  model={settings.model}\n"
        f"  num_ctx={settings.num_ctx}  num_predict={settings.num_predict}  "
        f"temperature={settings.temperature}\n"
        f"  timeouts: connect={settings.connect_timeout}s read={settings.read_timeout}s "
        f"write={settings.write_timeout}s pool={settings.pool_timeout}s"
    )

    try:
        return asyncio.run(run(settings))
    except KeyboardInterrupt:
        return fail("Interrupted by user")
    except Exception as exc:  # noqa: BLE001 - last-resort so the script always reports cleanly
        return fail(f"Unexpected error: {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    sys.exit(main())
