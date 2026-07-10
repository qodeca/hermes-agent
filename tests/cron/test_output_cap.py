"""Tests for byte-capped cron output storage (finding 15).

A trivial cron job once persisted 261 KB of raw model deliberation as its
stored output — ``save_job_output`` had no byte cap, only a count-based
retention across runs. These tests cover the new ``cron.output_max_bytes``
cap: head(60%) + tail(30%) of the budget survive, joined by an elision
marker, and small outputs are stored byte-identical.
"""

import re

import pytest

from cron.jobs import (
    _CRON_OUTPUT_DEFAULT_MAX_BYTES,
    _cap_output_bytes,
    _cron_output_max_bytes,
    save_job_output,
)


@pytest.fixture()
def tmp_cron_dir(tmp_path, monkeypatch):
    """Redirect cron storage to a temp directory (mirrors tests/cron/test_jobs.py)."""
    monkeypatch.setattr("cron.jobs.CRON_DIR", tmp_path / "cron")
    monkeypatch.setattr("cron.jobs.JOBS_FILE", tmp_path / "cron" / "jobs.json")
    monkeypatch.setattr("cron.jobs.OUTPUT_DIR", tmp_path / "cron" / "output")
    return tmp_path


MARKER_RE = re.compile(r"\[\.\.\. (\d+) bytes elided by cron\.output_max_bytes \.\.\.\]")


def _marker_reserve(total_bytes: int) -> int:
    """The marker-length upper bound the implementation subtracts from the cap."""
    return len(f"\n[... {total_bytes} bytes elided by cron.output_max_bytes ...]\n")


def _split_around_marker(capped: str) -> tuple:
    """Return (head_part, tail_part) of a capped string, excluding the marker line."""
    m = MARKER_RE.search(capped)
    assert m is not None, "elision marker line must be present"
    # The marker is wrapped in newlines that belong to the marker, not content.
    head_part = capped[: m.start()].removesuffix("\n")
    tail_part = capped[m.end():].removeprefix("\n")
    return head_part, tail_part


class TestCapOutputBytesUnit:
    def test_default_cap_is_262144(self):
        assert _CRON_OUTPUT_DEFAULT_MAX_BYTES == 262144

    def test_under_cap_returned_unchanged(self):
        small = "hello world\n" * 10
        assert _cap_output_bytes(small, 262144) == small

    def test_at_exact_cap_returned_unchanged(self):
        exact = "x" * 100
        assert _cap_output_bytes(exact, 100) == exact

    def test_over_cap_truncated_with_marker(self):
        big = "A" * 1_048_576  # 1 MB
        capped = _cap_output_bytes(big, 262144)
        encoded = capped.encode("utf-8")
        assert len(encoded) <= 262144
        m = MARKER_RE.search(capped)
        assert m is not None, "elision marker line must be present"
        assert capped.startswith("A" * 100)  # head preserved
        assert capped.rstrip().endswith("A" * 100)  # tail preserved
        assert int(m.group(1)) > 0

    def test_head_is_roughly_60_percent_tail_roughly_30_percent(self):
        big = "B" * 1_048_576
        cap = 262144
        capped = _cap_output_bytes(big, cap)
        head_part, tail_part = _split_around_marker(capped)
        budget = cap - _marker_reserve(len(big.encode("utf-8")))
        assert len(head_part.encode("utf-8")) == int(budget * 0.6)
        assert len(tail_part.encode("utf-8")) == int(budget * 0.3)

    def test_non_positive_cap_disables_capping(self):
        big = "C" * 1_048_576
        assert _cap_output_bytes(big, 0) == big
        assert _cap_output_bytes(big, -1) == big

    def test_multibyte_boundary_no_corruption(self):
        # Emoji (4-byte UTF-8) and Polish diacritics (2-byte UTF-8) placed
        # right around where a naive byte-slice would land, to prove the
        # truncation never splits a multi-byte sequence.
        cap = 1000
        head_boundary = int(cap * 0.6)
        filler = "z" * (head_boundary - 5)
        emoji_run = "\U0001F600" * 20  # 4 bytes each, straddles the head cut
        polish_run = "zażółć gęślą jaźń " * 200  # multi-byte, straddles tail cut
        big = filler + emoji_run + polish_run
        assert len(big.encode("utf-8")) > cap

        capped = _cap_output_bytes(big, cap)
        encoded = capped.encode("utf-8")
        assert len(encoded) <= cap
        # Byte-level fidelity: the kept head must be a literal prefix of the
        # original and the kept tail a literal suffix (a partial multi-byte
        # char at either cut is dropped, never replaced or mangled).
        head_part, tail_part = _split_around_marker(capped)
        assert head_part and big.startswith(head_part)
        assert tail_part and big.endswith(tail_part)

    def test_elided_count_matches_dropped_bytes(self):
        big = "D" * 1_048_576
        cap = 262144
        capped = _cap_output_bytes(big, cap)
        m = MARKER_RE.search(capped)
        elided_n = int(m.group(1))
        original_bytes = len(big.encode("utf-8"))
        budget = cap - _marker_reserve(original_bytes)
        head_bytes = int(budget * 0.6)
        tail_bytes = int(budget * 0.3)
        assert elided_n == original_bytes - head_bytes - tail_bytes

    def test_tiny_cap_still_bounded_with_marker(self):
        # Regression: the marker used to be appended UNBUDGETED, so a small
        # cap (100) produced 60 + ~53-byte marker + 30 = 43% over cap.
        cap = 100
        big = "X" * 1000
        capped = _cap_output_bytes(big, cap)
        assert len(capped.encode("utf-8")) <= cap
        assert MARKER_RE.search(capped) is not None
        head_part, tail_part = _split_around_marker(capped)
        assert head_part and big.startswith(head_part)
        assert tail_part and big.endswith(tail_part)

    def test_cap_smaller_than_marker_degrades_to_bare_head(self):
        # Pinned behavior: when the cap can't even fit the marker line,
        # store a bare head truncated to the cap — no marker.
        cap = 10
        big = "Y" * 1000
        capped = _cap_output_bytes(big, cap)
        assert capped == "Y" * 10
        assert MARKER_RE.search(capped) is None

    @pytest.mark.parametrize("cap", [1, 2, 10, 50, 53, 54, 55, 100, 200, 1000, 4096])
    def test_result_never_exceeds_cap_for_any_positive_cap(self, cap):
        # The load-bearing invariant: for every max_bytes > 0 the stored
        # string is <= max_bytes, including caps around the marker's length.
        big = ("Z" * 500) + ("\U0001F600" * 50) + ("zażółć gęślą jaźń " * 30)
        capped = _cap_output_bytes(big, cap)
        encoded = capped.encode("utf-8")
        assert len(encoded) <= cap
        encoded.decode("utf-8")  # never a partial multi-byte sequence


