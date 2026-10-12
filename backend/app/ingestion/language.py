"""Script-based language detection for Japanese, English and mixed text.

The project only needs to tell Japanese from English (and their mixture), so detection
counts characters by Unicode script instead of using a statistical language-ID model:

* Japanese: hiragana, katakana, CJK ideographs and full-width forms;
* English: Latin letters.

Latin letters are weighted at half a character because an English word spends several
letters where Japanese often spends one or two characters, so a Japanese sentence with a
product name in it still counts as Japanese.

Limitations: text written only in CJK ideographs (no kana) is classified as Japanese even
if it is Chinese, and other Latin-script languages are classified as English.
"""

from app.db.models import Language

JAPANESE_THRESHOLD = 0.8
ENGLISH_THRESHOLD = 0.2
_LATIN_WEIGHT = 0.5


def is_cjk(char: str) -> bool:
    """True for characters used to write Japanese (kana, kanji, CJK punctuation, full-width)."""
    code = ord(char)
    return (
        0x3000 <= code <= 0x30FF  # CJK symbols and punctuation, hiragana, katakana
        or 0x31F0 <= code <= 0x31FF  # katakana phonetic extensions
        or 0x3400 <= code <= 0x4DBF  # CJK unified ideographs extension A
        or 0x4E00 <= code <= 0x9FFF  # CJK unified ideographs
        or 0xF900 <= code <= 0xFAFF  # CJK compatibility ideographs
        or 0xFF00 <= code <= 0xFFEF  # half-width and full-width forms
        or 0x20000 <= code <= 0x2FA1F  # CJK extensions B to F and compatibility supplement
    )


def _is_latin_letter(char: str) -> bool:
    # ASCII letters, plus Latin-1 Supplement and Latin Extended-A/B letters (accented).
    return (char.isascii() and char.isalpha()) or ("À" <= char <= "ɏ" and char.isalpha())


def japanese_share(text: str) -> float | None:
    """Weighted share of Japanese characters among letters, or None if there are none."""
    japanese = 0
    latin = 0
    for char in text:
        if is_cjk(char):
            # Full-width digits and punctuation are not evidence of either language.
            if char.isalpha() or "぀" <= char <= "ヿ":
                japanese += 1
        elif _is_latin_letter(char):
            latin += 1
    total = japanese + latin * _LATIN_WEIGHT
    if total == 0:
        return None
    return japanese / total


def detect_language(text: str) -> Language:
    share = japanese_share(text)
    if share is None:
        return Language.UNKNOWN
    if share >= JAPANESE_THRESHOLD:
        return Language.JAPANESE
    if share <= ENGLISH_THRESHOLD:
        return Language.ENGLISH
    return Language.MIXED
