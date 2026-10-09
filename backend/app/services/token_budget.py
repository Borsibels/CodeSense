"""Token budgeting for a fixed context window, reusable for any prompt assembly.

Why an estimator
----------------
The model is Qwen2.5-Coder served by Ollama. Ollama has no tokenize endpoint
and no tokenizer library or vocabulary file is available offline, so exact
counts cannot be computed before a request. :class:`ConservativeTokenEstimator`
therefore *estimates*, and every result it produces is marked ``exact=False``.
Treat the numbers as estimates, never as counts.

How the estimate is built (see ``backend/README.md`` for the evidence)
---------------------------------------------------------------------
Qwen uses a byte-level BPE vocabulary, so one ASCII character can never cost
more than one token; 1.0 per ASCII character is a hard ceiling. Real code
costs far less (about 0.2-0.35 tokens per character), so costs are tuned from
measurements of Ollama's own ``prompt_eval_count``:

* runs of letters: ceil(length / 2.5) up to 12 letters, 0.6 per letter beyond
  that (long unbroken letter runs are usually not natural words)
* digits: 1.0 each, plus 1.0 for a space/tab directly before a digit
  (Qwen splits numbers into single digits, so that space cannot merge)
* punctuation and symbols: 1.0 each (the ceiling)
* newlines: 1.0, other whitespace: 0.15 (indentation and spaces merge)
* long mixed letter+digit runs (hashes, base64, ids): 1.0 per character
* non-ASCII code points: 1.25, or 2.0 above U+FFFF (emoji measured ~1.25)

Measured against real token counts this over-estimates ordinary code and
prose by roughly 1.5-3.5x, which is the price of being safe. That is evidence,
**not a guarantee**: it is an estimate, and unusual text can still tokenize
denser than any sample measured. The safety margin and the post-hoc
``prompt_eval_count`` audit exist to catch that.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Protocol

from app.config import ConfigError

# Tokens Ollama's chat template adds around a prompt (measured: "a" is 1 token
# raw and 30 templated). It is not added to estimates; it lives inside the
# safety margin, which must stay comfortably larger than this.
MEASURED_TEMPLATE_OVERHEAD_TOKENS = 29


@dataclass(frozen=True)
class TokenCount:
    tokens: int
    # False for anything produced by an estimator. Only a real tokenizer may say True.
    exact: bool


class TokenEstimator(Protocol):
    """Anything that can count tokens. A future exact tokenizer can implement this."""

    def count(self, text: str) -> TokenCount: ...


# Long runs that mix letters and digits look like hashes, base64 or ids and
# tokenize at close to one token per character.
_DENSE_RUN = re.compile(r"[A-Za-z0-9+/=_\-]{16,}")

# Pre-tokenisation units, in priority order: letter runs, a space/tab glued to a
# digit, single digits, newlines, other whitespace runs, any other printable
# ASCII symbol, any non-ASCII code point.
_UNITS = re.compile(r"[A-Za-z]+|[ \t]\d|\d|\n|[ \t\r\f\v]+|[\x21-\x7e]|[^\x00-\x7f]")

_LETTERS_PER_TOKEN = 2.5  # short letter runs (words)
_LONG_RUN_LETTERS = 12  # beyond this a letter run is priced per letter
_W_LONG_LETTER = 0.6
_W_DIGIT = 1.0
_W_SPACE_BEFORE_DIGIT = 2.0
_W_PUNCT = 1.0
_W_NEWLINE = 1.0
_W_SPACE = 0.15
_W_NON_ASCII = 1.25
_W_ASTRAL = 2.0
_W_DENSE_RUN = 1.0


def _segment_cost(segment: str) -> float:
    total = 0.0
    for match in _UNITS.finditer(segment):
        unit = match.group()
        first = unit[0]
        if "a" <= first <= "z" or "A" <= first <= "Z":
            length = len(unit)
            if length <= _LONG_RUN_LETTERS:
                total += math.ceil(length / _LETTERS_PER_TOKEN)
            else:
                total += _W_LONG_LETTER * length
        elif len(unit) == 2 and unit[1].isdigit():
            total += _W_SPACE_BEFORE_DIGIT
        elif first.isdigit() and first.isascii():
            total += _W_DIGIT
        elif first == "\n":
            total += _W_NEWLINE
        elif first in " \t\r\f\v":
            total += _W_SPACE
        elif ord(first) < 128:
            total += _W_PUNCT
        elif ord(first) > 0xFFFF:
            total += _W_ASTRAL
        else:
            total += _W_NON_ASCII
    return total


class ConservativeTokenEstimator:
    """Class-weighted estimate of Qwen2.5 token counts (always ``exact=False``)."""

    def count(self, text: str) -> TokenCount:
        total = 0.0
        position = 0
        for match in _DENSE_RUN.finditer(text):
            run = match.group()
            if not (any(c.isdigit() for c in run) and any(c.isalpha() for c in run)):
                continue  # long but not hash-like (e.g. a long snake_case name): normal rules apply
            total += _segment_cost(text[position : match.start()])
            total += _W_DENSE_RUN * len(run)
            position = match.end()
        total += _segment_cost(text[position:])
        return TokenCount(tokens=math.ceil(total), exact=False)


@dataclass(frozen=True)
class BudgetCheck:
    """Result of checking one text against the input budget."""

    tokens: int
    is_estimate: bool
    limit: int

    @property
    def fits(self) -> bool:
        return self.tokens <= self.limit

    @property
    def remaining(self) -> int:
        return self.limit - self.tokens


class PromptTooLargeError(ValueError):
    """The prompt's token count (estimated) exceeds the input budget."""

    def __init__(self, check: BudgetCheck) -> None:
        kind = "estimated " if check.is_estimate else ""
        super().__init__(f"prompt is {kind}{check.tokens} tokens; the input limit is {check.limit}")
        self.check = check


