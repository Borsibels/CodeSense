"""Plain-language meanings of common programming words, for beginner-depth answers.

Why this exists: measured on the real model, a 3B network follows the beginner *structure* (what
it does, step by step, role, one concept) but still leaves about 7 technical words per 100
undefined even when told to explain each one. Instead of trusting the model to define terms, the
backend attaches its own hand-written definitions for the terms that actually appear in the AI's
text. Deterministic: same text, same glossary. The AI contributes nothing to it, so it cannot
invent a wrong definition; the definitions are deliberately short and language-neutral
(Python / JavaScript / HTML / CSS).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

MAX_GLOSSARY_ENTRIES = 8


@dataclass(frozen=True)
class GlossaryTerm:
    term: str
    pattern: re.Pattern[str]
    meaning: str


def _t(term: str, pattern: str, meaning: str) -> GlossaryTerm:
    return GlossaryTerm(term, re.compile(rf"\b(?:{pattern})\b", re.IGNORECASE), meaning)


TERMS: tuple[GlossaryTerm, ...] = (
    _t("variable", r"variables?", "A named box in the program that holds a value, which can change later."),
    _t("function", r"functions?", "A named, reusable set of steps that does one job, often handing back a result."),
    _t("parameter", r"parameters?|arguments?", "A piece of information you give to a function so it can do its job."),
    _t("return value", r"returns?|returned|returning|return values?|return statements?", "The result a function hands back to the code that used it."),
    _t("loop", r"loops?|looping|loops through", "A way to repeat the same steps again and again, for example once for each item."),
    _t("iterate", r"iterat(?:e|es|ed|ing|ion|ions)", "To go through items one at a time, in order."),
    _t("initialize", r"initiali[sz](?:e|es|ed|ing|ation)", "To give something its first starting value."),
    _t("list", r"lists?", "An ordered collection of items, such as several numbers or names kept together."),
    _t("array", r"arrays?", "An ordered collection of items, such as several numbers or names kept together."),
    _t("dictionary", r"dictionar(?:y|ies)|dicts?", "A collection where each value is stored under a name (a key) so you can look it up by that name."),
    _t("string", r"strings?", "A piece of text, such as a word or a sentence."),
    _t("integer", r"integers?", "A whole number, like 3 or -12, with no decimal part."),
    _t("boolean", r"booleans?", "A value that is only ever true or false."),
    _t("class", r"class(?:es)?", "A blueprint that describes what a kind of thing holds and can do. (In CSS, a class is instead a label used to style elements.)"),
    _t("object", r"objects?", "A bundle of related information and actions, built from a blueprint (a class)."),
    _t("method", r"methods?", "A function that belongs to an object or class."),
    _t("module", r"modules?", "A separate file of code that other files can borrow from."),
    _t("import", r"imports?|imported|importing", "Bringing in code from another file or library so it can be used."),
    _t("library", r"librar(?:y|ies)", "Ready-made code written by other people that you can reuse."),
    _t("condition", r"conditions?|conditionals?", "A yes-or-no question the program asks to decide what to do next."),
    _t("operator", r"operators?", "A symbol that does something with values, such as + to add or == to compare."),
    _t("modulo", r"modulo|remainder", "What is left over after dividing one number by another. Dividing by 2 and checking for 0 left over tells you if a number is even."),
    _t("index", r"index(?:es)?(?!\.html?)|indices", "The position number of an item in a list. Counting usually starts at 0, not 1."),
    _t("exception", r"exceptions?", "An error that stops the normal flow of a program unless the code deals with it."),
    _t("null", r"null|undefined|(?-i:None)(?!\s+of)", "A special value meaning 'nothing is here'."),
    _t("callback", r"callbacks?", "A function handed over to be run later, once something else has finished or happened."),
    _t("event", r"events?|event listeners?", "Something that happens, like a click or a key press, that the code can react to."),
    _t("element", r"elements?", "One building block of a web page, such as a heading, a button or a paragraph."),
    _t("DOM", r"dom|document object model", "The live, in-memory version of a web page that JavaScript can read and change."),
    _t("API", r"apis?", "A set of rules that lets one piece of software ask another to do something."),
    _t("syntax", r"syntax", "The exact way code must be written for the computer to understand it."),
    _t("constant", r"constants?", "A named value that is set once and is not meant to change."),
    _t("attribute", r"attributes?", "An extra detail attached to something, such as the link address of a web page link."),
    _t("JSON", r"json", "A simple text format for storing and sending data."),
    _t("recursion", r"recursion|recursive", "When a function solves a problem by calling itself on a smaller version of it."),
    _t("instance", r"(?<!for )instances?|instantiate[sd]?", "One actual object made from a class (the blueprint)."),
    _t("parse", r"pars(?:e|es|ed|ing)|parser", "To read text and work out its structure or meaning."),
    _t("selector", r"selectors?", "In CSS, a pattern that picks which elements a style applies to."),
    _t("framework", r"frameworks?", "A ready-made structure that you build your own code on top of."),
)


def find_terms(texts: Iterable[str], limit: int = MAX_GLOSSARY_ENTRIES) -> list[tuple[str, str]]:
    """``(term, meaning)`` for each known word found in ``texts``, in order of first appearance."""
    text = "\n".join(t for t in texts if t)
    found: list[tuple[int, GlossaryTerm]] = []
    seen_meanings: set[str] = set()
    for entry in TERMS:
        match = entry.pattern.search(text)
        if match:
            found.append((match.start(), entry))
    found.sort(key=lambda item: item[0])
    out: list[tuple[str, str]] = []
    for _, entry in found:
        if entry.meaning in seen_meanings:  # list/array share one meaning
            continue
        seen_meanings.add(entry.meaning)
        out.append((entry.term, entry.meaning))
        if len(out) == limit:
            break
    return out
