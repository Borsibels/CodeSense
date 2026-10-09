import pytest

from app.config import ApiSettings, ConfigError, OllamaSettings


def test_defaults_match_phase_1_spec():
    s = OllamaSettings()
    assert s.base_url == "http://127.0.0.1:11434"
    assert s.model == "qwen2.5-coder:3b"
    assert s.num_ctx == 4096
    assert s.num_predict == 1024
    assert s.temperature == 0.2
    assert s.connect_timeout == 5.0
    assert s.read_timeout == 120.0
    assert s.write_timeout == 10.0
    assert s.pool_timeout == 5.0


def test_from_env_with_empty_environment_uses_defaults():
    assert OllamaSettings.from_env({}) == OllamaSettings()


def test_from_env_overrides():
    s = OllamaSettings.from_env(
        {
            "OLLAMA_BASE_URL": "http://localhost:9999/",
            "OLLAMA_MODEL": "other:1b",
            "OLLAMA_NUM_CTX": "2048",
            "OLLAMA_NUM_PREDICT": "256",
            "OLLAMA_TEMPERATURE": "0",
            "OLLAMA_CONNECT_TIMEOUT": "1.5",
            "OLLAMA_READ_TIMEOUT": "300",
            "OLLAMA_WRITE_TIMEOUT": "2",
            "OLLAMA_POOL_TIMEOUT": "3",
            "OLLAMA_HEALTH_READ_TIMEOUT": "4",
        }
    )
    assert s.base_url == "http://localhost:9999"  # trailing slash stripped
    assert s.model == "other:1b"
    assert (s.num_ctx, s.num_predict, s.temperature) == (2048, 256, 0.0)
    assert (s.connect_timeout, s.read_timeout, s.write_timeout, s.pool_timeout) == (
        1.5,
        300.0,
        2.0,
        3.0,
    )
    assert s.health_read_timeout == 4.0


def test_blank_env_values_fall_back_to_defaults():
    assert OllamaSettings.from_env({"OLLAMA_MODEL": "  ", "OLLAMA_NUM_CTX": ""}) == OllamaSettings()


@pytest.mark.parametrize(
    "env, variable",
    [
        ({"OLLAMA_NUM_CTX": "lots"}, "OLLAMA_NUM_CTX"),
        ({"OLLAMA_NUM_CTX": "4096.5"}, "OLLAMA_NUM_CTX"),
        ({"OLLAMA_TEMPERATURE": "warm"}, "OLLAMA_TEMPERATURE"),
        ({"OLLAMA_READ_TIMEOUT": "soon"}, "OLLAMA_READ_TIMEOUT"),
    ],
)
def test_unparseable_env_value_names_the_variable(env, variable):
    with pytest.raises(ConfigError, match=variable):
        OllamaSettings.from_env(env)


@pytest.mark.parametrize(
    "overrides",
    [
        {"base_url": "127.0.0.1:11434"},  # no scheme
        {"base_url": "ftp://127.0.0.1"},
        {"model": "  "},
        {"num_ctx": 0},
        {"num_predict": -1},
        {"temperature": -0.1},
        {"temperature": float("nan")},
        {"read_timeout": 0},
        {"connect_timeout": float("inf")},
    ],
)
def test_invalid_values_rejected(overrides):
    with pytest.raises(ConfigError):
        OllamaSettings(**overrides)


# --------------------------------------------------------------------------- #
# ApiSettings
# --------------------------------------------------------------------------- #
def test_api_defaults_are_conservative_for_a_4gb_gpu():
    s = ApiSettings()
    assert s.max_concurrent_requests == 1
    assert s.queue_wait_timeout == 30.0
    assert s.max_prompt_chars == 6000
    assert "http://127.0.0.1:5173" in s.cors_origins