@dataclass(frozen=True)
class TokenBudget:
    """Splits a context window into prompt space, reserved output and a safety margin.

    ``total_context`` is Ollama's ``num_ctx``; ``reserved_generation`` is
    ``num_predict`` (the output may use that many tokens of the same window).
    ``max_input_tokens`` optionally lowers the input limit below what is
    available; it can never raise it.
    """

    total_context: int
    reserved_generation: int
    safety_margin: int
    max_input_tokens: int | None = None
    estimator: TokenEstimator = field(default_factory=ConservativeTokenEstimator, compare=False)

    def __post_init__(self) -> None:
        for name in ("total_context", "reserved_generation"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ConfigError(f"{name} must be a positive integer, got {value!r}")
        if (
            isinstance(self.safety_margin, bool)
            or not isinstance(self.safety_margin, int)
            or self.safety_margin < 0
        ):
            raise ConfigError(f"safety_margin must be an integer >= 0, got {self.safety_margin!r}")
        if self.reserved_generation + self.safety_margin >= self.total_context:
            raise ConfigError(
                f"reserved_generation ({self.reserved_generation}) + safety_margin "
                f"({self.safety_margin}) must be smaller than total_context ({self.total_context}); "
                "no room would be left for the prompt"
            )
        if self.max_input_tokens is not None:
            if (
                isinstance(self.max_input_tokens, bool)
                or not isinstance(self.max_input_tokens, int)
                or self.max_input_tokens <= 0
            ):
                raise ConfigError(
                    f"max_input_tokens must be a positive integer, got {self.max_input_tokens!r}"
                )
            if self.max_input_tokens > self.available_input_tokens:
                raise ConfigError(
                    f"max_input_tokens ({self.max_input_tokens}) exceeds the "
                    f"{self.available_input_tokens} tokens available after reserving generation "
                    "and the safety margin"
                )

    @property
    def available_input_tokens(self) -> int:
        return self.total_context - self.reserved_generation - self.safety_margin

    @property
    def input_limit(self) -> int:
        """Maximum (estimated) prompt tokens accepted."""
        return self.max_input_tokens or self.available_input_tokens

    def count(self, text: str) -> TokenCount:
        return self.estimator.count(text)

    def check(self, text: str) -> BudgetCheck:
        count = self.estimator.count(text)
        return BudgetCheck(tokens=count.tokens, is_estimate=not count.exact, limit=self.input_limit)

    def ensure_fits(self, text: str) -> BudgetCheck:
        """Return the check, or raise :class:`PromptTooLargeError`. Never truncates."""
        result = self.check(text)
        if not result.fits:
            raise PromptTooLargeError(result)
        return result
