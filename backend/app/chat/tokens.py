"""Token estimates for prompt budgeting.

The provider's tokenizer is not available locally, so budgets use a conservative estimate:
one token per Japanese character (kana, kanji, full-width forms) and one per three other
characters. English prose averages about four characters per token with current Claude
tokenizers and Japanese about one to one and a half characters per token, so the estimate
errs on the high side. Actual usage is recorded from the provider's response.
"""

import math

from app.ingestion.language import is_cjk


def estimate_tokens(text: str) -> int:
    cjk = sum(1 for char in text if is_cjk(char))
    return cjk + math.ceil((len(text) - cjk) / 3)


def truncate_to_tokens(text: str, budget: int) -> str:
    """The longest prefix of ``text`` whose estimate fits in ``budget`` tokens."""
    if estimate_tokens(text) <= budget:
        return text
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if estimate_tokens(text[:middle]) <= budget:
            low = middle
        else:
            high = middle - 1
    return text[:low]
