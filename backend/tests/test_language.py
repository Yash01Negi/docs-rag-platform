import pytest

from app.db.models import Language
from app.ingestion.language import detect_language, is_cjk, japanese_share
from tests.samples import ENGLISH_TEXT, JAPANESE_TEXT


@pytest.mark.parametrize(
    ("text", "language"),
    [
        (JAPANESE_TEXT, Language.JAPANESE),
        (ENGLISH_TEXT, Language.ENGLISH),
        ("ひらがなだけ", Language.JAPANESE),
        ("カタカナ", Language.JAPANESE),
        ("BGE-M3は多言語の埋め込みモデルです。", Language.JAPANESE),
        ("The word 東京 means eastern capital.", Language.ENGLISH),
        ("本日の meeting では new product launch について discuss しました", Language.MIXED),
        ("2026-10-12 12:00 / 100%", Language.UNKNOWN),
        ("", Language.UNKNOWN),
        ("Café déjà vu", Language.ENGLISH),
    ],
)
def test_detect_language(text: str, language: Language) -> None:
    assert detect_language(text) is language


def test_fullwidth_digits_are_not_evidence_of_japanese() -> None:
    assert japanese_share("１２３４５") is None


@pytest.mark.parametrize(
    ("char", "expected"),
    [
        ("あ", True),
        ("ア", True),
        ("漢", True),
        ("。", True),
        ("Ａ", True),
        ("a", False),
        (" ", False),
        ("é", False),
    ],
)
def test_is_cjk(char: str, expected: bool) -> None:
    assert is_cjk(char) is expected