def test_api_from_env_overrides():
    s = ApiSettings.from_env(
        {
            "AI_MAX_PROMPT_CHARS": "1000",
            "AI_MAX_CONCURRENT_REQUESTS": "2",
            "AI_QUEUE_WAIT_TIMEOUT": "0",
            "API_CORS_ORIGINS": "http://localhost:3000/, http://127.0.0.1:3000",
        }
    )
    assert (s.max_prompt_chars, s.max_concurrent_requests, s.queue_wait_timeout) == (1000, 2, 0.0)
    assert s.cors_origins == ("http://localhost:3000", "http://127.0.0.1:3000")


def test_api_from_env_empty_uses_defaults_and_empty_origins_disable_cors():
    assert ApiSettings.from_env({}) == ApiSettings()
    assert ApiSettings.from_env({"API_CORS_ORIGINS": " "}) == ApiSettings()


@pytest.mark.parametrize(
    "env, variable",
    [
        ({"AI_MAX_PROMPT_CHARS": "big"}, "AI_MAX_PROMPT_CHARS"),
        ({"AI_MAX_CONCURRENT_REQUESTS": "1.5"}, "AI_MAX_CONCURRENT_REQUESTS"),
        ({"AI_QUEUE_WAIT_TIMEOUT": "soon"}, "AI_QUEUE_WAIT_TIMEOUT"),
    ],
)
def test_api_unparseable_env_names_the_variable(env, variable):
    with pytest.raises(ConfigError, match=variable):
        ApiSettings.from_env(env)


@pytest.mark.parametrize(
    "overrides",
    [
        {"max_prompt_chars": 0},
        {"max_concurrent_requests": 0},
        {"queue_wait_timeout": -1},
        {"queue_wait_timeout": float("nan")},
        {"cors_origins": ("localhost:5173",)},
        {"cors_origins": ("http://localhost:5173/app",)},
    ],
)
def test_api_invalid_values_rejected(overrides):
    with pytest.raises(ConfigError):
        ApiSettings(**overrides)


def test_api_defaults_for_budget_retries_hosts_and_body_limit():
    s = ApiSettings()
    assert s.safety_margin_tokens == 256
    assert s.max_input_tokens is None
    assert s.structured_max_retries == 1
    assert s.allowed_hosts == ("127.0.0.1", "localhost", "[::1]")
    assert s.max_body_bytes == 131072


def test_api_new_settings_from_environment():
    s = ApiSettings.from_env(
        {
            "AI_SAFETY_MARGIN_TOKENS": "128",
            "AI_MAX_INPUT_TOKENS": "2000",
            "AI_STRUCTURED_MAX_RETRIES": "0",
            "API_ALLOWED_HOSTS": "localhost",
            "API_MAX_BODY_BYTES": "4096",
        }
    )
    assert (s.safety_margin_tokens, s.max_input_tokens, s.structured_max_retries) == (128, 2000, 0)
    assert (s.allowed_hosts, s.max_body_bytes) == (("localhost",), 4096)


@pytest.mark.parametrize(
    "env, variable",
    [
        ({"AI_SAFETY_MARGIN_TOKENS": "lots"}, "AI_SAFETY_MARGIN_TOKENS"),
        ({"AI_MAX_INPUT_TOKENS": "2k"}, "AI_MAX_INPUT_TOKENS"),
        ({"AI_STRUCTURED_MAX_RETRIES": "many"}, "AI_STRUCTURED_MAX_RETRIES"),
        ({"API_MAX_BODY_BYTES": "big"}, "API_MAX_BODY_BYTES"),
    ],
)
def test_api_new_settings_unparseable_env_names_the_variable(env, variable):
    with pytest.raises(ConfigError, match=variable):
        ApiSettings.from_env(env)


@pytest.mark.parametrize(
    "overrides",
    [
        {"safety_margin_tokens": -1},
        {"max_input_tokens": 0},
        {"structured_max_retries": -1},
        {"structured_max_retries": 6},
        {"allowed_hosts": ("LocalHost",)},
        {"max_body_bytes": 100},
    ],
)
def test_api_new_settings_invalid_values_rejected(overrides):
    with pytest.raises(ConfigError):
        ApiSettings(**overrides)
