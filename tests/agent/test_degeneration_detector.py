"""Unit tests for the pure degeneration detector.

A looping model once emitted 261,333 bytes of "I'm done. Let me write the
response now. Final: Hello Marcin!" for a two-word answer — nothing scored
the *content* of the loop, so it only ended on max_iterations.
``agent.degeneration_detector.looks_degenerate`` is the pure scoring
function: two cheap signals (shingled 8-gram overlap with the previous
texts; the same normalized line repeated within the newest text), no I/O,
fully deterministic.
"""

import os
import sys

# Repo root = three levels up from tests/agent/<file>.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from agent.degeneration_detector import looks_degenerate  # noqa: E402

# The incident loop unit, verbatim.
INCIDENT_LINE = "I'm done. Let me write the response now. Final: Hello Marcin!"
INCIDENT_TEXT = "\n".join([INCIDENT_LINE] * 40)

VARIED_PARAGRAPHS = [
    "The gateway restarts cleanly now; the launchd plist reloads the "
    "service and the health endpoint reports ready within four seconds.",
    "Next I inspected the session database. FTS5 indexing lags roughly "
    "two hundred milliseconds behind writes, which is acceptable here.",
    "Deployment notes: the Tailscale certificate renews on the first of "
    "the month, and the dashboard picks it up without a restart.",
    "Finally, the cron scheduler honours the wall-clock cap — a job that "
    "spins on a failing backend is now killed after one hour as designed.",
]


# ── (a) incident fixture trips both signals ──────────────────────────────

def test_incident_loop_trips_line_repeat_signal():
    """The 261 KB greeting-loop pattern trips the repeated-line signal on
    a single message — no prior texts needed."""
    reason = looks_degenerate([INCIDENT_TEXT])
    assert reason is not None
    assert "repeat" in reason.lower()


def test_incident_loop_trips_ngram_overlap_signal():
    """With the loop spanning several turns, the newest text's 8-grams are
    entirely contained in the previous ones. Isolated from the line signal
    by raising its threshold out of reach."""
    reason = looks_degenerate(
        [INCIDENT_TEXT, INCIDENT_TEXT, INCIDENT_TEXT, INCIDENT_TEXT],
        line_repeat_threshold=10**9,
    )
    assert reason is not None
    assert "overlap" in reason.lower()


def test_incident_loop_detected_with_defaults():
    assert looks_degenerate([INCIDENT_TEXT] * 4) is not None


# ── (b) normal varied prose does not trip ────────────────────────────────

def test_varied_prose_is_not_degenerate():
    assert looks_degenerate(VARIED_PARAGRAPHS) is None


def test_single_normal_message_is_not_degenerate():
    assert looks_degenerate([VARIED_PARAGRAPHS[0]]) is None


def test_short_near_identical_narration_does_not_strike():
    """Legitimate short cron narration that repeats with one changed token
    each run must NOT strike. Each line is 15 words → 8 distinct 8-grams,
    below MIN_NGRAMS (10), so the overlap signal is suppressed even though
    the raw overlap (7/8 ≈ 88%) is well over the 60% threshold. Pins the
    MIN_NGRAMS guard against a future threshold reduction that would
    false-positive on progressing-but-similar narration."""
    base = "checked the rss feed and queued new items for review at time slot number {}"
    texts = [base.format(i) for i in range(4)]
    # Precondition: the newest text really is under the MIN_NGRAMS floor.
    from agent.degeneration_detector import MIN_NGRAMS, _ngrams
    assert len(_ngrams(texts[-1], 8)) < MIN_NGRAMS
    assert looks_degenerate(texts) is None


# ── (c) thresholds: just-under / just-over boundaries ────────────────────

def _overlap_pair(shared_words: int, new_words: int):
    """Build (prior, newest) where the newest text has exactly
    ``(shared_words - 7) + (new_words)`` ... precisely:

    newest = first ``shared_words`` tokens copied verbatim from prior
    + ``new_words`` brand-new tokens. All tokens distinct, one line, so
    only the n-gram signal can fire. Shared 8-grams = shared_words - 7
    (only n-grams fully inside the copied run exist in prior); total
    8-grams = shared_words + new_words - 7.
    """
    prior = " ".join(f"w{i}" for i in range(200))
    newest = " ".join(
        [f"w{i}" for i in range(shared_words)]
        + [f"u{i}" for i in range(new_words)]
    )
    return prior, newest


def test_overlap_exactly_at_threshold_does_not_trip():
    # 60 shared / 100 total 8-grams = 0.60 exactly — NOT > 0.60.
    prior, newest = _overlap_pair(shared_words=67, new_words=40)
    assert looks_degenerate([prior, newest]) is None


def test_overlap_just_over_threshold_trips():
    # 61 shared / 100 total 8-grams = 0.61 > 0.60.
    prior, newest = _overlap_pair(shared_words=68, new_words=39)
    reason = looks_degenerate([prior, newest])
    assert reason is not None
    assert "overlap" in reason.lower()


def _line_repeat_text(repeats: int) -> str:
    lines = [INCIDENT_LINE] * repeats + [
        f"unique filler line number {i} with several distinct words"
        for i in range(6)
    ]
    return "\n".join(lines)


def test_line_repeated_four_times_does_not_trip():
    # Single text → no priors → only the line signal can fire; 4 < 5.
    assert looks_degenerate([_line_repeat_text(4)]) is None


def test_line_repeated_five_times_trips():
    reason = looks_degenerate([_line_repeat_text(5)])
    assert reason is not None
    assert "repeat" in reason.lower()


def test_line_normalization_folds_case_and_whitespace():
    text = "\n".join([
        "Let me   finish THE response now please.",
        "let me finish the response now please.",
        "LET ME FINISH THE RESPONSE NOW PLEASE.",
        "  let me finish the response   now please.  ",
        "Let Me Finish The Response Now Please.",
    ])
    assert looks_degenerate([text]) is not None


def test_short_repeated_lines_are_ignored():
    """Structural noise (bullets, braces, separators) must not count as
    degeneration even when repeated many times."""
    text = "\n".join(["---"] * 20 + ["}"] * 20 + ["- ok"] * 20)
    assert looks_degenerate([text]) is None


# ── (d) empty / short input is safe ──────────────────────────────────────

def test_empty_input_is_safe():
    assert looks_degenerate([]) is None
    assert looks_degenerate([""]) is None
    assert looks_degenerate(["", "", ""]) is None


def test_short_texts_are_safe():
    assert looks_degenerate(["ok"]) is None
    assert looks_degenerate(["hello world"] * 4) is None
    assert looks_degenerate(["Done.", "Done.", "Done.", "Done."]) is None


def test_non_string_entries_are_safe():
    # Non-string priors are ignored; a degenerate newest still trips.
    assert looks_degenerate([None, INCIDENT_TEXT]) is not None  # type: ignore[list-item]
    # A non-string newest cannot be scored.
    assert looks_degenerate([None]) is None  # type: ignore[list-item]
    assert looks_degenerate([VARIED_PARAGRAPHS[0], None]) is None  # type: ignore[list-item]
