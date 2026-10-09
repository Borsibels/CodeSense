"""Central configuration for the local Ollama integration.

Every default lives here. Each value can be overridden with an environment
variable of the same name as the ``DEFAULT_`` constant minus the prefix, e.g.
in PowerShell::

    $env:OLLAMA_MODEL = "qwen2.5-coder:7b"
    $env:OLLAMA_READ_TIMEOUT = "300"

No cloud credentials or third-party configuration libraries are involved.
"""

from __future__ import annotations

import math
import os
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlparse

# Initial development defaults - tune after performance testing.
DEFAULT_OLLAMA_BASE_URL = "http://127.0.0.1:11434"
DEFAULT_OLLAMA_MODEL = "qwen2.5-coder:3b"

DEFAULT_OLLAMA_NUM_CTX = 4096
DEFAULT_OLLAMA_NUM_PREDICT = 1024
DEFAULT_OLLAMA_TEMPERATURE = 0.2

DEFAULT_OLLAMA_CONNECT_TIMEOUT = 5.0
DEFAULT_OLLAMA_READ_TIMEOUT = 120.0
DEFAULT_OLLAMA_WRITE_TIMEOUT = 10.0
DEFAULT_OLLAMA_POOL_TIMEOUT = 5.0
# Read timeout for lightweight metadata calls (health, model list) so a hung
# server does not block a probe for the full generation timeout.
DEFAULT_OLLAMA_HEALTH_READ_TIMEOUT = 10.0


# API layer defaults (see ApiSettings).
# num_ctx is 4096 tokens and the model may spend up to num_predict of them on its
# answer, so ~3k prompt tokens (~6000 chars of code) is the safe ceiling before
# Ollama starts silently truncating the prompt.
DEFAULT_AI_MAX_PROMPT_CHARS = 6000
# One request at a time: the target GPU has 4 GB VRAM.
DEFAULT_AI_MAX_CONCURRENT_REQUESTS = 1
# How long a request may wait for the inference slot before getting AI_BUSY.
DEFAULT_AI_QUEUE_WAIT_TIMEOUT = 30.0
# Vite dev server and preview server (browser calls need CORS; there is no proxy).
DEFAULT_API_CORS_ORIGINS = (
    "http://127.0.0.1:5173",
    "http://localhost:5173",
    "http://127.0.0.1:4173",
    "http://localhost:4173",
)


# Token budget: 4096 total - 1024 reserved generation - 256 margin = 2816 input tokens.
# The margin must also absorb Ollama's chat-template overhead (~29 tokens) and
# any estimator error, so keep it well above that.
DEFAULT_AI_SAFETY_MARGIN_TOKENS = 256
# Structured output: extra attempts after the first one (so 1 = at most 2 attempts).
DEFAULT_AI_STRUCTURED_MAX_RETRIES = 1
MAX_STRUCTURED_RETRIES = 5
# Only loopback names may be used in the Host header (DNS-rebinding defence).
DEFAULT_API_ALLOWED_HOSTS = ("127.0.0.1", "localhost", "[::1]")
# Hard cap on a request body, checked before the body is parsed. Far above a
# max-size prompt (JSON-escaped); upload endpoints will need their own limit.
DEFAULT_API_MAX_BODY_BYTES = 131072


class ConfigError(ValueError):
    """Raised when a setting (or its environment override) is invalid."""


def _read_env(env: Mapping[str, str], var: str, default, convert):
    """Read ``var`` from ``env``; blank/unset gives ``default``, bad values raise."""
    raw = env.get(var)
    if raw is None or not raw.strip():
        return default
    try:
        return convert(raw.strip())
    except ValueError as exc:
        raise ConfigError(
            f"Invalid value for {var}: {raw!r} (expected {convert.__name__})"
        ) from exc


def _origins(raw: str) -> tuple[str, ...]:
    return tuple(part.strip().rstrip("/") for part in raw.split(",") if part.strip())


def _hosts(raw: str) -> tuple[str, ...]:
    return tuple(part.strip().lower() for part in raw.split(",") if part.strip())


