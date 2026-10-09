"""Token budgeting: estimator behaviour, evidence against real token counts, budget arithmetic."""

from __future__ import annotations

import json
import random
import string
from pathlib import Path

import pytest

from app.config import DEFAULT_AI_SAFETY_MARGIN_TOKENS, ConfigError
from app.services.token_budget import (
    MEASURED_TEMPLATE_OVERHEAD_TOKENS,
    BudgetCheck,
    ConservativeTokenEstimator,
    PromptTooLargeError,
    TokenBudget,
    TokenCount,
)

estimator = ConservativeTokenEstimator()
SAMPLES = json.loads((Path(__file__).parent / "data" / "token_samples.json").read_text(encoding="utf-8"))["samples"]


class FixedEstimator:
    """Returns a preset count, so budget arithmetic can be tested exactly."""

    def __init__(self, tokens: int) -> None:
        self.tokens = tokens

    def count(self, text: str) -> TokenCount:
        return TokenCount(tokens=self.tokens, exact=False)


# --------------------------------------------------------------------------- #
# Estimator: honesty and basic behaviour
# --------------------------------------------------------------------------- #
def test_estimates_are_always_labelled_as_estimates():
    for text in ("", "hello", "def f(): pass", "日本語", "🚀" * 10):
        assert estimator.count(text).exact is False


def test_budget_checks_report_is_estimate():
    budget = TokenBudget(4096, 1024, 256)

    assert budget.check("print('hi')").is_estimate is True


def test_empty_text_is_zero_tokens():
    assert estimator.count("").tokens == 0


def test_longer_text_never_estimates_fewer_tokens():
    text = "def calculate_total(items):\n    return sum(i.price for i in items)\n"
    counts = [estimator.count(text * n).tokens for n in (1, 2, 4, 8)]

    assert counts == sorted(counts) and counts[0] < counts[-1]


@pytest.mark.parametrize(
    "text",
    [
        "plain ascii prose with several ordinary english words in it",
        "x = [1, 2, 3]  # numbers 42 and 7",
        "日本語のテキストと中文文本 русский текст العربية",
        "emoji 🚀🔥🐛 and ZWJ 👩‍💻",
        "{" * 50,
        "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8",
    ],
)
def test_estimate_never_exceeds_the_utf8_byte_ceiling(text):
    # Byte-level BPE can never emit more tokens than bytes; the estimator must respect that.
    assert estimator.count(text).tokens <= len(text.encode("utf-8"))


def test_ceiling_holds_for_random_strings():
    rng = random.Random(7)
    alphabet = string.printable + "éüñ日本語русский🚀🔥‍"
    for _ in range(200):
        text = "".join(rng.choices(alphabet, k=rng.randint(1, 400)))
        assert estimator.count(text).tokens <= len(text.encode("utf-8")) + 1


# --------------------------------------------------------------------------- #
# Estimator: the content classes the task calls out
# --------------------------------------------------------------------------- #
def test_ascii_prose_is_far_cheaper_than_its_byte_count():
    text = "The quick brown fox jumps over the lazy dog. " * 20

    assert estimator.count(text).tokens < len(text) * 0.6


def test_digits_cost_one_token_each():
    assert estimator.count("1234567890" * 10).tokens >= 100


def test_space_before_digit_is_priced_separately():
    # Qwen splits numbers into single digits, so " 5" cannot merge into one token.
    assert estimator.count(" 1 2 3 4 5 6 7 8 9").tokens >= 18


def test_non_ascii_costs_more_than_ascii_of_the_same_length():
    ascii_text, cjk_text, emoji_text = "a" * 40, "日" * 40, "🚀" * 40

    assert estimator.count(ascii_text).tokens < estimator.count(cjk_text).tokens
    assert estimator.count(cjk_text).tokens < estimator.count(emoji_text).tokens


def test_dense_code_costs_about_a_token_per_character():
    dense = "".join(random.Random(3).choices("{}[]()<>;:,.=+-*/&|!?%^~#@$", k=300))

    assert estimator.count(dense).tokens >= 300


def test_hash_like_runs_are_priced_per_character_but_long_names_are_not():
    sha = "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"  # 64 chars, letters+digits
    snake = "calculate_user_account_balance_manager_factory_handler"  # long, but real words

    assert estimator.count(sha).tokens >= len(sha)
    assert estimator.count(snake).tokens < len(snake) * 0.8


def test_long_identifiers_stay_bounded():
    camel = "CalculateUserAccountBalanceManagerFactoryHandlerConfigRequest" * 3

    assert estimator.count(camel).tokens <= len(camel)


def test_whitespace_runs_do_not_explode_the_estimate():
    indented = ("    " * 6 + "x = 1\n") * 50

    assert estimator.count(indented).tokens < len(indented) * 0.6


