"""Tests for near-term date-pinned cron → one-shot conversion (#incident: model
emitted `0 2 10 7 *` for "tonight at 02:00"; it fired once and silently
rescheduled a year out instead of completing).

A 5-field cron expression whose day-of-month AND month fields are both pure
digits (no `*`, `-`, `,`, `/`) is structurally identical to legitimate yearly
cron syntax (`0 9 1 1 *` = every Jan 1). The only way to distinguish "the
model meant a one-shot" from "the user wants a yearly job" is proximity: if
the next occurrence is <=31 days away, treat it as a one-shot; otherwise it
stays a recurring cron job (with a create-time notice — covered in
tests/tools/test_cronjob_tools.py).
"""

from datetime import datetime, timedelta, timezone

import pytest

from cron.jobs import (
    is_date_pinned_cron_expr,
    is_date_pinned_yearly_schedule,
    parse_schedule,
)

CONFIGURED_TZ = timezone(timedelta(hours=1))  # arbitrary fixed offset, not UTC/local


def _fixed_now(dt: datetime):
    """A tz-aware datetime factory usable with monkeypatch.setattr."""
    return lambda: dt


class TestNearTermDatePinnedConversion:
    def test_near_term_date_pinned_becomes_one_shot(self, monkeypatch):
        """The incident case: '0 2 10 7 *' evaluated a day before July 10
        converts to kind='once' with run_at pinned to July 10 02:00."""
        now = datetime(2026, 7, 9, 10, 0, 0, tzinfo=CONFIGURED_TZ)
        monkeypatch.setattr("cron.jobs._hermes_now", _fixed_now(now))

        result = parse_schedule("0 2 10 7 *")

        assert result["kind"] == "once"
        run_at = datetime.fromisoformat(result["run_at"])
        assert run_at.replace(tzinfo=None) == datetime(2026, 7, 10, 2, 0, 0)
        # Timezone-consistent with the configured Hermes tz (same anchoring
        # as the ISO one-shot branch), not UTC or server-local.
        assert run_at.utcoffset() == now.utcoffset()

    def test_same_expr_far_from_its_date_stays_cron(self, monkeypatch):
        """Yearly-job regression guard: the identical expression, evaluated
        far from its next occurrence, must NOT be treated as a one-shot trap
        — it's legitimate yearly syntax ('0 9 1 1 *' style) and must stay
        recurring."""
        now = datetime(2026, 1, 1, 0, 0, 0, tzinfo=CONFIGURED_TZ)
        monkeypatch.setattr("cron.jobs._hermes_now", _fixed_now(now))

        result = parse_schedule("0 2 10 7 *")

        assert result["kind"] == "cron"
        assert result["expr"] == "0 2 10 7 *"

    def test_daily_cron_stays_cron(self, monkeypatch):
        now = datetime(2026, 7, 9, 10, 0, 0, tzinfo=CONFIGURED_TZ)
        monkeypatch.setattr("cron.jobs._hermes_now", _fixed_now(now))

        result = parse_schedule("0 9 * * *")

        assert result["kind"] == "cron"
        assert result["expr"] == "0 9 * * *"

    def test_weekday_pinned_cron_stays_cron(self, monkeypatch):
        now = datetime(2026, 7, 9, 10, 0, 0, tzinfo=CONFIGURED_TZ)
        monkeypatch.setattr("cron.jobs._hermes_now", _fixed_now(now))

        result = parse_schedule("0 9 * * 1")

        assert result["kind"] == "cron"
        assert result["expr"] == "0 9 * * 1"

    def test_monthly_cron_stays_cron(self, monkeypatch):
        now = datetime(2026, 7, 9, 10, 0, 0, tzinfo=CONFIGURED_TZ)
        monkeypatch.setattr("cron.jobs._hermes_now", _fixed_now(now))

        result = parse_schedule("0 9 1 * *")

        assert result["kind"] == "cron"
        assert result["expr"] == "0 9 1 * *"

    def test_display_string_shape_for_conversion(self, monkeypatch):
        now = datetime(2026, 7, 9, 10, 0, 0, tzinfo=CONFIGURED_TZ)
        monkeypatch.setattr("cron.jobs._hermes_now", _fixed_now(now))

        result = parse_schedule("0 2 10 7 *")

        assert result["display"].startswith("0 2 10 7 * → one-shot ")
        # The run_at echoed in the display matches the returned run_at.
        assert result["run_at"] in result["display"]

    def test_boundary_just_inside_31_days_is_one_shot(self, monkeypatch):
        """Next occurrence exactly 31 days away (well inside the window)
        converts to a one-shot."""
        target_month, target_day = 8, 9  # 31 days after 2026-07-09
        assert (
            datetime(2026, target_month, target_day) - datetime(2026, 7, 9)
        ).days == 31
        now = datetime(2026, 7, 9, 0, 0, 0, tzinfo=CONFIGURED_TZ)
        monkeypatch.setattr("cron.jobs._hermes_now", _fixed_now(now))

        result = parse_schedule(f"0 0 {target_day} {target_month} *")

        assert result["kind"] == "once"

    def test_boundary_just_outside_31_days_stays_cron(self, monkeypatch):
        """Next occurrence 32 days away (just past the window) stays cron —
        the near-term trap only covers genuinely close dates."""
        target_month, target_day = 8, 10  # 32 days after 2026-07-09
        assert (
            datetime(2026, target_month, target_day) - datetime(2026, 7, 9)
        ).days == 32
        now = datetime(2026, 7, 9, 0, 0, 0, tzinfo=CONFIGURED_TZ)
        monkeypatch.setattr("cron.jobs._hermes_now", _fixed_now(now))

        result = parse_schedule(f"0 0 {target_day} {target_month} *")

        assert result["kind"] == "cron"

    def test_six_field_expression_with_seconds_left_untouched(self, monkeypatch):
        """A 6-field expression (croniter appends seconds as the 6th field,
        confirmed: minute hour day month weekday second) is left untouched by
        the near-term conversion — the brief scopes detection to 5-field cron
        only."""
        pytest.importorskip("croniter")
        now = datetime(2026, 7, 9, 10, 0, 0, tzinfo=CONFIGURED_TZ)
        monkeypatch.setattr("cron.jobs._hermes_now", _fixed_now(now))

        result = parse_schedule("0 2 10 7 * 30")

        assert result["kind"] == "cron"
        assert result["expr"] == "0 2 10 7 * 30"


