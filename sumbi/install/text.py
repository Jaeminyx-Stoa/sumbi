"""Public script labels and a versioned, tokenizer-free cost heuristic."""

from collections import Counter
import math
import unicodedata

ESTIMATE_RULE = ("script-aware-v1: ceil(other_characters / 4) + "
    "Hangul/Han/Hiragana/Katakana characters")
CJK_SCRIPTS = {"Hangul", "Han", "Hiragana", "Katakana"}


def script(character: str) -> str | None:
    """Classify letters by Unicode names; never return input text as a label."""
    name = unicodedata.name(character, "")
    if name.startswith(("CJK ", "IDEOGRAPHIC ")):
        return "Han"
    for category in ("HANGUL", "HIRAGANA", "KATAKANA", "LATIN"):
        if category in name.replace("-", " ").split():
            return category.title()
    return name.split()[0].title() if name else "Other"


def scripts(text: str) -> Counter:
    return Counter(category for character in text if unicodedata.category(character).startswith("L")
        and (category := script(character)))


def estimate_tokens(text: str) -> int:
    """Upper-leaning heuristic, not a tokenizer or a guaranteed upper bound."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    cjk = sum(script(character) in CJK_SCRIPTS for character in text)
    return math.ceil((len(text) - cjk) / 4) + cjk
