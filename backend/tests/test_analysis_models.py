"""Phase 4 schemas: strict structure, clipped prose, bounded lists, sentinels for "no location"."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from app.services.analysis_models import (
    ELLIPSIS,
    MAX_EVIDENCE_CHARS,
    MAX_FINDINGS,
    MAX_SECTIONS,
    BeginnerExplainDraft,
    CompactBeginnerExplainDraft,
    CompactDebugDraft,
    CompactTechnicalExplainDraft,
    DebugDraft,
    FindingDraft,
    SectionDraft,
    TechnicalExplainDraft,
    clip_prose,
    draft_model,
)


def section(**over):
    base = {"file_path": "a.py", "start_line": 1, "end_line": 2, "title": "Step", "description": "Does a thing."}
    return {**base, **over}


def beginner(**over):
    base = {
        "summary": "It counts.",
        "analogy": "",
        "sections": [section()],
        "role_in_app": "Helps.",
        "concept_name": "Loop",
        "concept_explanation": "Repeats work.",
        "assumptions": [],
    }
    return {**base, **over}


def finding(**over):
    base = {
        "file_path": "a.py",
        "evidence": "3 |     x = 1",
        "start_line": 3,
        "end_line": 3,
        "title": "Odd",
        "category": "logic",
        "severity": "low",
        "confidence": "low",
        "problem": "p",
        "what_could_happen": "w",
        "likely_cause": "c",
        "suggestion": "s",
    }
    return {**base, **over}


# --------------------------------------------------------------------------- #
# Prose clipping
# --------------------------------------------------------------------------- #
def test_prose_is_whitespace_normalised():
    assert clip_prose("  hello \n  world  ", 50) == "hello world"


def test_short_prose_is_untouched():
    assert clip_prose("Counts the even numbers.", 100) == "Counts the even numbers."


def test_overlong_prose_is_clipped_with_an_ellipsis_at_a_word_boundary():
    out = clip_prose("one two three four five six seven eight", 20)
    assert out.endswith(ELLIPSIS)
    assert len(out) <= 20
    assert out == "one two three four" + ELLIPSIS


def test_prose_that_exactly_fills_the_cap_is_treated_as_cut_off_by_the_grammar():
    # Ollama's grammar cuts at exactly maxLength, mid-word.
    out = clip_prose("a" * 10 + " " + "b" * 9, 20)
    assert out.endswith(ELLIPSIS) and len(out) <= 20


def test_a_single_overlong_word_is_hard_cut():
    out = clip_prose("x" * 100, 20)
    assert out == "x" * 19 + ELLIPSIS


def test_prose_a_few_characters_under_the_cap_without_a_sentence_end_gets_an_ellipsis():
    # Measured on the real model: the cut can land 1 character under the cap.
    text = "word " * 47 + "word"  # 239 chars, no final punctuation
    assert len(text) == 239
    assert clip_prose(text, 240).endswith(ELLIPSIS)


def test_prose_near_the_cap_that_ends_a_sentence_is_untouched():
    text = "w" * 233 + " end."
    assert clip_prose(text, 240) == text


def test_non_strings_are_left_for_pydantic_to_reject():
    assert clip_prose(5, 10) == 5
    with pytest.raises(ValidationError):
        BeginnerExplainDraft.model_validate(beginner(summary=5))


def test_overlong_prose_is_clipped_not_rejected_by_the_model():
    draft = BeginnerExplainDraft.model_validate(beginner(summary="word " * 500))
    assert len(draft.summary) <= 400 and draft.summary.endswith(ELLIPSIS)


# --------------------------------------------------------------------------- #
# Structural strictness
# --------------------------------------------------------------------------- #
def test_valid_beginner_draft_round_trips():
    draft = BeginnerExplainDraft.model_validate(beginner())
    assert draft.sections[0].file_path == "a.py" and draft.concept_name == "Loop"


@pytest.mark.parametrize("missing", ["summary", "analogy", "sections", "role_in_app", "concept_name", "concept_explanation", "assumptions"])
def test_missing_beginner_field_is_rejected(missing):
    data = beginner()
    del data[missing]
    with pytest.raises(ValidationError):
        BeginnerExplainDraft.model_validate(data)


def test_unknown_fields_are_rejected():
    with pytest.raises(ValidationError):
        BeginnerExplainDraft.model_validate(beginner(confirmed_bug=True))


def test_too_many_sections_are_rejected_not_silently_dropped():
    with pytest.raises(ValidationError):
        TechnicalExplainDraft.model_validate({"summary": "s", "sections": [section()] * (MAX_SECTIONS + 1), "assumptions": []})


def test_too_many_findings_are_rejected():
    with pytest.raises(ValidationError):
        DebugDraft.model_validate({"summary": "s", "findings": [finding()] * (MAX_FINDINGS + 1)})


def test_empty_findings_list_is_valid_because_clean_code_is_a_good_answer():
    assert DebugDraft.model_validate({"summary": "Nothing wrong.", "findings": []}).findings == []


@pytest.mark.parametrize("field, value", [("category", "bug"), ("severity", "critical"), ("confidence", "certain"), ("severity", "HIGH")])
def test_invalid_enum_values_are_rejected(field, value):
    with pytest.raises(ValidationError):
        FindingDraft.model_validate(finding(**{field: value}))


@pytest.mark.parametrize("value", [-1, 1_000_001, 1.5, None, "seven"])
def test_line_numbers_must_be_bounded_integers(value):
    with pytest.raises(ValidationError):
        SectionDraft.model_validate(section(start_line=value))


def test_zero_and_empty_string_are_the_no_location_sentinels():
    draft = SectionDraft.model_validate(section(file_path="", start_line=0, end_line=0))
    assert (draft.file_path, draft.start_line, draft.end_line) == ("", 0, 0)


def test_over_long_file_path_is_rejected():
    with pytest.raises(ValidationError):
        SectionDraft.model_validate(section(file_path="a" * 301))


def test_assumptions_are_bounded():
    with pytest.raises(ValidationError):
        BeginnerExplainDraft.model_validate(beginner(assumptions=["a", "b", "c"]))


# --------------------------------------------------------------------------- #
# Evidence quotes are never clipped
# --------------------------------------------------------------------------- #
def test_overlong_evidence_is_treated_as_absent_not_clipped():
    draft = FindingDraft.model_validate(finding(evidence="x" * (MAX_EVIDENCE_CHARS + 5)))
    assert draft.evidence == ""


def test_evidence_that_reaches_the_cap_counts_as_a_cut_off_runaway_copy():
    assert FindingDraft.model_validate(finding(evidence="y" * MAX_EVIDENCE_CHARS)).evidence == ""


def test_normal_evidence_is_preserved_exactly():
    assert FindingDraft.model_validate(finding(evidence="12 |     total += prices[i]")).evidence == "12 |     total += prices[i]"


# --------------------------------------------------------------------------- #
# Model selection and the JSON schema sent to Ollama
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "intent, depth, compact, expected",
    [
        ("explain", "beginner", False, BeginnerExplainDraft),
        ("overview", "beginner", False, BeginnerExplainDraft),
        ("explain", "intermediate", False, TechnicalExplainDraft),
        ("explain", "advanced", False, TechnicalExplainDraft),
        ("overview", "advanced", False, TechnicalExplainDraft),
        ("debug", "beginner", False, DebugDraft),
        ("debug", "advanced", False, DebugDraft),
        ("explain", "beginner", True, CompactBeginnerExplainDraft),
        ("explain", "advanced", True, CompactTechnicalExplainDraft),
        ("debug", "intermediate", True, CompactDebugDraft),
    ],
)
def test_draft_model_selection(intent, depth, compact, expected):
    assert draft_model(intent, depth, compact=compact) is expected


@pytest.mark.parametrize("model", [BeginnerExplainDraft, TechnicalExplainDraft, DebugDraft, CompactBeginnerExplainDraft, CompactTechnicalExplainDraft, CompactDebugDraft])
def test_schema_forbids_extra_properties_and_carries_the_bounds(model):
    text = json.dumps(model.model_json_schema())
    assert '"additionalProperties": false' in text
    assert "maxLength" in text and "maxItems" in text


def test_debug_findings_put_the_quote_before_the_line_numbers():
    # Measured: the model copies a line (with its margin number) and then repeats the number;
    # asked for the numbers first it recalls them wrongly (quote matched 4/20 vs 12/16).
    order = list(FindingDraft.model_fields)
    assert order.index("file_path") < order.index("evidence") < order.index("start_line") < order.index("end_line")
    assert order.index("end_line") < order.index("title")


def test_compact_models_are_smaller_than_standard_ones():
    assert len(json.dumps(CompactDebugDraft.model_json_schema())) <= len(json.dumps(DebugDraft.model_json_schema())) + 200
    assert CompactDebugDraft.model_fields["findings"].metadata[0].max_length == 1
    assert CompactBeginnerExplainDraft.model_fields["sections"].metadata[0].max_length == 2
