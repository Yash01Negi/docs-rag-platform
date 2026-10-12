"""Citation markers in answers: streaming validation and final classification.

The model cites source passages with markers such as ``[S2]`` or ``[S1, S3]`` (full-width
brackets ``［S2］`` and ``【S2】`` are accepted too). Every marker is checked against the
sources that were actually given to the model before any text reaches the client:

* valid markers are normalised to ``[S1][S3]``;
* a marker naming a source that was not provided is removed (and counted), so the client
  never receives a citation that does not map to a retrieved chunk;
* ``[NOT_FOUND]``, which the prompt asks for when the sources do not answer the question,
  is removed from the text and recorded as an abstention.

Because a marker can be split across streamed deltas, text from an opening bracket onwards
is held back until it is clearly a marker or clearly not one (at most a few dozen
characters).
"""

import re
from dataclasses import dataclass, field

ABSTAIN_MARKER = "NOT_FOUND"
_OPEN_CHARS = "[［【"
_OPEN = re.escape("[") + "［【"
_CLOSE = re.escape("]") + "］】"
_SEP = ",，、"
_MAX_HELD = 64

_ITEM = r"\s*[Ss]\s*(\d{1,3})\s*"
_COMPLETE = re.compile(
    rf"^[{_OPEN}](?:(?P<items>{_ITEM}(?:[{_SEP}]{_ITEM})*)|\s*(?P<abstain>{ABSTAIN_MARKER})\s*)"
    rf"[{_CLOSE}]"
)
# What a marker can look like before its closing bracket has arrived.
_PARTIAL = re.compile(
    rf"^[{_OPEN}](?:\s*(?:[Ss]\s*\d{{0,3}}\s*(?:[{_SEP}]\s*(?:[Ss]\s*\d{{0,3}}\s*)?)*)?"
    rf"|\s*N(?:O(?:T(?:_(?:F(?:O(?:U(?:N(?:D)?)?)?)?)?)?)?)?\s*)$"
)
_NUMBER = re.compile(r"\d{1,3}")
# For removing markers from earlier answers before they are sent back as history.
_ANY_MARKER = re.compile(
    rf"[{_OPEN}](?:{_ITEM}(?:[{_SEP}]{_ITEM})*|\s*{ABSTAIN_MARKER}\s*)[{_CLOSE}]"
)


@dataclass
class CitationFilter:
    """Validates markers in streamed text. Feed deltas, then call :meth:`flush`."""

    source_count: int
    cited: list[int] = field(default_factory=list)  # in order of first citation
    invalid_markers: int = 0
    abstained: bool = False
    _held: str = ""

    def feed(self, text: str) -> str:
        """Return the text that is safe to emit now."""
        buffer = self._held + text
        self._held = ""
        out: list[str] = []
        position = 0
        while position < len(buffer):
            start = _next_open(buffer, position)
            if start == -1:
                out.append(buffer[position:])
                break
            out.append(buffer[position:start])
            rest = buffer[start:]
            match = _COMPLETE.match(rest)
            if match:
                out.append(self._replace(match))
                position = start + match.end()
                continue
            if len(rest) < _MAX_HELD and _PARTIAL.match(rest):
                self._held = rest  # wait for more text
                break
            out.append(buffer[start])  # an ordinary bracket
            position = start + 1
        return "".join(out)

    def flush(self) -> str:
        """Release held text at the end of the stream (an unfinished marker is dropped)."""
        held, self._held = self._held, ""
        if held and _PARTIAL.match(held) and any(c.isdigit() for c in held):
            self.invalid_markers += 1  # a cut-off citation marker; do not show half of it
            return ""
        return held

    def _replace(self, match: re.Match[str]) -> str:
        if match.group("abstain"):
            self.abstained = True
            return ""
        valid: list[int] = []
        for number in (int(n) for n in _NUMBER.findall(match.group("items"))):
            if 1 <= number <= self.source_count:
                if number not in valid:
                    valid.append(number)
                if number not in self.cited:
                    self.cited.append(number)
            else:
                self.invalid_markers += 1
        return "".join(f"[S{number}]" for number in valid)


def _next_open(text: str, start: int) -> int:
    positions = [p for p in (text.find(c, start) for c in _OPEN_CHARS) if p != -1]
    return min(positions) if positions else -1


def strip_markers(text: str) -> str:
    """Remove citation and abstention markers (used for conversation history)."""
    return re.sub(r"[ \t]+\n", "\n", _ANY_MARKER.sub("", text)).strip()


def cited_indices(text: str, source_count: int) -> list[int]:
    """Valid source numbers cited in a complete text, in order of first appearance."""
    citation_filter = CitationFilter(source_count)
    citation_filter.feed(text)
    citation_filter.flush()
    return citation_filter.cited