class TestIsDatePinnedCronExpr:
    """Unit tests for the structural detection helper."""

    def test_pure_digit_dom_and_month_is_date_pinned(self):
        assert is_date_pinned_cron_expr("0 9 1 1 *") is True
        assert is_date_pinned_cron_expr("0 2 10 7 *") is True

    def test_wildcard_dom_is_not_date_pinned(self):
        assert is_date_pinned_cron_expr("0 9 * 1 *") is False

    def test_wildcard_month_is_not_date_pinned(self):
        assert is_date_pinned_cron_expr("0 9 1 * *") is False

    def test_range_or_list_or_step_fields_are_not_date_pinned(self):
        assert is_date_pinned_cron_expr("0 9 1-5 1 *") is False
        assert is_date_pinned_cron_expr("0 9 1,15 1 *") is False
        assert is_date_pinned_cron_expr("0 9 1 1/2 *") is False

    def test_six_field_expression_is_not_date_pinned(self):
        assert is_date_pinned_cron_expr("0 9 1 1 * 30") is False

    def test_non_cron_string_is_not_date_pinned(self):
        assert is_date_pinned_cron_expr("every 30m") is False
        assert is_date_pinned_cron_expr("30m") is False


class TestIsDatePinnedYearlySchedule:
    """Unit tests for the schedule-dict-level helper consumed by the
    cronjob tool's create path to surface the far-out yearly notice."""

    def test_cron_kind_with_date_pinned_expr_is_yearly(self):
        schedule = {"kind": "cron", "expr": "0 9 1 1 *", "display": "0 9 1 1 *"}
        assert is_date_pinned_yearly_schedule(schedule) is True

    def test_cron_kind_with_wildcard_dom_is_not_yearly(self):
        schedule = {"kind": "cron", "expr": "0 9 * * *", "display": "0 9 * * *"}
        assert is_date_pinned_yearly_schedule(schedule) is False

    def test_once_kind_is_not_yearly(self):
        schedule = {"kind": "once", "run_at": "2026-07-10T02:00:00+01:00", "display": "x"}
        assert is_date_pinned_yearly_schedule(schedule) is False

    def test_interval_kind_is_not_yearly(self):
        schedule = {"kind": "interval", "minutes": 60, "display": "every 60m"}
        assert is_date_pinned_yearly_schedule(schedule) is False

    def test_non_dict_input_is_not_yearly(self):
        assert is_date_pinned_yearly_schedule(None) is False
        assert is_date_pinned_yearly_schedule("0 9 1 1 *") is False
