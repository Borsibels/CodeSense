"""The deterministic glossary that backs up the beginner-depth wording."""

from __future__ import annotations

import pytest

from app.services.glossary import MAX_GLOSSARY_ENTRIES, TERMS, find_terms


def terms(*texts):
    return [term for term, _ in find_terms(texts)]


def test_known_words_are_found_in_order_of_first_appearance():
    assert terms("The loop goes through a list and returns a count.") == ["loop", "list", "return value"]


def test_inflections_are_recognised():
    assert "iterate" in terms("It iterates over each item") and "iterate" in terms("Iterating is repeating")
    assert "initialize" in terms("The count is initialised to zero") and "initialize" in terms("initializes it")
    assert "variable" in terms("Both variables are reset")


def test_only_whole_words_match():
    assert terms("The classification of randomness in a shipment") == []
    assert terms("A cartoon of an apiary") == []


def test_ordinary_english_is_not_defined_as_code():
    assert terms("None of the files import index.html; for instance, a page") == ["import"]
    assert terms("The scope of the project") == []


def test_the_none_value_is_recognised_when_it_is_code():
    assert terms("the result is None when empty") == ["null"]
    assert terms("it may be undefined") == ["null"]


def test_entries_are_bounded_and_every_meaning_is_unique():
    everything = " ".join(entry.term for entry in TERMS)
    text = "variable function parameter returns loop iterate initialized list string integer boolean class object method module import"
    assert len(find_terms([text])) == MAX_GLOSSARY_ENTRIES
    assert len(find_terms([everything])) <= MAX_GLOSSARY_ENTRIES
    found = find_terms(["a list and an array"])
    assert len(found) == 1  # list and array share one meaning


def test_no_text_means_no_glossary():
    assert find_terms([]) == [] and find_terms(["", "plain words only"]) == []


def test_it_is_deterministic():
    text = "A function with a variable and a loop."
    assert find_terms([text]) == find_terms([text])


def test_definitions_are_short_plain_and_self_contained():
    for entry in TERMS:
        assert 20 <= len(entry.meaning) <= 170, entry.term
        assert entry.meaning[0].isupper() and entry.meaning.endswith((".", ")")), entry.term


@pytest.mark.parametrize(
    "term, must_contain",
    [("modulo", "left over"), ("index", "0"), ("boolean", "true or false"), ("null", "nothing"), ("class", "CSS")],
)
def test_definitions_for_commonly_confused_words_are_accurate(term, must_contain):
    meaning = next(e.meaning for e in TERMS if e.term == term)
    assert must_contain in meaning


# --------------------------------------------------------------------------- #
# Phase 4.5: words with two meanings define BOTH (measured: "element" was attached to 9 of 15 debug
# answers, usually where the model meant a list item, but defined only a web-page building block)
# --------------------------------------------------------------------------- #
def meaning_of(term):
    return next(e.meaning for e in TERMS if e.term == term).lower()


def test_element_covers_a_list_item_and_a_web_page_building_block():
    meaning = meaning_of("element")
    assert "item" in meaning and "list" in meaning
    assert "web page" in meaning and "button" in meaning


def test_attribute_covers_the_python_object_sense_and_the_html_sense():
    meaning = meaning_of("attribute")
    assert "object" in meaning and "html" in meaning and "tag" in meaning


def test_list_and_class_cover_their_web_senses_too():
    assert "web page" in meaning_of("list") and "collection" in meaning_of("list")
    assert "css" in meaning_of("class") and "blueprint" in meaning_of("class")


def test_the_entry_attached_for_a_list_item_answer_is_not_only_about_web_pages():
    shown = dict(find_terms(["Each element is checked."]))
    assert "item in a list" in shown["element"]


def test_list_and_array_are_one_idea_even_though_their_wording_differs():
    assert len(find_terms(["a list and an array"])) == 1
    assert [t for t, _ in find_terms(["an array and a list"])] == ["array"]


def test_every_group_has_at_least_one_term_and_definitions_stay_within_the_length_limits():
    for entry in TERMS:
        assert entry.group
        assert 20 <= len(entry.meaning) <= 170, entry.term
