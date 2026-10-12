import re
from pathlib import Path

import pytest

from app.db.models import FileType, Language
from app.embeddings.fake import fake_token_count
from app.ingestion.chunking import ChunkingConfig, TextChunk, chunk_blocks, split_sentences
from app.ingestion.extraction import Block, extract, extract_markdown
from tests.samples import ENGLISH_TEXT, JAPANESE_TEXT, make_pdf

count = fake_token_count
SMALL = ChunkingConfig(target_tokens=60, overlap_tokens=15, min_tokens=10)


def _squash(text: str) -> str:
    return re.sub(r"\s+", "", text)


def _assert_covers(chunks: list[TextChunk], source: str) -> None:
    """Every sentence of the source appears in some chunk, in source order."""
    sentences = [s for _, s in split_sentences(source)]
    joined = _squash("".join(c.text for c in chunks))
    position = 0
    for sentence in sentences:
        found = joined.find(_squash(sentence), position)
        assert found >= 0, f"sentence missing or out of order: {sentence!r}"
        position = found


def _assert_within_limits(chunks: list[TextChunk], config: ChunkingConfig) -> None:
    for chunk in chunks:
        assert 0 < chunk.token_count <= config.max_tokens
        assert chunk.token_count == count(chunk.text)
    assert [c.ordinal for c in chunks] == list(range(len(chunks)))


def test_split_sentences_handles_japanese_and_english_punctuation() -> None:
    assert split_sentences("今日は晴れ。明日は雨！本当？「はい。」 Yes. No! Why?") == [
        ("", "今日は晴れ。"),
        ("", "明日は雨！"),
        ("", "本当？"),
        ("", "「はい。」"),
        (" ", "Yes."),
        (" ", "No!"),
        (" ", "Why?"),
    ]


def test_split_sentences_does_not_split_decimals_or_versions() -> None:
    assert split_sentences("Version 3.12 costs $1.50 today.") == [
        ("", "Version 3.12 costs $1.50 today.")
    ]


PURE_JAPANESE = (
    "吾輩は猫である。名前はまだ無い。どこで生れたかとんと見当がつかぬ。"
    "何でも薄暗いじめじめした所でニャーニャー泣いていた事だけは記憶している。"
)


def test_japanese_only_text() -> None:
    text = PURE_JAPANESE * 8
    chunks = chunk_blocks([Block(text)], count, SMALL)

    assert len(chunks) > 1
    assert all(c.language is Language.JAPANESE for c in chunks)
    # Japanese sentences are joined without inserted spaces.
    assert all(" " not in c.text for c in chunks)
    _assert_within_limits(chunks, SMALL)
    _assert_covers(chunks, text)
    # Chunks end on sentence boundaries when sentences fit.
    assert all(c.text.endswith("。") for c in chunks)


def test_english_only_text() -> None:
    text = " ".join([ENGLISH_TEXT] * 6)
    chunks = chunk_blocks([Block(text)], count, SMALL)

    assert len(chunks) > 1
    assert all(c.language is Language.ENGLISH for c in chunks)
    assert all(c.text.endswith(".") for c in chunks)
    _assert_within_limits(chunks, SMALL)
    _assert_covers(chunks, text)


def test_mixed_japanese_and_english() -> None:
    blocks = [Block(JAPANESE_TEXT), Block(ENGLISH_TEXT), Block(JAPANESE_TEXT + " " + ENGLISH_TEXT)]
    chunks = chunk_blocks(blocks, count, ChunkingConfig(400, 60, 40))

    assert len(chunks) == 1
    assert chunks[0].language is Language.MIXED
    # Paragraph boundaries are preserved inside a chunk.
    assert chunks[0].text.count("\n\n") == 2


def test_short_document_is_one_chunk() -> None:
    chunks = chunk_blocks([Block("短い文書です。")], count, ChunkingConfig())
    assert len(chunks) == 1
    assert chunks[0].text == "短い文書です。"
    assert chunks[0].token_count == count("短い文書です。")


def test_no_blocks_produce_no_chunks() -> None:
    assert chunk_blocks([], count, ChunkingConfig()) == []


def test_very_long_japanese_sentence_is_split_without_loss() -> None:
    # One "sentence" with commas but no full stop, far longer than the target.
    sentence = "、".join(["これは非常に長い文の一部です"] * 60) + "。"
    chunks = chunk_blocks([Block(sentence)], count, SMALL)

    assert len(chunks) > 3
    _assert_within_limits(chunks, SMALL)
    assert _squash("".join(c.text for c in chunks)) == _squash(sentence)


def test_very_long_unbroken_run_is_hard_split_without_loss() -> None:
    run = "あ" * 500  # no punctuation or whitespace at all
    chunks = chunk_blocks([Block(run)], count, SMALL)

    assert "".join(c.text for c in chunks) == run
    assert all(c.token_count <= SMALL.target_tokens for c in chunks)


def test_very_long_english_sentence_breaks_at_word_boundaries() -> None:
    sentence = " ".join(f"word{i}" for i in range(300)) + "."
    chunks = chunk_blocks([Block(sentence)], count, SMALL)

    _assert_within_limits(chunks, SMALL)
    words = " ".join(c.text for c in chunks).split()
    assert all(re.fullmatch(r"word\d+\.?", w) for w in words)  # no word was cut in half
    assert " ".join(dict.fromkeys(words)) == sentence  # every word, in order


