"""The bilingual keyword-search analyzer (SudachiPy for Japanese, Snowball for English)."""

import threading

import pytest

from app.search.analyzer import (
    analyze,
    analyze_query,
    any_term_query,
    highlight_spans,
    phrase_query,
    to_tsvector_literal,
)


def _terms(text: str) -> list[str]:
    return [token.term for token in analyze(text)]


def test_japanese_is_segmented_without_spaces_and_particles_are_dropped() -> None:
    terms = _terms("YashRAGは文書をPostgreSQLに保存します。")
    assert terms == ["yashrag", "文書", "postgresql", "保存"]


def test_compound_words_are_indexed_whole_and_as_parts() -> None:
    tokens = analyze("東京都庁で会議")
    assert [(t.term, t.position, t.compound) for t in tokens] == [
        ("東京都庁", 1, True),
        ("東京", 1, False),
        ("都庁", 2, False),
        ("会議", 4, False),
    ]


@pytest.mark.parametrize(
    ("variant", "canonical"),
    [
        ("ｶﾞｲﾄﾞ", "ガイド"),  # half-width katakana
        ("サーバ", "サーバー"),  # spelling variant
        ("附属", "付属"),  # kanji variant
    ],
)
def test_japanese_spelling_and_width_variants_are_normalised(variant: str, canonical: str) -> None:
    assert _terms(variant) == _terms(canonical)


def test_english_is_lower_cased_stemmed_and_stop_words_dropped() -> None:
    assert _terms("The Documents are split into Chunks") == ["document", "split", "chunk"]


def test_full_width_latin_matches_ascii() -> None:
    assert _terms("ＰｏｓｔｇｒｅＳＱＬ") == _terms("postgresql") == ["postgresql"]


def test_mixed_text_keeps_identifiers_and_numbers() -> None:
    terms = _terms("BGE-M3の埋め込みベクトルは1024次元です")
    assert terms[:2] == ["bge", "m3"]
    assert {"埋め込む", "ベクトル", "1024", "次元"} <= set(terms)


@pytest.mark.parametrize("text", ["", "   ", "。、！？", "the and of"])
def test_text_without_content_has_no_terms(text: str) -> None:
    assert analyze(text) == []


def test_offsets_point_into_the_original_text() -> None:
    text = "ＢＧＥ-M3 で ｶﾞｲﾄﾞ を読む"
    for token in analyze(text):
        assert 0 <= token.start < token.end <= len(text)
        assert text[token.start : token.end].strip()


def test_tsvector_literal_quotes_lexemes_and_groups_positions() -> None:
    tokens = analyze("search it's search")
    assert to_tsvector_literal(tokens) == "'it':2 'search':1,3"


def test_tsvector_literal_escapes_quotes_and_backslashes() -> None:
    from app.search.analyzer import Token

    literal = to_tsvector_literal([Token("a'b\\c", 1, 0, 5)])
    assert literal == "'a''b\\\\c':1"


def test_query_terms_are_deduplicated_in_order() -> None:
    analysis = analyze_query("Vector vectors 検索 検索")
    assert analysis.terms == ("vector", "検索")
    assert analysis.phrases == ()


def test_quoted_phrases_keep_relative_positions() -> None:
    analysis = analyze_query('"state of the art" 「東京都庁」')
    assert analysis.phrases == (
        ((0, "state"), (3, "art")),
        ((0, "東京"), (1, "都庁")),
    )
    assert phrase_query(analysis.phrases[0]) == "'state' <3> 'art'"


def test_any_term_query_is_an_or_of_quoted_terms() -> None:
    assert any_term_query(["a", "検索"]) == "'a' | '検索'"


def test_query_without_terms_is_empty() -> None:
    assert analyze_query("、。!?").is_empty


def test_highlights_cover_matching_words_only() -> None:
    text = "PostgreSQLに埋め込みベクトルを保存する"
    spans = highlight_spans(text, analyze_query("ベクトル postgresql").terms)
    assert [text[start:end] for start, end in spans] == ["PostgreSQL", "ベクトル"]


def test_highlights_merge_adjacent_parts_of_a_compound() -> None:
    text = "埋め込みベクトル"
    spans = highlight_spans(text, analyze_query("埋め込み").terms)
    assert [text[start:end] for start, end in spans] == ["埋め込み"]


def test_analyzer_is_thread_safe() -> None:
    text = "YashRAGは文書をPostgreSQLに保存します。Documents are split into chunks."
    expected = analyze(text)
    results: list[bool] = []

    def run() -> None:
        results.extend(analyze(text) == expected for _ in range(50))

    threads = [threading.Thread(target=run) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(results) == 400 and all(results)
