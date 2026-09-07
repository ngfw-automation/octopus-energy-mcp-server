"""Unit tests for the pure shaping helpers (no network)."""

from datetime import datetime, timedelta, timezone

import pytest

from octopus_mcp import shaping


def make_rows(n: int, start: str = "2026-08-01T00:00:00Z") -> list[dict]:
    base = datetime.fromisoformat(start.replace("Z", "+00:00"))
    return [
        {
            "interval_start": (base + timedelta(minutes=30 * i)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
            "consumption": 1.0,
        }
        for i in range(n)
    ]


def test_parse_tariff_code_electricity_agile():
    p = shaping.parse_tariff_code("E-1R-AGILE-18-02-21-C")
    assert p["fuel"] == "E"
    assert p["registers"] == "1R"
    assert p["product_code"] == "AGILE-18-02-21"
    assert p["region"] == "C"


def test_fuel_from_tariff():
    assert shaping.fuel_from_tariff("E-1R-AGILE-18-02-21-C") == "electricity"
    assert shaping.fuel_from_tariff("G-1R-FLX-24-01-01-B") == "gas"
    assert shaping.tariff_path("electricity") == "electricity-tariffs"
    assert shaping.tariff_path("gas") == "gas-tariffs"


def test_summarize_day():
    # 48 slots from midnight UTC = 01:00 BST, so they straddle two UK days:
    # UK "days" are what the bill charges for, not UTC days.
    rows = make_rows(48)
    series, stats = shaping.summarize(
        rows, "2026-08-01T00:00:00Z", "2026-08-02T00:00:00Z", "day"
    )
    assert len(series) == 2
    assert series[0]["start"] == "2026-08-01T00:00:00+01:00"
    assert sum(s["value"] for s in series) == pytest.approx(48.0)
    assert stats["total"] == pytest.approx(48.0)
    assert stats["n"] == 48
    assert stats["missing_intervals"] == 0


def test_summarize_hour():
    rows = make_rows(48)
    series, stats = shaping.summarize(
        rows, "2026-08-01T00:00:00Z", "2026-08-02T00:00:00Z", "hour"
    )
    assert len(series) == 24
    assert all(s["value"] == pytest.approx(2.0) for s in series)
    assert stats["total"] == pytest.approx(48.0)


def test_summarize_half_hour_passthrough():
    rows = make_rows(4)
    series, stats = shaping.summarize(
        rows, "2026-08-01T00:00:00Z", "2026-08-01T02:00:00Z", "half_hour"
    )
    assert len(series) == 4
    assert stats["n"] == 4
    assert series[0]["start"] == "2026-08-01T01:00:00+01:00"  # BST label
    assert series[0]["end"] == "2026-08-01T01:30:00+01:00"


def test_missing_intervals_detected():
    rows = make_rows(10)  # only 5 hours of data
    _, stats = shaping.summarize(
        rows, "2026-08-01T00:00:00Z", "2026-08-02T00:00:00Z", "day"
    )
    assert stats["n"] == 10
    assert stats["missing_intervals"] == 38  # 48 expected - 10 present


def test_peak_period_and_extremes():
    rows = make_rows(4)
    rows[2]["consumption"] = 9.0  # make the 3rd slot the peak
    _, stats = shaping.summarize(
        rows, "2026-08-01T00:00:00Z", "2026-08-01T02:00:00Z", "half_hour"
    )
    assert stats["max"] == 9.0
    assert stats["peak_period"] == "2026-08-01T02:00:00+01:00"


def test_empty_rows():
    series, stats = shaping.summarize(
        [], "2026-08-01T00:00:00Z", "2026-08-02T00:00:00Z", "day"
    )
    assert series == []
    assert stats["n"] == 0
    assert stats["total"] == 0.0


# --- bill_cost (spec §1.2) ---------------------------------------------------


def _kwh_rows(values: list) -> list[dict]:
    base = datetime.fromisoformat("2026-08-01T00:00:00+00:00")
    return [
        {
            "interval_start": (base + timedelta(minutes=30 * i)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
            "consumption": v,
        }
        for i, v in enumerate(values)
    ]


def _rate(value, pm=None, valid_from="2026-07-01T00:00:00Z"):
    return {"valid_from": valid_from, "valid_to": None, "value_exc_vat": value,
            "payment_method": pm}


def _sc(value, valid_from="2026-07-01T00:00:00Z", pm=None):
    """A standing-charge row shaped like the real endpoint's."""
    return {
        "valid_from": valid_from,
        "valid_to": None,
        "value_exc_vat": value,
        "value_inc_vat": round(value * 1.05, 6),
        "payment_method": pm,
    }


def test_bill_cost_penny_math():
    b = shaping.bill_cost(
        _kwh_rows([1.0, 2.0]), [_rate(20.0, "DIRECT_DEBIT")],
        "2026-08-01T00:00:00Z", "2026-08-02T00:00:00Z",
        "DIRECT_DEBIT", [],
    )
    assert b["unit_cost_pence"] == 60
    assert b["standing_charge_pence"] == 0
    assert b["subtotal_ex_vat_pence"] == 60
    assert b["vat_5pct_pence"] == 3
    assert b["total_inc_vat_pence"] == 63
    assert b["total_gbp"] == 0.63
    assert b["sc_source"] is None


def test_bill_cost_rounds_kwh_per_interval_but_not_cost():
    # 0.125 kWh -> 0.12 (banker's) per interval, * 10p = 1.2p, kept sub-penny.
    # 8 intervals = 9.6p, rounded to the penny once: 10p. Rounding each
    # interval to 1p instead would give 8p -- a 20% error here, ~1% on a
    # real month (F-02).
    b = shaping.bill_cost(
        _kwh_rows([0.125] * 8), [_rate(10.0)],
        "2026-08-01T00:00:00Z", "2026-08-02T00:00:00Z",
        None, [],
    )
    assert b["billed_kwh"] == 0.96
    assert b["unit_cost_pence"] == 10
    assert b["total_gbp"] == 0.10


def test_bill_cost_standing_charge_and_vat():
    # 31 days * 45p = 1395p; unit 10p -> subtotal 1405p; VAT round(70.25) = 70p
    b = shaping.bill_cost(
        _kwh_rows([1.0]), [_rate(10.0)],
        "2026-08-01T00:00:00Z", "2026-09-01T00:00:00Z",
        None, [_sc(45.0)],
    )
    assert b["standing_charge_pence"] == 1395
    assert b["sc_source"] == "api"
    assert b["subtotal_ex_vat_pence"] == 1405
    assert b["vat_5pct_pence"] == 70
    assert b["total_inc_vat_pence"] == 1475
    assert b["total_gbp"] == 14.75


def test_bill_cost_says_so_when_no_standing_charge_is_published():
    """The published charge is the only source. A tariff that publishes none
    contributes zero, and the total says it may be low, rather than the server
    substituting a figure of its own."""
    b = shaping.bill_cost(
        _kwh_rows([1.0]), [_rate(10.0)],
        "2026-08-01T00:00:00Z", "2026-09-01T00:00:00Z",
        None, [_sc(0.0)],
    )
    assert b["sc_source"] is None
    assert b["standing_charge_pence"] == 0
    assert any("publishes no standing charge" in n for n in b["notes"])


def test_bill_cost_fractional_day_standing_charge():
    # 1 hour of a 48p/day standing charge = 2p
    b = shaping.bill_cost(
        _kwh_rows([1.0, 1.0]), [_rate(10.0)],
        "2026-08-01T00:00:00Z", "2026-08-01T01:00:00Z",
        None, [_sc(48.0)],
    )
    assert b["standing_charge_pence"] == 2


def test_bill_cost_payment_method_filter():
    rows = _kwh_rows([1.0])
    rates = [_rate(10.0, "DIRECT_DEBIT"), _rate(12.0, "NON_DIRECT_DEBIT")]
    # Exact payment-method match wins
    b = shaping.bill_cost(
        rows, rates, "2026-08-01T00:00:00Z", "2026-08-02T00:00:00Z",
        "DIRECT_DEBIT", [],
    )
    assert b["unit_cost_pence"] == 10
    # pm=None -> conservative fallback to the highest rate, nothing unpriced
    b2 = shaping.bill_cost(
        rows, rates, "2026-08-01T00:00:00Z", "2026-08-02T00:00:00Z",
        None, [],
    )
    assert b2["unit_cost_pence"] == 12
    assert b2["unpriced_kwh"] == 0.0
    assert any("highest rate" in n for n in b2["notes"])


def test_bill_cost_no_rates():
    b = shaping.bill_cost(
        _kwh_rows([1.0]), [],
        "2026-08-01T00:00:00Z", "2026-08-02T00:00:00Z",
        None, [],
    )
    assert b["error"] == "no_rates"


# --- F-01: standing charges come from value_exc_vat / value_inc_vat -------


def test_sc_reads_exc_vat_field():
    pence, note, source = shaping.sc_pence_for_period(
        [_sc(48.2442)], "2026-08-01T00:00:00Z", "2026-09-01T00:00:00Z"
    )
    assert source == "api"
    assert note is None
    assert round(float(pence)) == 1496  # 31 days * 48.2442p ex VAT


def test_sc_falls_back_to_inc_vat_field():
    row = {"valid_from": "2026-07-01T00:00:00Z", "valid_to": None,
           "value_inc_vat": 52.5, "payment_method": None}
    pence, _note, source = shaping.sc_pence_for_period(
        [row], "2026-08-01T00:00:00Z", "2026-08-02T00:00:00Z"
    )
    assert source == "api"
    assert round(float(pence), 4) == 50.0  # 52.5 inc VAT -> 50p ex VAT


def test_sc_accepts_legacy_value_key():
    legacy = {"valid_from": "2026-07-01T00:00:00Z", "valid_to": None, "value": 45.0}
    pence, _note, source = shaping.sc_pence_for_period(
        [legacy], "2026-08-01T00:00:00Z", "2026-08-02T00:00:00Z"
    )
    assert (round(float(pence)), source) == (45, "api")


def test_sc_picks_the_matching_payment_method():
    rows = [_sc(48.2442, pm="DIRECT_DEBIT"), _sc(56.4891, pm="NON_DIRECT_DEBIT")]
    dd, _n, _s = shaping.sc_pence_for_period(
        rows, "2026-08-01T00:00:00Z", "2026-08-02T00:00:00Z", "DIRECT_DEBIT"
    )
    ndd, _n2, _s2 = shaping.sc_pence_for_period(
        rows, "2026-08-01T00:00:00Z", "2026-08-02T00:00:00Z", "NON_DIRECT_DEBIT"
    )
    assert round(float(dd), 4) == 48.2442
    assert round(float(ndd), 4) == 56.4891
    # Unpinned: the dearest, conservatively, with a note saying so.
    either, note, _s3 = shaping.sc_pence_for_period(
        rows, "2026-08-01T00:00:00Z", "2026-08-02T00:00:00Z"
    )
    assert round(float(either), 4) == 56.4891
    assert "highest" in note


def test_sc_unpublished_is_zero_with_a_note_not_a_substituted_figure():
    """Nothing about the standing charge is configurable: what the tariff
    publishes is what is used, and a tariff publishing none says so."""
    pence, note, source = shaping.sc_pence_for_period(
        [_sc(0.0)], "2026-08-01T00:00:00Z", "2026-08-02T00:00:00Z"
    )
    assert source is None
    assert float(pence) == 0.0
    assert "publishes no standing charge" in note
    # ... and a published charge is used as published.
    pence2, _n, source2 = shaping.sc_pence_for_period(
        [_sc(48.0)], "2026-08-01T00:00:00Z", "2026-08-02T00:00:00Z"
    )
    assert (round(float(pence2)), source2) == (48, "api")


# --- F-02: a month of real-shaped data reconciles with the bill ----------


def test_bill_cost_reconciles_a_month_without_penny_bias():
    """August 2026 shaped like the real export: 1,488 half-hours averaging
    ~0.4 kWh, so each interval costs ~10p and the fractional pence do not
    cancel. Per-interval penny rounding put this ~1% over the invoice."""
    profile = [0.19, 0.41, 0.63, 0.27, 0.52, 0.35]  # repeats over the month
    values = [profile[i % len(profile)] for i in range(48 * 31)]
    rate = 25.1251
    b = shaping.bill_cost(
        _kwh_rows(values), [_rate(rate, "DIRECT_DEBIT")],
        "2026-08-01T00:00:00Z", "2026-09-01T00:00:00Z",
        "DIRECT_DEBIT", [_sc(48.2442, pm="DIRECT_DEBIT")],
    )
    expected_unit = round(sum(values) * rate)          # kWh already at 2 dp
    expected_sc = round(31 * 48.2442)
    assert b["unit_cost_pence"] == expected_unit
    assert b["standing_charge_pence"] == expected_sc
    subtotal = expected_unit + expected_sc
    assert b["subtotal_ex_vat_pence"] == subtotal
    assert b["total_inc_vat_pence"] == subtotal + round(subtotal * 0.05)
    # The old per-interval rounding inflated this by roughly a percent.
    naive = sum(round(v * rate) for v in values)
    assert naive > expected_unit * 1.005


# --- F-05: gas conversion factor -----------------------------------------


def test_m3_to_kwh_factor():
    # 39.5 MJ/m3 * 1.02264 / 3.6
    assert round(shaping.m3_to_kwh_factor(39.5), 4) == 11.2206
    assert round(shaping.m3_to_kwh_factor(38.0), 4) == 10.7945
    assert shaping.m3_to_kwh_factor() == shaping.m3_to_kwh_factor(39.5)


def test_stale_standing_charge_is_flagged():
    """Agile's region-A charge is still the 20p/day row dated 2017; now that
    published charges are actually used, say how old one is."""
    ancient = _sc(20.0, valid_from="2017-01-01T00:00:00Z")
    assert shaping.sc_staleness_note([ancient], "2026-08-01T00:00:00Z") is not None
    assert shaping.sc_staleness_note([_sc(48.0)], "2026-08-01T00:00:00Z") is None

    b = shaping.bill_cost(
        _kwh_rows([1.0]), [_rate(10.0)],
        "2026-08-01T00:00:00Z", "2026-09-01T00:00:00Z",
        None, [ancient],
    )
    assert any("last published on 2017-01-01" in n for n in b["notes"])


# --- F-10: UK local buckets and period bounds ----------------------------


def test_day_buckets_follow_uk_local_days():
    """A UK calendar day in summer is 23:00Z to 23:00Z, not midnight to
    midnight -- the bill buckets that way and so must we."""
    rows = make_rows(48, start="2026-07-31T23:00:00Z")
    series, _stats = shaping.summarize(
        rows, "2026-07-31T23:00:00Z", "2026-08-01T23:00:00Z", "day"
    )
    assert len(series) == 1
    assert series[0]["start"] == "2026-08-01T00:00:00+01:00"
    assert series[0]["end"] == "2026-08-02T00:00:00+01:00"
    assert series[0]["value"] == pytest.approx(48.0)


def test_month_bucket_does_not_leak_into_the_previous_month():
    """The whole of August, as a UK month, is one bucket -- it used to spill
    an hour into July."""
    rows = make_rows(48 * 31, start="2026-07-31T23:00:00Z")
    series, _stats = shaping.summarize(
        rows, "2026-07-31T23:00:00Z", "2026-08-31T23:00:00Z", "month"
    )
    assert len(series) == 1
    assert series[0]["start"] == "2026-08-01T00:00:00+01:00"


def test_clocks_change_days_are_23_and_25_hours():
    def elapsed(day: str) -> timedelta:
        start = shaping.bucket_start(shaping.parse_dt(f"{day}T12:00:00Z"), "day")
        end = shaping.bucket_end(start, "day")
        # Compare in UTC: subtracting two datetimes that share a tzinfo gives
        # the wall-clock difference, which is 24h on both of these days.
        return end.astimezone(timezone.utc) - start.astimezone(timezone.utc)

    assert elapsed("2026-03-29") == timedelta(hours=23)  # clocks forward
    assert elapsed("2026-10-25") == timedelta(hours=25)  # clocks back


def test_normalise_period_accepts_plain_dates():
    # "August" in UK local time, which is what a customer means.
    start, end = shaping.normalise_period("2026-08-01", "2026-08-31")
    assert start == "2026-07-31T23:00:00Z"
    assert end == "2026-08-31T23:00:00Z"
    # Winter dates have no offset to apply.
    assert shaping.normalise_period("2026-01-01", "2026-01-02")[0] == "2026-01-01T00:00:00Z"


def test_normalise_period_passes_timestamps_through():
    assert shaping.normalise_period(
        "2026-08-01T00:00:00Z", "2026-08-02T00:00:00Z"
    ) == ("2026-08-01T00:00:00Z", "2026-08-02T00:00:00Z")


def test_normalise_period_rejects_bad_input_locally():
    with pytest.raises(ValueError, match="not a date or timestamp"):
        shaping.normalise_period("yesterday", "2026-08-02T00:00:00Z")
    with pytest.raises(ValueError, match="must be before"):
        shaping.normalise_period("2026-08-02T00:00:00Z", "2026-08-01T00:00:00Z")
    with pytest.raises(ValueError, match="required"):
        shaping.normalise_period("", "2026-08-02T00:00:00Z")