def test_overlap_repeats_whole_sentences_up_to_the_budget() -> None:
    sentences = [f"Sentence number {i} is here." for i in range(40)]
    chunks = chunk_blocks([Block(" ".join(sentences))], count, SMALL)

    assert len(chunks) > 2
    for previous, current in zip(chunks, chunks[1:], strict=False):
        previous_sentences = [s for _, s in split_sentences(previous.text)]
        current_sentences = [s for _, s in split_sentences(current.text)]
        shared = [s for s in current_sentences if s in previous_sentences]
        assert shared, "consecutive chunks should overlap"
        assert current_sentences[: len(shared)] == shared  # overlap is a prefix
        assert previous_sentences[-len(shared) :] == shared  # taken from the previous end
        assert count(" ".join(shared)) <= SMALL.overlap_tokens


def test_zero_overlap_partitions_text_exactly() -> None:
    text = " ".join([ENGLISH_TEXT] * 5)
    config = ChunkingConfig(target_tokens=50, overlap_tokens=0, min_tokens=0)
    chunks = chunk_blocks([Block(text)], count, config)
    assert " ".join(c.text for c in chunks) == text


def test_token_count_boundaries() -> None:
    # Each sentence is exactly 10 fake tokens (one per Japanese character).
    sentence = "あ" * 9 + "。"
    assert count(sentence) == 10
    config = ChunkingConfig(target_tokens=30, overlap_tokens=0, min_tokens=0)

    chunks = chunk_blocks([Block(sentence * 6)], count, config)
    # Exactly three sentences fit in 30 tokens; a fourth would exceed the target.
    assert [c.token_count for c in chunks] == [30, 30]

    # A short tail that cannot be merged without exceeding target + min stays separate.
    chunks = chunk_blocks([Block(sentence * 3 + "い。")], count, config)
    assert [c.token_count for c in chunks] == [30, 2]


def test_tiny_trailing_chunk_is_merged_when_it_fits() -> None:
    sentence = "あ" * 9 + "。"  # 10 tokens
    config = ChunkingConfig(target_tokens=30, overlap_tokens=0, min_tokens=5)
    chunks = chunk_blocks([Block(sentence * 3 + "短い。")], count, config)
    assert len(chunks) == 1
    assert chunks[0].token_count == 33 <= config.max_tokens


def test_trailing_chunk_is_kept_when_merge_would_exceed_the_hard_limit() -> None:
    sentence = "あ" * 9 + "。"  # 10 tokens
    config = ChunkingConfig(target_tokens=30, overlap_tokens=0, min_tokens=5)
    tail = "いいい。"  # 4 tokens: below min_tokens, but 30 + 8 > max_tokens (35)
    chunks = chunk_blocks([Block(sentence * 3 + tail * 2)], count, config)
    assert [c.token_count for c in chunks] == [30, 8]


def test_headings_start_new_chunks_and_set_sections() -> None:
    body_ja = JAPANESE_TEXT * 2
    body_en = " ".join([ENGLISH_TEXT] * 2)
    blocks = extract_markdown(f"# 概要\n\n{body_ja}\n\n## Details\n\n{body_en}\n")
    chunks = chunk_blocks(blocks, count, ChunkingConfig(400, 60, 20))

    assert [c.section for c in chunks] == ["概要", "概要 > Details"]
    assert chunks[0].text.startswith("概要\n\n")
    assert chunks[1].text.startswith("Details\n\n")
    assert chunks[0].language is Language.JAPANESE
    assert chunks[1].language is Language.ENGLISH


def test_multi_page_pdf_chunks_carry_page_ranges(tmp_path: Path) -> None:
    path = tmp_path / "doc.pdf"
    path.write_bytes(make_pdf([ENGLISH_TEXT, ENGLISH_TEXT, "", JAPANESE_TEXT]))
    document = extract(path, FileType.PDF)
    chunks = chunk_blocks(document.blocks, count, ChunkingConfig(80, 10, 10))

    assert chunks[0].page_start == 1
    assert chunks[-1].page_end == 4
    for chunk in chunks:
        assert chunk.page_start is not None and chunk.page_end is not None
        assert 1 <= chunk.page_start <= chunk.page_end <= 4
        assert 3 not in range(chunk.page_start, chunk.page_end + 1) or (
            chunk.page_start < 3 < chunk.page_end
        )
    starts = [c.page_start for c in chunks]
    assert starts == sorted(starts)


def test_empty_pages_do_not_produce_chunks(tmp_path: Path) -> None:
    path = tmp_path / "doc.pdf"
    path.write_bytes(make_pdf(["", JAPANESE_TEXT, ""]))
    chunks = chunk_blocks(extract(path, FileType.PDF).blocks, count, ChunkingConfig())
    assert len(chunks) == 1
    assert (chunks[0].page_start, chunks[0].page_end) == (2, 2)


@pytest.mark.parametrize(
    ("target", "overlap", "minimum"),
    [(10, 10, 0), (10, 11, 0), (10, 0, 10), (0, 0, 0)],
)
def test_invalid_config_is_rejected(target: int, overlap: int, minimum: int) -> None:
    with pytest.raises(ValueError, match="tokens"):
        ChunkingConfig(target_tokens=target, overlap_tokens=overlap, min_tokens=minimum)
