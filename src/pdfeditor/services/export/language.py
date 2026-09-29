"""Guessing text language for exports, so Word proofs the text in the right language.

Without a language tag Word checks everything in the user's editing language, which underlines
every word of, say, a Bulgarian document as misspelled English. Only what the letters give away
is guessed: a script, and for Cyrillic the language its letters point to. Latin-script text is
left to the document's /Lang or to Word's default.
"""

from __future__ import annotations

from collections import Counter

CYRILLIC = "cyrl"
GREEK = "grek"
LATIN = "latn"

# letters found in only some Cyrillic alphabets, most telling first
_CYRILLIC_MARKERS = (
    ("ў", "be-BY"),
    ("ґєії", "uk-UA"),
    ("ѓќѕ", "mk-MK"),
    ("ђћљњџј", "sr-Cyrl-RS"),
    ("ыэё", "ru-RU"),
)
# Bulgarian uses ъ as a common vowel (about 2% of letters); Russian only as a rare sign
_BULGARIAN_HARD_SIGN_SHARE = 0.005
_MARKER_SHARE = 0.001
_CYRILLIC_LANGS = {"bg", "ru", "uk", "be", "mk", "kk", "ky", "mn", "tg", "tt", "ba", "cv"}


def script_of(ch: str) -> str:
    code = ord(ch)
    if 0x0400 <= code <= 0x052F:
        return CYRILLIC
    if 0x0370 <= code <= 0x03FF or 0x1F00 <= code <= 0x1FFF:
        return GREEK
    if (ch.isascii() and ch.isalpha()) or 0x00C0 <= code <= 0x024F:
        return LATIN
    return ""


def dominant_script(text: str) -> str:
    """The script most letters of ``text`` are written in ("" when there are no letters)."""
    counts = Counter(s for s in map(script_of, text) if s)
    return counts.most_common(1)[0][0] if counts else ""


def lang_script(tag: str) -> str:
    """The script a BCP 47 language tag is written in (Latin unless known otherwise)."""
    parts = tag.replace("_", "-").split("-")
    if "Latn" in parts:
        return LATIN
    if "Cyrl" in parts or parts[0].lower() in _CYRILLIC_LANGS:
        return CYRILLIC
    if parts[0].lower() == "el":
        return GREEK
    return LATIN


def guess_cyrillic(text: str) -> str:
    counts = Counter(text.lower())
    total = sum(n for c, n in counts.items() if c.isalpha())
    if not total:
        return "ru-RU"
    # a stray quoted word shouldn't decide: the marker letters must be a regular sight
    enough = max(2, total * _MARKER_SHARE)
    for markers, tag in _CYRILLIC_MARKERS:
        if sum(counts[c] for c in markers) >= enough:
            return tag
    return "bg-BG" if counts["ъ"] / total >= _BULGARIAN_HARD_SIGN_SHARE else "ru-RU"


class Languages:
    """The language of a document's text and of runs in another script.

    ``hint`` is the document's declared language (PDF /Lang); ``text`` is all of its text.
    """

    def __init__(self, text: str, hint: str = "") -> None:
        cyrillic = "".join(c for c in text if script_of(c) == CYRILLIC)
        self._by_script = {
            CYRILLIC: guess_cyrillic(cyrillic) if cyrillic else "ru-RU",
            GREEK: "el-GR",
            LATIN: "en-US",
        }
        if hint:
            self._by_script[lang_script(hint)] = hint
            self.default = hint
        else:
            script = dominant_script(text)
            # a Latin document keeps Word's own default language: the letters don't tell which
            self.default = self._by_script[script] if script in (CYRILLIC, GREEK) else ""
        self._default_script = lang_script(self.default) if self.default else LATIN

    def for_run(self, text: str) -> str:
        """The language to tag a run with, or "" when the document default fits."""
        script = dominant_script(text)
        if not script or script == self._default_script:
            return ""
        return self._by_script[script]
