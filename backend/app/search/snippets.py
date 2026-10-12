"""Snippets: the part of a chunk shown in a search result, with the query's words marked.

Highlights are computed by running the keyword analyzer over the chunk text, so a match is
marked only where the chunk really contains one of the query's terms (after the same
normalisation used for searching). No text is generated or rewritten.
"""

from dataclasses import dataclass

from app.search.analyzer import highlight_spans


@dataclass(frozen=True)
class Snippet:
    text: str
    start: int  # offset of the snippet in the chunk text
    highlights: list[tuple[int, int]]  # offsets relative to the snippet text

    def truncated_start(self) -> bool:
        return self.start > 0


def make_snippet(chunk_text: str, terms: tuple[str, ...], max_chars: int) -> Snippet:
    """Pick the window of ``max_chars`` characters containing the most query-term matches."""
    spans = highlight_spans(chunk_text, terms) if terms else []
    if len(chunk_text) <= max_chars:
        return Snippet(chunk_text, 0, spans)
    start = 0
    if spans:
        lead = max_chars // 5  # show some context before the first match
        best_count = -1
        for span_start, _ in spans:
            window_start = max(0, span_start - lead)
            window_end = window_start + max_chars
            count = sum(1 for s, e in spans if s >= window_start and e <= window_end)
            if count > best_count:
                best_count, start = count, window_start
        start = min(start, len(chunk_text) - max_chars)
        start = _snap_back(chunk_text, start)
    end = min(len(chunk_text), start + max_chars)
    window = [
        (max(s, start) - start, min(e, end) - start) for s, e in spans if s < end and e > start
    ]
    return Snippet(chunk_text[start:end], start, window)


def _snap_back(text: str, start: int, reach: int = 20) -> int:
    """Move a window start back to the beginning of a word or sentence when one is near."""
    for offset in range(start, max(-1, start - reach), -1):
        if offset == 0 or text[offset - 1] in " \n\t。、．，.!?！？":
            return offset
    return start