@dataclass(frozen=True)
class OllamaSettings:
    base_url: str = DEFAULT_OLLAMA_BASE_URL
    model: str = DEFAULT_OLLAMA_MODEL

    num_ctx: int = DEFAULT_OLLAMA_NUM_CTX
    num_predict: int = DEFAULT_OLLAMA_NUM_PREDICT
    temperature: float = DEFAULT_OLLAMA_TEMPERATURE

    connect_timeout: float = DEFAULT_OLLAMA_CONNECT_TIMEOUT
    read_timeout: float = DEFAULT_OLLAMA_READ_TIMEOUT
    write_timeout: float = DEFAULT_OLLAMA_WRITE_TIMEOUT
    pool_timeout: float = DEFAULT_OLLAMA_POOL_TIMEOUT
    health_read_timeout: float = DEFAULT_OLLAMA_HEALTH_READ_TIMEOUT

    def __post_init__(self) -> None:
        parsed = urlparse(self.base_url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ConfigError(
                f"base_url must be an http(s) URL such as {DEFAULT_OLLAMA_BASE_URL!r}, "
                f"got {self.base_url!r}"
            )
        # "http://host:11434/" and "http://host:11434" must behave identically.
        object.__setattr__(self, "base_url", self.base_url.rstrip("/"))

        if not self.model or not self.model.strip():
            raise ConfigError("model must not be empty")

        for name in ("num_ctx", "num_predict"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ConfigError(f"{name} must be a positive integer, got {value!r}")

        if not math.isfinite(self.temperature) or self.temperature < 0:
            raise ConfigError(f"temperature must be a finite number >= 0, got {self.temperature!r}")

        for name in (
            "connect_timeout",
            "read_timeout",
            "write_timeout",
            "pool_timeout",
            "health_read_timeout",
        ):
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0:
                raise ConfigError(f"{name} must be a finite number > 0, got {value!r}")

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> OllamaSettings:
        """Build settings from defaults, overridden by ``OLLAMA_*`` variables.

        Unset or blank variables fall back to the default. Invalid values raise
        :class:`ConfigError` naming the offending variable.
        """
        env = os.environ if environ is None else environ

        def read(var: str, default, convert):
            return _read_env(env, var, default, convert)

        return cls(
            base_url=read("OLLAMA_BASE_URL", DEFAULT_OLLAMA_BASE_URL, str),
            model=read("OLLAMA_MODEL", DEFAULT_OLLAMA_MODEL, str),
            num_ctx=read("OLLAMA_NUM_CTX", DEFAULT_OLLAMA_NUM_CTX, int),
            num_predict=read("OLLAMA_NUM_PREDICT", DEFAULT_OLLAMA_NUM_PREDICT, int),
            temperature=read("OLLAMA_TEMPERATURE", DEFAULT_OLLAMA_TEMPERATURE, float),
            connect_timeout=read("OLLAMA_CONNECT_TIMEOUT", DEFAULT_OLLAMA_CONNECT_TIMEOUT, float),
            read_timeout=read("OLLAMA_READ_TIMEOUT", DEFAULT_OLLAMA_READ_TIMEOUT, float),
            write_timeout=read("OLLAMA_WRITE_TIMEOUT", DEFAULT_OLLAMA_WRITE_TIMEOUT, float),
            pool_timeout=read("OLLAMA_POOL_TIMEOUT", DEFAULT_OLLAMA_POOL_TIMEOUT, float),
            health_read_timeout=read(
                "OLLAMA_HEALTH_READ_TIMEOUT", DEFAULT_OLLAMA_HEALTH_READ_TIMEOUT, float
            ),
        )


@dataclass(frozen=True)
class ApiSettings:
    """Settings for the FastAPI layer (inference limiting, request size, CORS)."""

    max_prompt_chars: int = DEFAULT_AI_MAX_PROMPT_CHARS
    max_concurrent_requests: int = DEFAULT_AI_MAX_CONCURRENT_REQUESTS
    queue_wait_timeout: float = DEFAULT_AI_QUEUE_WAIT_TIMEOUT
    cors_origins: tuple[str, ...] = DEFAULT_API_CORS_ORIGINS
    # Token budget (see app.services.token_budget): total context and reserved
    # generation come from OllamaSettings (num_ctx / num_predict).
    safety_margin_tokens: int = DEFAULT_AI_SAFETY_MARGIN_TOKENS
    max_input_tokens: int | None = None  # None = derive from the context window
    structured_max_retries: int = DEFAULT_AI_STRUCTURED_MAX_RETRIES
    allowed_hosts: tuple[str, ...] = DEFAULT_API_ALLOWED_HOSTS
    max_body_bytes: int = DEFAULT_API_MAX_BODY_BYTES

    def __post_init__(self) -> None:
        for name in ("max_prompt_chars", "max_concurrent_requests"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ConfigError(f"{name} must be a positive integer, got {value!r}")
        if (
            isinstance(self.safety_margin_tokens, bool)
            or not isinstance(self.safety_margin_tokens, int)
            or self.safety_margin_tokens < 0
        ):
            raise ConfigError(
                f"safety_margin_tokens must be an integer >= 0, got {self.safety_margin_tokens!r}"
            )
        if self.max_input_tokens is not None and (
            isinstance(self.max_input_tokens, bool)
            or not isinstance(self.max_input_tokens, int)
            or self.max_input_tokens <= 0
        ):
            raise ConfigError(f"max_input_tokens must be a positive integer, got {self.max_input_tokens!r}")
        if (
            isinstance(self.structured_max_retries, bool)
            or not isinstance(self.structured_max_retries, int)
            or not 0 <= self.structured_max_retries <= MAX_STRUCTURED_RETRIES
        ):
            raise ConfigError(
                f"structured_max_retries must be an integer from 0 to {MAX_STRUCTURED_RETRIES}, "
                f"got {self.structured_max_retries!r}"
            )
        if not self.allowed_hosts or any(not h or h != h.lower() for h in self.allowed_hosts):
            raise ConfigError(f"allowed_hosts must be a non-empty list of lowercase hosts, got {self.allowed_hosts!r}")
        if (
            isinstance(self.max_body_bytes, bool)
            or not isinstance(self.max_body_bytes, int)
            or self.max_body_bytes < 1024
        ):
            raise ConfigError(f"max_body_bytes must be an integer >= 1024, got {self.max_body_bytes!r}")
        if not math.isfinite(self.queue_wait_timeout) or self.queue_wait_timeout < 0:
            raise ConfigError(
                f"queue_wait_timeout must be a finite number >= 0, got {self.queue_wait_timeout!r}"
            )
        for origin in self.cors_origins:
            parsed = urlparse(origin)
            if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.path:
                raise ConfigError(
                    f"cors_origins entries must look like 'http://host:port', got {origin!r}"
                )

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> ApiSettings:
        """Build settings from defaults, overridden by ``AI_*`` / ``API_*`` variables."""
        env = os.environ if environ is None else environ

        def read(var: str, default, convert):
            return _read_env(env, var, default, convert)

        return cls(
            max_prompt_chars=read("AI_MAX_PROMPT_CHARS", DEFAULT_AI_MAX_PROMPT_CHARS, int),
            max_concurrent_requests=read(
                "AI_MAX_CONCURRENT_REQUESTS", DEFAULT_AI_MAX_CONCURRENT_REQUESTS, int
            ),
            queue_wait_timeout=read(
                "AI_QUEUE_WAIT_TIMEOUT", DEFAULT_AI_QUEUE_WAIT_TIMEOUT, float
            ),
            cors_origins=read("API_CORS_ORIGINS", DEFAULT_API_CORS_ORIGINS, _origins),
            safety_margin_tokens=read(
                "AI_SAFETY_MARGIN_TOKENS", DEFAULT_AI_SAFETY_MARGIN_TOKENS, int
            ),
            max_input_tokens=read("AI_MAX_INPUT_TOKENS", None, int),
            structured_max_retries=read(
                "AI_STRUCTURED_MAX_RETRIES", DEFAULT_AI_STRUCTURED_MAX_RETRIES, int
            ),
            allowed_hosts=read("API_ALLOWED_HOSTS", DEFAULT_API_ALLOWED_HOSTS, _hosts),
            max_body_bytes=read("API_MAX_BODY_BYTES", DEFAULT_API_MAX_BODY_BYTES, int),
        )


# --------------------------------------------------------------------------- #
# Project analysis (ZIP ingestion, static analysis, context assembly)
# --------------------------------------------------------------------------- #
MIB = 1024 * 1024
KIB = 1024

DEFAULT_PROJECT_MAX_ZIP_BYTES = 10 * MIB
DEFAULT_PROJECT_MAX_UNCOMPRESSED_BYTES = 25 * MIB
# Hard limit for ANY entry (binary assets included); bigger rejects the archive.
DEFAULT_PROJECT_MAX_ENTRY_BYTES = 5 * MIB
# Limit for a supported SOURCE file; bigger files are excluded (with a reason), not fatal.
DEFAULT_PROJECT_MAX_FILE_BYTES = 512 * KIB
DEFAULT_PROJECT_MAX_ENTRIES = 500
DEFAULT_PROJECT_MAX_COMPRESSION_RATIO = 100
# Ratios of tiny payloads say nothing about bombs; only entries/archives at least
# this large (uncompressed) are ratio-checked.
DEFAULT_PROJECT_RATIO_FLOOR_BYTES = 64 * KIB
DEFAULT_PROJECT_MAX_PATH_CHARS = 240
DEFAULT_PROJECT_MAX_PATH_DEPTH = 32
DEFAULT_PROJECT_PROCESSING_TIMEOUT = 30.0
DEFAULT_PROJECT_MAX_CONCURRENT = 2
DEFAULT_PROJECT_QUEUE_WAIT_TIMEOUT = 10.0
# Tokens of the input budget kept free for the future system prompt, task
# instructions, JSON-schema guidance and chat formatting (NOT available for code).
DEFAULT_PROJECT_INSTRUCTION_RESERVE_TOKENS = 700


@dataclass(frozen=True)
class ProjectSettings:
    """Limits and defaults for the project-analysis pipeline (``PROJECT_*`` variables)."""

    max_zip_bytes: int = DEFAULT_PROJECT_MAX_ZIP_BYTES
    max_uncompressed_bytes: int = DEFAULT_PROJECT_MAX_UNCOMPRESSED_BYTES
    max_entry_bytes: int = DEFAULT_PROJECT_MAX_ENTRY_BYTES
    max_file_bytes: int = DEFAULT_PROJECT_MAX_FILE_BYTES
    max_entries: int = DEFAULT_PROJECT_MAX_ENTRIES
    max_compression_ratio: int = DEFAULT_PROJECT_MAX_COMPRESSION_RATIO
    ratio_floor_bytes: int = DEFAULT_PROJECT_RATIO_FLOOR_BYTES
    max_path_chars: int = DEFAULT_PROJECT_MAX_PATH_CHARS
    max_path_depth: int = DEFAULT_PROJECT_MAX_PATH_DEPTH
    processing_timeout: float = DEFAULT_PROJECT_PROCESSING_TIMEOUT
    max_concurrent: int = DEFAULT_PROJECT_MAX_CONCURRENT
    queue_wait_timeout: float = DEFAULT_PROJECT_QUEUE_WAIT_TIMEOUT
    instruction_reserve_tokens: int = DEFAULT_PROJECT_INSTRUCTION_RESERVE_TOKENS

    def __post_init__(self) -> None:
        for name in (
            "max_zip_bytes",
            "max_uncompressed_bytes",
            "max_entry_bytes",
            "max_file_bytes",
            "max_entries",
            "max_compression_ratio",
            "max_path_chars",
            "max_path_depth",
            "max_concurrent",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ConfigError(f"{name} must be a positive integer, got {value!r}")
        for name in ("ratio_floor_bytes", "instruction_reserve_tokens"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ConfigError(f"{name} must be an integer >= 0, got {value!r}")
        if self.max_file_bytes > self.max_entry_bytes:
            raise ConfigError("max_file_bytes must not exceed max_entry_bytes")
        if self.max_entry_bytes > self.max_uncompressed_bytes:
            raise ConfigError("max_entry_bytes must not exceed max_uncompressed_bytes")
        if not math.isfinite(self.processing_timeout) or self.processing_timeout <= 0:
            raise ConfigError(
                f"processing_timeout must be a finite number > 0, got {self.processing_timeout!r}"
            )
        if not math.isfinite(self.queue_wait_timeout) or self.queue_wait_timeout < 0:
            raise ConfigError(
                f"queue_wait_timeout must be a finite number >= 0, got {self.queue_wait_timeout!r}"
            )

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> ProjectSettings:
        env = os.environ if environ is None else environ

        def read(var: str, default, convert):
            return _read_env(env, var, default, convert)

        return cls(
            max_zip_bytes=read("PROJECT_MAX_ZIP_BYTES", DEFAULT_PROJECT_MAX_ZIP_BYTES, int),
            max_uncompressed_bytes=read(
                "PROJECT_MAX_UNCOMPRESSED_BYTES", DEFAULT_PROJECT_MAX_UNCOMPRESSED_BYTES, int
            ),
            max_entry_bytes=read("PROJECT_MAX_ENTRY_BYTES", DEFAULT_PROJECT_MAX_ENTRY_BYTES, int),
            max_file_bytes=read("PROJECT_MAX_FILE_BYTES", DEFAULT_PROJECT_MAX_FILE_BYTES, int),
            max_entries=read("PROJECT_MAX_ENTRIES", DEFAULT_PROJECT_MAX_ENTRIES, int),
            max_compression_ratio=read(
                "PROJECT_MAX_COMPRESSION_RATIO", DEFAULT_PROJECT_MAX_COMPRESSION_RATIO, int
            ),
            ratio_floor_bytes=read(
                "PROJECT_RATIO_FLOOR_BYTES", DEFAULT_PROJECT_RATIO_FLOOR_BYTES, int
            ),
            max_path_chars=read("PROJECT_MAX_PATH_CHARS", DEFAULT_PROJECT_MAX_PATH_CHARS, int),
            max_path_depth=read("PROJECT_MAX_PATH_DEPTH", DEFAULT_PROJECT_MAX_PATH_DEPTH, int),
            processing_timeout=read(
                "PROJECT_PROCESSING_TIMEOUT", DEFAULT_PROJECT_PROCESSING_TIMEOUT, float
            ),
            max_concurrent=read("PROJECT_MAX_CONCURRENT", DEFAULT_PROJECT_MAX_CONCURRENT, int),
            queue_wait_timeout=read(
                "PROJECT_QUEUE_WAIT_TIMEOUT", DEFAULT_PROJECT_QUEUE_WAIT_TIMEOUT, float
            ),
            instruction_reserve_tokens=read(
                "PROJECT_INSTRUCTION_RESERVE_TOKENS", DEFAULT_PROJECT_INSTRUCTION_RESERVE_TOKENS, int
            ),
        )
