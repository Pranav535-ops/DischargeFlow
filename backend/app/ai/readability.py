"""Deterministic readability of patient-facing text (ADR-002 item 18).

Flesch-Kincaid grade level: 0.39 * (words / sentences) + 11.8 * (syllables / words) - 15.59. Syllables are counted
with a vowel-group heuristic, which is close enough to rank drafts and gate evals, and identical on every run.

The formula is calibrated for English only. For any other language `grade_for` returns None rather than a number
that would look meaningful and is not.
"""
import re

WORD_RE = re.compile(r"[A-Za-z]+(?:'[A-Za-z]+)?")
# A sentence ends at terminal punctuation, a line break (list items and headings) or a colon introducing a list.
SENTENCE_SPLIT_RE = re.compile(r"[.!?]+|\n+|:\s")
VOWEL_GROUP_RE = re.compile(r"[aeiouy]+")


def syllables(word: str) -> int:
    w = word.lower()
    if len(w) <= 3:
        return 1
    w = re.sub(r"(?:[^laeiouy]es|[^laeiouy]ed|[^laeiouy]e)$", "", w)  # silent final e / -es / -ed
    w = re.sub(r"^y", "", w)
    return max(len(VOWEL_GROUP_RE.findall(w)), 1)


def flesch_kincaid_grade(text: str) -> float | None:
    """Grade level of `text`, rounded to one decimal; None when there are no words."""
    sentences = [s for s in SENTENCE_SPLIT_RE.split(text or "") if WORD_RE.search(s)]
    words = WORD_RE.findall(text or "")
    if not words:
        return None
    grade = 0.39 * (len(words) / len(sentences)) + 11.8 * (sum(map(syllables, words)) / len(words)) - 15.59
    return round(max(grade, 0.0), 1)


def is_english(language: str) -> bool:
    return language.lower().split("-", 1)[0] == "en"


def grade_for(text: str, language: str) -> float | None:
    """Flesch-Kincaid grade for English text; None for other languages (no comparable deterministic measure here)."""
    return flesch_kincaid_grade(text) if is_english(language) else None