class TestCronOutputMaxBytesConfig:
    def test_reads_config(self, monkeypatch):
        monkeypatch.setattr(
            "hermes_cli.config.load_config", lambda: {"cron": {"output_max_bytes": 1000}}
        )
        assert _cron_output_max_bytes() == 1000

    def test_defaults_on_missing_config(self, monkeypatch):
        monkeypatch.setattr("hermes_cli.config.load_config", lambda: {})
        assert _cron_output_max_bytes() == _CRON_OUTPUT_DEFAULT_MAX_BYTES

    def test_defaults_on_bad_config(self, monkeypatch):
        monkeypatch.setattr(
            "hermes_cli.config.load_config", lambda: {"cron": {"output_max_bytes": "oops"}}
        )
        assert _cron_output_max_bytes() == _CRON_OUTPUT_DEFAULT_MAX_BYTES


class TestSaveJobOutputByteCap:
    def test_small_output_stored_byte_identical(self, tmp_cron_dir):
        text = "# Results\nEverything ok.\n"
        output_file = save_job_output("small-job", text)
        assert output_file.read_text(encoding="utf-8") == text

    def test_large_output_capped_on_disk(self, tmp_cron_dir):
        big = "E" * 1_048_576  # 1 MB, well past the 256 KiB default cap
        output_file = save_job_output("big-job", big)
        stored = output_file.read_bytes()
        assert len(stored) <= _CRON_OUTPUT_DEFAULT_MAX_BYTES
        text = stored.decode("utf-8")
        assert MARKER_RE.search(text) is not None
        assert text.startswith("E" * 100)
        assert text.rstrip().endswith("E" * 100)

    def test_large_output_respects_configured_cap(self, tmp_cron_dir, monkeypatch):
        monkeypatch.setattr("cron.jobs._cron_output_max_bytes", lambda: 2048)
        big = "F" * 1_048_576
        output_file = save_job_output("configured-cap-job", big)
        stored = output_file.read_bytes()
        assert len(stored) <= 2048
        assert MARKER_RE.search(stored.decode("utf-8")) is not None

    def test_multibyte_output_near_boundary_stored_without_corruption(self, tmp_cron_dir, monkeypatch):
        monkeypatch.setattr("cron.jobs._cron_output_max_bytes", lambda: 1000)
        filler = "z" * 595
        emoji_run = "\U0001F600" * 30
        polish_run = "zażółć gęślą jaźń " * 50
        big = filler + emoji_run + polish_run
        output_file = save_job_output("emoji-job", big)
        stored = output_file.read_bytes()
        assert len(stored) <= 1000
        # Must decode cleanly — no partial multi-byte sequence written to disk.
        stored.decode("utf-8")
