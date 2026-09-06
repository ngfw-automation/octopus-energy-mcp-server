"""Pure helpers for normalising timestamps, bucketing half-hourly data, and
computing the summary stats the tools return. No I/O, fully unit-testable."""

from __future__ import annotations

import bisect
import re
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any
from zoneinfo import ZoneInfo

# UK bills, tariff windows and "days" are all local: through BST a UTC day
# runs 01:00-01:00 local, so bucketing in UTC silently disagrees with every
# figure the customer can check. Timestamps go to Octopus in UTC; buckets and
# their labels are Europe/London.
LONDON = ZoneInfo("Europe/London")

_DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def parse_dt(value: str) -> datetime:
    """Parse an ISO-8601 timestamp (with ``Z`` or an offset) into aware UTC."""
    s = (value or "").strip()
    if s.endswith("Z") or s.endswith("z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def fmt_z(dt: datetime) -> str:
    """Format an aware datetime as a UTC ``Z`` timestamp."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fmt_local(dt: datetime) -> str:
    """Format an aware datetime in UK local time, with its offset."""
    return dt.astimezone(LONDON).isoformat(timespec="seconds")


def to_utc_z(value: str, end_of_day: bool = False) -> str:
    """Normalise a period bound to an ISO-8601 UTC ``Z`` string.

    A bare date (``2026-08-01``) means UK local midnight, which is what a
    customer means by "1 August" and what their bill buckets by -- so it
    becomes ``2026-07-31T23:00:00Z`` in summer. ``end_of_day`` moves a bare
    date to the following midnight, so ``period_to=2026-08-31`` covers the
    whole of the 31st.
    """
    s = (value or "").strip()
    if not s:
        raise ValueError("period bounds are required (ISO-8601 UTC, or YYYY-MM-DD)")
    if _DATE_ONLY.match(s):
        local = datetime.fromisoformat(s).replace(tzinfo=LONDON)
        if end_of_day:
            naive = local.replace(tzinfo=None) + timedelta(days=1)
            local = naive.replace(tzinfo=LONDON)
        return fmt_z(local)
    try:
        return fmt_z(parse_dt(s))
    except ValueError as exc:
        raise ValueError(
            f"{s!r} is not a date or timestamp. Use YYYY-MM-DD (UK local) or "
            "ISO-8601 UTC like 2026-08-01T00:00:00Z."
        ) from exc


def seconds_until_next_publish(hour: int = 16, floor: int = 300) -> int:
    """Seconds until the next Agile publish (about 4pm UK local).

    Next-day Agile and Agile Outgoing rates land between 4pm and 8pm, so a
    cache holding a forward-looking window should expire around then rather
    than sitting on yesterday's answer for a day.
    """
    now = datetime.now(LONDON)
    target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if target <= now:
        naive = target.replace(tzinfo=None) + timedelta(days=1)
        target = naive.replace(tzinfo=LONDON)
    delta = target.astimezone(timezone.utc) - now.astimezone(timezone.utc)
    return max(floor, int(delta.total_seconds()))


def normalise_period(period_from: str, period_to: str) -> tuple[str, str]:
    """Both period bounds as UTC ``Z`` strings, validated before any request.

    Catches reversed and unparseable ranges here rather than spending a round
    trip to have Octopus reject them with a message about the wrong field.
    """
    start = to_utc_z(period_from)
    end = to_utc_z(period_to, end_of_day=True)
    if parse_dt(start) >= parse_dt(end):
        raise ValueError(
            f"period_from ({start}) must be before period_to ({end})."
        )
    return start, end


def bucket_start(dt: datetime, group_by: str) -> datetime:
    """Floor a timestamp to the start of its bucket, in UK local time."""
    local = dt.astimezone(LONDON)
    if group_by == "half_hour":
        minute = 0 if local.minute < 30 else 30
        return local.replace(minute=minute, second=0, microsecond=0)
    if group_by == "hour":
        return local.replace(minute=0, second=0, microsecond=0)
    if group_by == "day":
        return local.replace(hour=0, minute=0, second=0, microsecond=0)
    if group_by == "week":
        start = local.replace(hour=0, minute=0, second=0, microsecond=0)
        return start - timedelta(days=start.weekday())  # Monday
    if group_by == "month":
        return local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    raise ValueError(f"unsupported group_by: {group_by}")


def bucket_end(start: datetime, group_by: str) -> datetime:
    """The nominal end of a bucket given its start.

    Day, week and month advance in local wall time, so the clocks-change days
    are correctly 23 and 25 hours long rather than a fixed 24.
    """
    if group_by == "half_hour":
        return start + timedelta(minutes=30)
    if group_by == "hour":
        return start + timedelta(hours=1)
    naive = start.astimezone(LONDON).replace(tzinfo=None)
    if group_by == "day":
        naive += timedelta(days=1)
    elif group_by == "week":
        naive += timedelta(days=7)
    elif group_by == "month":
        year, month = naive.year, naive.month + 1
        if month == 13:
            month, year = 1, year + 1
        naive = naive.replace(year=year, month=month)
    else:
        raise ValueError(f"unsupported group_by: {group_by}")
    return naive.replace(tzinfo=LONDON)


def summarize(
    rows: list[dict[str, Any]],
    period_from: str,
    period_to: str,
    group_by: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Aggregate half-hourly rows into ``group_by`` buckets and compute stats.

    Buckets are UK local (Europe/London), so a "day" is the day the bill
    charges for, and series timestamps carry their offset. Returns
    ``(series, stats)`` where each entry is ``{start, end, value}`` and stats
    carry totals, min/max/mean, the peak slot, and the count of missing
    half-hour intervals in the requested range.
    """
    values = [float(r["consumption"]) for r in rows]
    n = len(values)
    total = sum(values)

    buckets: dict[datetime, float] = {}
    for r in rows:
        key = bucket_start(parse_dt(r["interval_start"]), group_by)
        buckets[key] = buckets.get(key, 0.0) + float(r["consumption"])

    series = [
        {
            "start": fmt_local(key),
            "end": fmt_local(bucket_end(key, group_by)),
            "value": round(buckets[key], 3),
        }
        for key in sorted(buckets)
    ]

    if n:
        peak = max(rows, key=lambda r: float(r["consumption"]))
        try:
            expected = int(
                round((parse_dt(period_to) - parse_dt(period_from)) / timedelta(minutes=30))
            )
        except Exception:
            expected = n
        stats = {
            "total": round(total, 3),
            "mean": round(total / n, 3),
            "min": round(min(values), 3),
            "max": round(max(values), 3),
            "n": n,
            "peak_period": fmt_local(parse_dt(peak["interval_start"])),
            "missing_intervals": max(0, expected - n),
        }
    else:
        stats = {
            "total": 0.0,
            "mean": None,
            "min": None,
            "max": None,
            "n": 0,
            "peak_period": None,
            "missing_intervals": 0,
        }
    return series, stats


def parse_tariff_code(tariff_code: str) -> dict[str, str | None]:
    """Split a tariff code into its parts.

    ``E-1R-AGILE-18-02-21-C`` -> fuel ``E``, registers ``1R``,
    product ``AGILE-18-02-21``, region ``C``.
    """
    parts = (tariff_code or "").split("-")
    if len(parts) < 4:
        return {"fuel": None, "registers": None, "product_code": None, "region": None}
    return {
        "fuel": parts[0],
        "registers": parts[1],
        "product_code": "-".join(parts[2:-1]),
        "region": parts[-1],
    }


def fuel_from_tariff(tariff_code: str) -> str:
    """``E-`` tariffs are electricity, anything else is treated as gas."""
    return "electricity" if (tariff_code or "").startswith("E-") else "gas"


def tariff_path(fuel: str) -> str:
    """The REST path segment for a fuel's tariffs."""
    return "electricity-tariffs" if fuel == "electricity" else "gas-tariffs"


# --- Billing-accurate cost (spec §1.2) ----------------------------------
#
# Octopus bills each half-hour by rounding the interval's consumption to
# 0.01 kWh (banker's rounding) and pricing it at the *exc-VAT* unit rate.
# Those costs are then summed at full precision: rounding each half-hour to a
# whole penny biases the total upwards by ~1% at typical domestic usage
# (~10p per interval, so the fractional pence do not cancel). Only the period
# totals are rounded -- unit cost to the penny, then the standing charge, then
# 5% VAT on their sum. bill_cost() implements exactly that, in Decimal, so
# totals line up with the invoice rather than with a rounding artefact.

_KWH_CENT = Decimal("0.01")
_COST_PRECISION = Decimal("0.0001")  # pence; kept sub-penny until the total
_ONE_PENCE = Decimal("1")
_VAT_RATE = Decimal("0.05")
_VAT_MULTIPLIER = Decimal("1.05")
_SECONDS_PER_DAY = Decimal("86400")


def _dec(value: Any) -> Decimal:
    """Decimal from int/float/str via str(), so 0.1 and 25.1251 stay exact."""
    return value if isinstance(value, Decimal) else Decimal(str(value))


def m3_to_kwh_factor(calorific_value: float | Decimal = 39.5) -> float:
    """kWh per m3 of gas: ``CV x 1.02264 / 3.6`` (spec §1.2).

    A calorific value of 39.5 MJ/m3 -- typical for the UK grid -- gives
    ~11.22 kWh/m3. The exact figure varies by region and period and is
    printed on the gas bill; pass it when you have it.
    """
    return float(_dec(calorific_value) * Decimal("1.02264") / Decimal("3.6"))


def _billing_index(
    rates: list[dict[str, Any]],
) -> dict[str, list[tuple[datetime, datetime | None, Decimal]]]:
    """Index exc-VAT unit rates as ``{payment_method: [(valid_from, valid_to, value)]}``."""
    methods: dict[str, list[tuple[datetime, datetime | None, Decimal]]] = {}
    for r in rates:
        value = r.get("value_exc_vat")
        if value is None or not r.get("valid_from"):
            continue
        pm = (r.get("payment_method") or "").strip().upper()
        methods.setdefault(pm, []).append(
            (
                parse_dt(r["valid_from"]),
                parse_dt(r["valid_to"]) if r.get("valid_to") else None,
                _dec(value),
            )
        )
    for entries in methods.values():
        entries.sort(key=lambda e: e[0])
    return methods


def _billing_rate_at(
    methods: dict[str, list[tuple[datetime, datetime | None, Decimal]]],
    starts_by_pm: dict[str, list[datetime]],
    when: datetime,
    pm: str | None,
) -> tuple[Decimal | None, str | None]:
    """Exc-VAT rate covering ``when`` plus the tier it came from.

    Tier preference mirrors the GBP estimator: the requested payment method,
    then a payment-method-free rate, then the highest other rate (so costs
    are never understated when the method is unpinned).
    """

    def covering(entries: list, starts: list[datetime]) -> Decimal | None:
        i = bisect.bisect_right(starts, when) - 1
        while i >= 0:
            _vf, vt, value = entries[i]
            if vt is None or vt > when:
                return value
            i -= 1
        return None

    if pm:
        value = covering(methods.get(pm, []), starts_by_pm.get(pm, []))
        if value is not None:
            return value, "exact"
    value = covering(methods.get("", []), starts_by_pm.get("", []))
    if value is not None:
        return value, "single"
    best: Decimal | None = None
    for method, entries in methods.items():
        if method in ("", pm):
            continue
        value = covering(entries, starts_by_pm[method])
        if value is not None and (best is None or value > best):
            best = value
    if best is not None:
        return best, "fallback"
    return None, None


def sc_value_exc_vat(row: dict[str, Any]) -> Decimal | None:
    """Ex-VAT pence/day from a standing-charge row.

    The REST endpoint returns ``value_exc_vat`` and ``value_inc_vat`` (never a
    bare ``value`` -- reading that key is what made every standing charge look
    like zero). ``value`` is still accepted last, for older payloads.
    """
    if row.get("value_exc_vat") is not None:
        return _dec(row["value_exc_vat"])
    if row.get("value_inc_vat") is not None:
        return _dec(row["value_inc_vat"]) / _VAT_MULTIPLIER
    if row.get("value") is not None:
        return _dec(row["value"])
    return None


def sc_pence_for_period(
    sc_rows: list[dict[str, Any]] | None,
    period_from: str,
    period_to: str,
    override_p_per_day_inc_vat: float | None = None,
    pm: str | None = None,
) -> tuple[Decimal, str | None, str | None]:
    """Standing charge for ``[period_from, period_to)`` in **ex-VAT pence**.

    API rows are per-day values weighted by their overlap with the period and
    filtered by payment method the same way unit rates are: the requested
    method, else rows carrying no method, else the dearest other method.

    ``override_p_per_day_inc_vat`` -- the VAT-inclusive pence/day figure from
    the dashboard, pinned via ``STANDING_CHARGE_*`` -- wins when it is set,
    because it comes from the account's own bill while the API publishes the
    tariff's generic charge, and the two can differ by a fraction of a penny
    a day. It is converted back to ex-VAT here so the caller can add VAT once,
    at the end.

    Returns ``(pence_exc_vat, note, source)`` with source in
    ``{"api", "configured", None}``.
    """
    lo, hi = parse_dt(period_from), parse_dt(period_to)
    groups: dict[str, Decimal] = {}
    for row in sc_rows or []:
        value = sc_value_exc_vat(row)
        if value is None:
            continue
        vf = parse_dt(row["valid_from"]) if row.get("valid_from") else lo
        vt = parse_dt(row["valid_to"]) if row.get("valid_to") else hi
        overlap_days = max(
            Decimal("0"),
            _dec((min(hi, vt) - max(lo, vf)).total_seconds()) / _SECONDS_PER_DAY,
        )
        if overlap_days <= 0:
            continue
        key = (row.get("payment_method") or "").strip().upper()
        groups[key] = groups.get(key, Decimal("0")) + value * overlap_days

    note: str | None = None
    chosen: Decimal | None = None
    if pm and groups.get(pm):
        chosen = groups[pm]
    elif groups.get(""):
        chosen = groups[""]
    else:
        priced = [(k, v) for k, v in groups.items() if v > 0]
        if priced:
            method, chosen = max(priced, key=lambda kv: kv[1])
            note = (
                f"No standing charge published for payment method {pm}; used the "
                f"{method or 'unlabelled'} one instead."
                if pm
                else "The tariff publishes a standing charge per payment method and "
                "none was pinned; the highest was used (conservative)."
            )
    if override_p_per_day_inc_vat is not None:
        total_days = _dec((hi - lo).total_seconds()) / _SECONDS_PER_DAY
        pence = _dec(override_p_per_day_inc_vat) / _VAT_MULTIPLIER * total_days
        if chosen:
            published = chosen / total_days * _VAT_MULTIPLIER if total_days else Decimal("0")
            override_note = (
                f"Used the configured standing charge ({override_p_per_day_inc_vat} p/day "
                f"inc VAT) rather than the {round(float(published), 4)} p/day the tariff "
                "publishes; clear STANDING_CHARGE_* to use the published value."
            )
        else:
            override_note = (
                "The API published no standing charge for this tariff; used the "
                "configured STANDING_CHARGE_* value (VAT-inclusive pence per day)."
            )
        return pence, override_note, "configured"
    if chosen:
        return chosen, note, "api"
    return Decimal("0"), None, "api" if sc_rows else None


def sc_staleness_note(
    sc_rows: list[dict[str, Any]] | None,
    period_from: str,
    max_age_days: int = 730,
) -> str | None:
    """Warn when the standing charge covering the period was published years ago.

    Some products carry a placeholder the feed never refreshes -- Agile's
    region-A charge is still the 20p/day row dated 2017 -- which makes the
    tariff look cheaper than it bills. The figure is reported as published;
    this just says how old it is.
    """
    lo = parse_dt(period_from)
    published: datetime | None = None
    for row in sc_rows or []:
        value = sc_value_exc_vat(row)
        if not value or not row.get("valid_from"):
            continue
        vf = parse_dt(row["valid_from"])
        vt = parse_dt(row["valid_to"]) if row.get("valid_to") else None
        if vf <= lo and (vt is None or vt > lo):
            published = vf if published is None else max(published, vf)
    if published is None:
        return None
    age_days = (lo - published).days
    if age_days <= max_age_days:
        return None
    return (
        f"The standing charge used here was last published on "
        f"{published.date().isoformat()} ({age_days // 365} years before this "
        "period) and may not be what the tariff actually bills."
    )


def rate_bands(kwh_by_rate: dict[Decimal, Decimal]) -> dict[str, Any] | None:
    """How a tariff's rates actually applied to this consumption.

    ``None`` for a single-rate tariff. For a time-of-use one it reports the
    share of kWh that landed in the cheapest band, which is the number that
    decides whether the tariff is worth switching to -- an unweighted average
    of the published rates says nothing about when the energy was used.
    """
    if len(kwh_by_rate) < 2:
        return None
    total = sum(kwh_by_rate.values())
    if not total:
        return None
    lo, hi = min(kwh_by_rate), max(kwh_by_rate)
    band = lo + Decimal("0.5")  # within half a penny of the cheapest rate
    cheapest_kwh = sum(v for k, v in kwh_by_rate.items() if k <= band)
    return {
        "distinct_rates": len(kwh_by_rate),
        "min_p_kwh_exc_vat": round(float(lo), 4),
        "max_p_kwh_exc_vat": round(float(hi), 4),
        "kwh_in_cheapest_band": round(float(cheapest_kwh), 3),
        "share_in_cheapest_band": round(float(cheapest_kwh / total), 4),
    }


def bill_cost(
    rows: list[dict[str, Any]],
    rates: list[dict[str, Any]],
    period_from: str,
    period_to: str,
    pm: str | None = None,
    sc_rows: list[dict[str, Any]] | None = None,
    sc_override_p_per_day: float | None = None,
) -> dict[str, Any]:
    """Billing-accurate cost of ``rows`` priced at ``rates`` (spec §1.2).

    Per half-hour: consumption rounded to 0.01 kWh (half-even), priced at the
    exc-VAT unit rate, kept to 0.0001p. The period's unit cost is then rounded
    to the penny, the standing charge added (also ex-VAT), and 5% VAT applied
    to the sum.

    Returns a breakdown dict (pence as ints, ``total_gbp`` rounded to 2 dp)
    plus ``unpriced_kwh``, ``sc_source`` and a ``notes`` list. Returns
    ``{"error": "no_rates" | "no_priced_intervals", ...}`` when nothing can
    be priced.
    """
    methods = _billing_index(rates)
    if not methods:
        return {
            "error": "no_rates",
            "message": (
                "The tariff returned no unit rates with an exc-VAT value; "
                "cannot price consumption."
            ),
        }
    starts_by_pm = {m: [e[0] for e in entries] for m, entries in methods.items()}

    unit_pence_exact = Decimal("0")
    billed_kwh = Decimal("0")
    kwh_by_rate: dict[Decimal, Decimal] = {}
    total_kwh = 0.0
    unpriced_kwh = 0.0
    priced_intervals = 0
    used_fallback = False
    notes: list[str] = []
    for r in rows:
        kwh = float(r["consumption"])
        when = parse_dt(r["interval_start"])
        total_kwh += kwh
        value, tier = _billing_rate_at(methods, starts_by_pm, when, pm)
        if value is None:
            unpriced_kwh += kwh
            continue
        if tier == "fallback":
            used_fallback = True
        kwh_billed = _dec(kwh).quantize(_KWH_CENT, rounding=ROUND_HALF_EVEN)
        billed_kwh += kwh_billed
        kwh_by_rate[value] = kwh_by_rate.get(value, Decimal("0")) + kwh_billed
        unit_pence_exact += (kwh_billed * value).quantize(
            _COST_PRECISION, rounding=ROUND_HALF_EVEN
        )
        priced_intervals += 1
    if rows and not priced_intervals:
        return {
            "error": "no_priced_intervals",
            "message": "No consumption interval fell inside a rate's validity window.",
        }

    sc_exact, sc_note, sc_source = sc_pence_for_period(
        sc_rows, period_from, period_to, sc_override_p_per_day, pm
    )
    if sc_note:
        notes.append(sc_note)
    if sc_source == "api":
        stale = sc_staleness_note(sc_rows, period_from)
        if stale:
            notes.append(stale)
    if unpriced_kwh:
        notes.append(
            f"{round(unpriced_kwh, 3)} kWh had no matching rate and are "
            "excluded from the total."
        )
    if used_fallback:
        if pm:
            notes.append(
                f"Part of the period has no rate for payment method {pm}; "
                "the available rate was used instead."
            )
        else:
            notes.append(
                "The tariff has multiple payment-method rates and no payment "
                "method was pinned; the highest rate was used (conservative)."
            )

    unit_pence = int(unit_pence_exact.quantize(_ONE_PENCE, rounding=ROUND_HALF_EVEN))
    sc_pence = int(sc_exact.quantize(_ONE_PENCE, rounding=ROUND_HALF_EVEN))
    subtotal = unit_pence + sc_pence
    vat = int((_dec(subtotal) * _VAT_RATE).quantize(_ONE_PENCE, rounding=ROUND_HALF_EVEN))
    total = subtotal + vat
    return {
        "total_kwh": round(total_kwh, 3),
        "billed_kwh": float(billed_kwh),
        "rate_bands": rate_bands(kwh_by_rate),
        "effective_p_kwh_exc_vat": (
            round(float(unit_pence_exact / billed_kwh), 4) if billed_kwh else None
        ),
        "unit_cost_pence": unit_pence,
        "standing_charge_pence": sc_pence,
        "subtotal_ex_vat_pence": subtotal,
        "vat_5pct_pence": vat,
        "total_inc_vat_pence": total,
        "total_gbp": round(total / 100, 2),
        "sc_source": sc_source,
        "unpriced_kwh": round(unpriced_kwh, 3),
        "notes": notes,
    }