# --------------------------------------------------------------------------- #
# Estimator vs REAL token counts (measured from Ollama, see tests/data)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("sample", SAMPLES, ids=[s["category"] for s in SAMPLES])
def test_estimate_is_not_below_the_real_token_count(sample):
    estimated = estimator.count(sample["text"]).tokens

    assert estimated >= sample["real_tokens"], (
        f"{sample['category']}: estimated {estimated} < real {sample['real_tokens']}"
    )


def test_estimator_is_not_uselessly_pessimistic_on_ordinary_code():
    # Evidence the estimate is usable, not just safe: ordinary code is covered < ~3x.
    ordinary = [s for s in SAMPLES if s["category"] in {"python", "js", "ts/jsx", "css", "html", "prose"}]
    assert ordinary
    for s in ordinary:
        ratio = estimator.count(s["text"]).tokens / s["real_tokens"]
        assert ratio < 3.0, (s["category"], ratio)


# --------------------------------------------------------------------------- #
# Budget arithmetic and invariants
# --------------------------------------------------------------------------- #
def test_default_budget_matches_the_specified_split():
    budget = TokenBudget(4096, 1024, DEFAULT_AI_SAFETY_MARGIN_TOKENS)

    assert DEFAULT_AI_SAFETY_MARGIN_TOKENS == 256
    assert budget.available_input_tokens == 2816
    assert budget.input_limit == 2816


def test_template_overhead_fits_inside_the_default_safety_margin():
    assert MEASURED_TEMPLATE_OVERHEAD_TOKENS * 4 < DEFAULT_AI_SAFETY_MARGIN_TOKENS


def test_text_exactly_at_the_limit_fits_and_one_over_does_not():
    at_limit = TokenBudget(4096, 1024, 256, estimator=FixedEstimator(2816))
    over = TokenBudget(4096, 1024, 256, estimator=FixedEstimator(2817))

    assert at_limit.check("x").fits is True
    assert at_limit.check("x").remaining == 0
    assert over.check("x").fits is False
    assert over.check("x").remaining == -1


def test_ensure_fits_raises_with_the_numbers_and_never_truncates():
    budget = TokenBudget(4096, 1024, 256, estimator=FixedEstimator(3000))
    prompt = "x" * 10

    with pytest.raises(PromptTooLargeError) as excinfo:
        budget.ensure_fits(prompt)

    assert excinfo.value.check == BudgetCheck(tokens=3000, is_estimate=True, limit=2816)
    assert "estimated 3000" in str(excinfo.value)
    assert prompt == "x" * 10  # input untouched; callers get a rejection, not a shortened prompt


def test_ensure_fits_returns_the_check_when_it_fits():
    budget = TokenBudget(4096, 1024, 256, estimator=FixedEstimator(100))

    assert budget.ensure_fits("x").remaining == 2716


def test_max_input_tokens_can_lower_the_limit():
    budget = TokenBudget(4096, 1024, 256, max_input_tokens=1000)

    assert budget.input_limit == 1000
    assert budget.available_input_tokens == 2816


def test_max_input_tokens_cannot_exceed_what_is_available():
    TokenBudget(4096, 1024, 256, max_input_tokens=2816)  # exactly the available amount is fine
    with pytest.raises(ConfigError, match="exceeds"):
        TokenBudget(4096, 1024, 256, max_input_tokens=2817)


@pytest.mark.parametrize(
    "args",
    [
        (4096, 4096, 0),  # generation takes the whole window
        (4096, 3840, 256),  # generation + margin == total: no prompt room
        (4096, 4000, 256),  # generation + margin > total
        (4096, 1024, 4096),  # margin alone fills the window
        (0, 1, 0),
        (4096, 0, 0),
        (4096, 1024, -1),
        (True, 1024, 256),
    ],
)
def test_invalid_budgets_are_rejected(args):
    with pytest.raises(ConfigError):
        TokenBudget(*args)


def test_non_positive_max_input_tokens_rejected():
    for bad in (0, -5):
        with pytest.raises(ConfigError):
            TokenBudget(4096, 1024, 256, max_input_tokens=bad)


def test_zero_margin_is_allowed_when_there_is_room():
    assert TokenBudget(4096, 1024, 0).available_input_tokens == 3072


def test_budget_scales_with_other_context_sizes():
    assert TokenBudget(8192, 2048, 512).available_input_tokens == 5632


def test_budget_is_reusable_for_assembling_context_from_parts():
    # The Phase 3 use: keep adding pieces while they fit.
    budget = TokenBudget(4096, 1024, 256)
    piece = "def f(x):\n    return x * 2\n"
    used, kept = 0, 0
    for _ in range(10_000):
        cost = budget.count(piece).tokens
        if used + cost > budget.input_limit:
            break
        used += cost
        kept += 1

    assert 0 < kept < 10_000
    assert used <= budget.input_limit
