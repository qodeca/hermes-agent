"""Pure detection of degenerating (repetition-loop) model output.

A looping model can emit the same deliberation endlessly while staying
inside every other guard: ``max_iterations`` counts tool calls, the
failure breaker counts errors, and the session output-token budget needs
volume to accumulate first. Observed incident: a trivial cron job emitted
261,333 bytes of "I'm done. Let me write the response now." repeated for
hundreds of KB before finally answering.

``looks_degenerate`` scores the newest assistant text with two cheap,
deterministic signals — no I/O, no model calls:

1. **Shingled n-gram overlap**: the share of the newest text's distinct
   word ``NGRAM_SIZE``-grams that also appear in the concatenation of the
   previous ``NGRAM_PRIOR_TEXTS`` texts exceeds
   ``NGRAM_OVERLAP_THRESHOLD`` (the model is re-emitting earlier turns).
2. **Repeated line**: the same normalized (case- and whitespace-folded)
   line appears at least ``LINE_REPEAT_THRESHOLD`` times within the
   newest text alone (the model is looping inside a single message).

Inputs are the *visible content* of recent assistant messages (newest
last). Reasoning traces are deliberately excluded by the extraction
helpers below: chain-of-thought legitimately revisits the same ground,
and the incident degeneration lived in the visible content.
"""

from collections import Counter
from typing import Any, Dict, List, Optional

# Tunable thresholds (also overridable per call via keyword arguments).
NGRAM_SIZE = 8
NGRAM_OVERLAP_THRESHOLD = 0.60
NGRAM_PRIOR_TEXTS = 3
# The newest text must contain at least this many distinct n-grams before
# the overlap signal is trusted — tiny messages ("Done.", one-liners)
# carry too little signal to accuse of degeneration.
MIN_NGRAMS = 10
LINE_REPEAT_THRESHOLD = 5
# Normalized lines shorter than this never count as repeats: structural
# noise (bullets, braces, "---" separators) repeats legitimately.
MIN_LINE_CHARS = 12


def _ngrams(text: str, n: int) -> set:
    words = text.lower().split()
    return {tuple(words[i:i + n]) for i in range(len(words) - n + 1)}


def _normalized_lines(text: str) -> List[str]:
    return [" ".join(line.split()).lower() for line in text.splitlines()]


def looks_degenerate(
    recent_texts: List[str],
    *,
    ngram_size: int = NGRAM_SIZE,
    overlap_threshold: float = NGRAM_OVERLAP_THRESHOLD,
    prior_texts: int = NGRAM_PRIOR_TEXTS,
    min_ngrams: int = MIN_NGRAMS,
    line_repeat_threshold: int = LINE_REPEAT_THRESHOLD,
    min_line_chars: int = MIN_LINE_CHARS,
) -> Optional[str]:
    """Score the newest of ``recent_texts`` (list ordered oldest→newest)
    for degeneration. Returns a short human-readable reason string when a
    signal trips, else ``None``. Pure and deterministic.
    """
    if not recent_texts:
        return None
    newest = recent_texts[-1]
    if not isinstance(newest, str) or not newest.strip():
        return None

    # Signal 1: the newest text mostly re-emits the previous texts.
    priors = [t for t in recent_texts[:-1] if isinstance(t, str) and t.strip()]
    priors = priors[-prior_texts:]
    if priors:
        newest_grams = _ngrams(newest, ngram_size)
        if len(newest_grams) >= min_ngrams:
            prior_grams = _ngrams("\n".join(priors), ngram_size)
            share = len(newest_grams & prior_grams) / len(newest_grams)
            if share > overlap_threshold:
                return (
                    f"{share:.0%} {ngram_size}-gram overlap with the previous "
                    f"{len(priors)} message(s) (threshold {overlap_threshold:.0%})"
                )

    # Signal 2: the newest text loops on itself line by line.
    counts = Counter(
        line for line in _normalized_lines(newest) if len(line) >= min_line_chars
    )
    if counts:
        line, repeats = counts.most_common(1)[0]
        if repeats >= line_repeat_threshold:
            return f"the same line repeated {repeats}x: {line[:80]!r}"

    return None


def newest_assistant_index(messages: List[Dict[str, Any]]) -> Optional[int]:
    """Index of the newest assistant message in an OpenAI-format
    transcript, or ``None`` when there is none."""
    for i in range(len(messages) - 1, -1, -1):
        m = messages[i]
        if isinstance(m, dict) and m.get("role") == "assistant":
            return i
    return None


def recent_assistant_texts(
    messages: List[Dict[str, Any]],
    limit: int = NGRAM_PRIOR_TEXTS + 1,
) -> List[str]:
    """Visible text content of the last ``limit`` assistant messages,
    ordered oldest→newest. Content only — tool results and reasoning are
    excluded (see module docstring); non-string content (multimodal
    parts) and blank messages are skipped."""
    texts: List[str] = []
    for m in reversed(messages):
        if not isinstance(m, dict) or m.get("role") != "assistant":
            continue
        content = m.get("content")
        if isinstance(content, str) and content.strip():
            texts.append(content)
            if len(texts) >= limit:
                break
    texts.reverse()
    return texts
