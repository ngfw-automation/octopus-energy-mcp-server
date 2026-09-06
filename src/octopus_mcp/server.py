"""MCP server exposing read-only Octopus Energy consumption + tariff tools."""

from __future__ import annotations

import bisect
import os
import sys
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Annotated, Any, Literal, Optional
from zoneinfo import ZoneInfo

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import Field

from . import __version__, observability, shaping
from .config import Settings
from .config import get_settings as process_settings
from .rest import OctopusAPIError, OctopusAuthError, OctopusNotFoundError, OctopusREST

mcp = MCPServer(
    "Octopus Energy",
    version=__version__,
    instructions=(
        "Read-only access to Octopus Energy (UK) consumer data: half-hourly "
        "electricity and gas consumption, plus tariff unit rates and standing "
        "charges. Every tool is read-only; none of them modify the account."
    ),
    middleware=[observability.LoggingMiddleware()],
)

# Every period argument accepts either form; normalise_period() resolves them.
_PERIOD_FROM = (
    "Start. A date (2026-08-01) is read as UK local midnight; a UTC timestamp "
    "(2026-08-01T00:00:00Z) is used as-is."
)
_PERIOD_TO = (
    "End. A date (2026-08-31) covers that whole day in UK local time; a UTC "
    "timestamp is an exclusive bound."
)

# UK local time for get_current_datetime (follows BST/GMT automatically).
_LONDON_TZ = ZoneInfo("Europe/London")


@dataclass
class ServerContext:
    """The credential and settings a request is served with.

    One process still serves one account, but the tools reach their settings
    and HTTP client through here rather than through module globals, so a
    per-session credential is a matter of setting the context var rather than
    a rewrite. Cache keys already carry the credential's fingerprint.
    """

    settings: Settings
    rest: OctopusREST


_context: ContextVar[Optional[ServerContext]] = ContextVar("octopus_context", default=None)
_process_context: Optional[ServerContext] = None


def context() -> ServerContext:
    """The context for this request, falling back to the process-wide one."""
    ctx = _context.get()
    if ctx is not None:
        return ctx
    global _process_context
    if _process_context is None:
        settings = process_settings()
        _process_context = ServerContext(
            settings=settings, rest=OctopusREST(api_key=settings.octopus_api_key)
        )
    return _process_context


def set_context(ctx: Optional[ServerContext]):
    """Serve the current task from ``ctx``; returns the token to reset with."""
    return _context.set(ctx)


def get_settings() -> Settings:
    return context().settings


def rest() -> OctopusREST:
    return context().rest


async def get_account() -> dict[str, Any]:
    s = get_settings()
    return await rest().get(
        f"/accounts/{s.octopus_account_number}/", auth=True, ttl=s.cache_ttl_account
    )


def _err(e: Exception) -> dict[str, Any]:
    if isinstance(e, OctopusAuthError):
        code = "auth"
    elif isinstance(e, OctopusNotFoundError):
        code = "not_found"
    elif isinstance(e, OctopusAPIError):
        code = "api"
    else:
        code = "error"
    return {"error": code, "status": getattr(e, "status", None), "message": str(e)}


# --- Account / meter-point parsing ---------------------------------------


def _one_line(prop: dict[str, Any]) -> str:
    """One-line address from the fields ``/accounts/`` actually returns.

    They sit on the property itself as ``address_line_1..3`` / ``town`` /
    ``county`` / ``postcode`` -- there is no nested ``address`` object, which
    is why this used to come back empty.
    """
    parts = [
        prop.get("address_line_1"),
        prop.get("address_line_2"),
        prop.get("address_line_3"),
        prop.get("town"),
        prop.get("county"),
        prop.get("postcode"),
    ]
    return ", ".join(p.strip() for p in parts if isinstance(p, str) and p.strip())


def _direction(mp: dict[str, Any]) -> str:
    """IMPORT or EXPORT for a meter point.

    The account payload flags export with ``is_export``; it has no
    ``direction`` field, so keying on that treated an export MPAN as another
    import meter.
    """
    if mp.get("direction"):
        return str(mp["direction"]).upper()
    return "EXPORT" if mp.get("is_export") else "IMPORT"


def _region(mp: dict[str, Any], agreements: list[dict[str, Any]]) -> Optional[str]:
    """The GSP region letter (A-P).

    The meter point does not carry one, so it comes from the tariff code
    suffix of the newest agreement (``E-1R-VAR-22-11-01-A`` -> ``A``).
    """
    for key in ("region", "gsp"):
        value = mp.get(key)
        if value:
            return str(value).lstrip("_")
    for a in agreements:
        region = shaping.parse_tariff_code(a.get("tariff_code") or "")["region"]
        if region and len(region) == 1 and region.isalpha():
            return region.upper()
    return None


def _norm(fuel: str, id_key: str, prop: dict, mp: dict, address: str) -> dict[str, Any]:
    meters = [
        {
            "serial_number": m.get("serial_number"),
            "smets": m.get("smets"),
            "meter_status": m.get("meter_status"),
            "registers": m.get("registers", []),
        }
        for m in mp.get("meters", [])
    ]
    agreements = []
    for a in mp.get("agreements", []):
        tc = a.get("tariff_code")
        parsed = shaping.parse_tariff_code(tc)
        agreements.append(
            {
                "tariff_code": tc,
                "product_code": parsed["product_code"],
                "valid_from": a.get("valid_from"),
                "valid_to": a.get("valid_to"),
                "active": a.get("valid_to") is None,
            }
        )
    agreements.sort(key=lambda a: a.get("valid_from") or "", reverse=True)
    out: dict[str, Any] = {
        "property_id": prop.get("id"),
        "address": address,
        "fuel": fuel,
        "region": _region(mp, agreements),
        "direction": _direction(mp),
        "meters": meters,
        "agreements": agreements,
    }
    # Estimated annual consumption and profile class, when the payload has
    # them: both are useful for sizing a tariff comparison.
    for key in ("profile_class", "consumption_standard"):
        if mp.get(key) is not None:
            out[key] = mp[key]
    out[id_key] = mp.get(id_key)
    return out


def iter_meter_points(account: dict[str, Any]):
    for prop in account.get("properties", []):
        address = _one_line(prop)
        for mp in prop.get("electricity_meter_points", []):
            yield _norm("electricity", "mpan", prop, mp, address)
        for mp in prop.get("gas_meter_points", []):
            yield _norm("gas", "mprn", prop, mp, address)


def _pinned(fuel: str, direction: str) -> tuple[Optional[str], Optional[str]]:
    """The (meter id, serial) pinned in .env for this fuel/direction, if any."""
    s = get_settings()
    if fuel == "gas":
        return s.octopus_gas_mprn, s.octopus_gas_serial
    if direction == "EXPORT":
        return s.octopus_export_mpan, None
    return s.octopus_electricity_mpan, s.octopus_electricity_serial


def resolve_meter(
    points: list[dict[str, Any]],
    fuel: str,
    mpan: Optional[str] = None,
    mprn: Optional[str] = None,
    serial: Optional[str] = None,
    direction: str = "IMPORT",
) -> tuple[Optional[dict[str, Any]], Optional[dict[str, Any]]]:
    """Pick the meter point + serial for a fuel, or explain why it can't.

    Returns ``(resolved, problem)``. ``resolved`` is ``{"point", "serial"}``
    when a single meter was identified; otherwise ``problem`` is the error
    dict the tool should return -- a disambiguation list rather than a guess
    when the account has more than one meter (spec §2.8).

    An argument wins over the ``OCTOPUS_*_MPAN`` / ``_SERIAL`` pins, which win
    over "the account only has one".
    """
    id_key = "mpan" if fuel == "electricity" else "mprn"
    pinned_id, pinned_serial = _pinned(fuel, direction)
    explicit = (mpan if fuel == "electricity" else mprn) or pinned_id
    serial = serial or pinned_serial

    cands = [p for p in points if p["fuel"] == fuel and p.get("direction") == direction]
    if not cands:
        noun = "export" if direction == "EXPORT" else fuel
        return None, {
            "error": "no_meter",
            "message": f"No {noun} meter point found on this account.",
        }
    if explicit:
        matching = [p for p in cands if p.get(id_key) == explicit]
        if not matching:
            return None, {
                "error": "unknown_meter",
                "message": f"No {fuel} meter point with {id_key} {explicit} on this account.",
                "options": [{id_key: p.get(id_key), "address": p["address"]} for p in cands],
            }
        cands = matching
    elif len(cands) > 1:
        return None, {
            "error": "multiple_meter_points",
            "message": (
                f"This account has {len(cands)} {fuel} meter points; pass {id_key} "
                f"(or pin one in .env) to choose."
            ),
            "options": [
                {
                    id_key: p.get(id_key),
                    "address": p["address"],
                    "region": p.get("region"),
                    "tariff_code": (p["agreements"][0]["tariff_code"] if p["agreements"] else None),
                }
                for p in cands
            ],
        }

    point = cands[0]
    meters = sorted(
        point["meters"],
        key=lambda m: 0 if m.get("meter_status") in (None, "ACTIVE") else 1,
    )
    if serial:
        matching = [m for m in meters if m["serial_number"] == serial]
        if not matching:
            return None, {
                "error": "unknown_serial",
                "message": (
                    f"No meter with serial {serial} on {id_key} {point.get(id_key)}. "
                    "Previously this silently used another meter."
                ),
                "options": [m["serial_number"] for m in meters],
            }
        meters = matching
    if not meters:
        return None, {
            "error": "no_serial",
            "message": f"No meter serial available for this {id_key}.",
        }
    return {"point": point, "serial": meters[0]["serial_number"]}, None


async def _fetch_consumption(
    fuel: str, idv: str, serial: str, period_from: str, period_to: str
) -> list[dict[str, Any]]:
    s = get_settings()
    if fuel == "electricity":
        url = f"/electricity-meter-points/{idv}/meters/{serial}/consumption/"
    else:
        url = f"/gas-meter-points/{idv}/meters/{serial}/consumption/"
    params = {
        "period_from": period_from,
        "period_to": period_to,
        "order_by": "period",
        "page_size": 25000,
    }
    rows = await rest().get_all(
        url,
        params=params,
        auth=True,
        ttl=s.cache_ttl_consumption,
        max_pages=25,
        row_cap=120000,
    )
    # The API treats period_to as inclusive: it also returns the interval that
    # starts exactly at period_to. Clamp to the documented [from, to) so
    # adjacent periods tile without double-counting the boundary interval.
    lo = shaping.parse_dt(period_from)
    hi = shaping.parse_dt(period_to)
    clamped: list[dict[str, Any]] = []
    for r in rows:
        try:
            st = shaping.parse_dt(r.get("interval_start"))
        except ValueError:
            clamped.append(r)
            continue
        if lo <= st < hi:
            clamped.append(r)
    return clamped


def _truncate(series: list[dict[str, Any]], notes: list[str]) -> list[dict[str, Any]]:
    """Cap a series at MAX_ROWS_RETURNED, saying so."""
    cap = get_settings().max_rows_returned
    if len(series) > cap:
        notes.append(f"Series truncated to {cap} buckets; stats cover the full range.")
        return series[:cap]
    return series


def _raw_too_wide(period_from: str, period_to: str) -> Optional[dict[str, Any]]:
    """Refuse include_raw over a range that would dump thousands of rows.

    One row per half hour is ~1,500 for a month, which is tens of thousands of
    tokens in a single tool result (spec §2.6 caps raw at a couple of days).
    """
    cap = get_settings().max_raw_days
    days = (
        shaping.parse_dt(period_to) - shaping.parse_dt(period_from)
    ).total_seconds() / 86400.0
    if days <= cap:
        return None
    return {
        "error": "range_too_wide_for_raw",
        "message": (
            f"include_raw returns one row per half hour -- {days:.0f} days is about "
            f"{days * 48:.0f} rows. The limit is {cap} day(s) (MAX_RAW_DAYS). Use "
            "group_by=half_hour without include_raw for aggregated slots, or a "
            "coarser group_by for a wider range."
        ),
    }


def _raw_series(rows: list[dict[str, Any]], notes: list[str]) -> list[dict[str, Any]]:
    """Half-hourly rows as a series, timestamped in UK local time."""
    series = [
        {
            "start": shaping.fmt_local(shaping.parse_dt(r["interval_start"])),
            "end": (
                shaping.fmt_local(shaping.parse_dt(r["interval_end"]))
                if r.get("interval_end")
                else None
            ),
            "value": round(float(r["consumption"]), 3),
        }
        for r in rows
    ]
    return _truncate(series, notes)


async def _consumption_result(
    fuel: str,
    point: dict[str, Any],
    id_key: str,
    idv: Optional[str],
    serial_v: Optional[str],
    rows: list[dict[str, Any]],
    period_from: str,
    period_to: str,
    group_by: str,
    unit: str,
    include_raw: bool,
    notes: list[str],
    allow_sc_override: bool = True,
) -> dict[str, Any]:
    """The response body shared by the electricity, gas and export tools."""
    if unit == "GBP":
        result = await _cost_series(
            fuel,
            point,
            rows,
            period_from,
            period_to,
            group_by,
            notes,
            allow_sc_override=allow_sc_override,
        )
        if "error" not in result:
            result[id_key] = idv
            result["serial"] = serial_v
        return result

    if include_raw and group_by == "half_hour":
        problem = _raw_too_wide(period_from, period_to)
        if problem:
            return problem
        _, stats = shaping.summarize(rows, period_from, period_to, "half_hour")
        notes.append("Raw half-hourly rows.")
        return {
            "unit": "kWh",
            "group_by": "half_hour",
            "timezone": "Europe/London",
            "include_raw": True,
            id_key: idv,
            "serial": serial_v,
            "total": stats["total"],
            "series": _raw_series(rows, notes),
            "stats": stats,
            "notes": " ".join(notes) or None,
        }

    series, stats = shaping.summarize(rows, period_from, period_to, group_by)
    if stats["missing_intervals"]:
        notes.append(f"{stats['missing_intervals']} half-hour interval(s) missing in range.")
    return {
        "unit": "kWh",
        "group_by": group_by,
        "timezone": "Europe/London",
        id_key: idv,
        "serial": serial_v,
        "total": stats["total"],
        "series": _truncate(series, notes),
        "stats": stats,
        "notes": " ".join(notes) or None,
    }


def _no_data(group_by: str, unit: str = "kWh") -> dict[str, Any]:
    return {
        "unit": unit,
        "group_by": group_by,
        "total": 0.0,
        "series": [],
        "stats": {
            "total": 0.0,
            "mean": None,
            "min": None,
            "max": None,
            "n": 0,
            "peak_period": None,
            "missing_intervals": 0,
        },
        "notes": (
            "No consumption data returned for this range. Settlement data can lag "
            "by up to a day; try an earlier period_to."
        ),
    }


# --- Cost (GBP) helpers ----------------------------------------------------


def _norm_pm(value: Optional[str]) -> Optional[str]:
    v = (value or "").strip().upper()
    return v or None


def _rate_index(
    rates: list[dict[str, Any]],
) -> dict[str, list[tuple[datetime, Optional[datetime], float]]]:
    """Index unit-rate rows by payment method for fast per-interval lookup.

    Returns ``{payment_method ("") or none: [(valid_from, valid_to|None,
    value_inc_vat), ...]}`` with each list sorted by ``valid_from``.
    """
    methods: dict[str, list[tuple[datetime, Optional[datetime], float]]] = {}
    for r in rates:
        value = r.get("value_inc_vat")
        if value is None:
            value = r.get("value_exc_vat")
        if value is None or not r.get("valid_from"):
            continue
        pm = (r.get("payment_method") or "").strip().upper()
        methods.setdefault(pm, []).append(
            (
                shaping.parse_dt(r["valid_from"]),
                shaping.parse_dt(r["valid_to"]) if r.get("valid_to") else None,
                float(value),
            )
        )
    for entries in methods.values():
        entries.sort(key=lambda e: e[0])
    return methods


def _first_covering(
    entries: list[tuple[datetime, Optional[datetime], float]],
    starts: list[datetime],
    when: datetime,
) -> Optional[float]:
    """Value of the latest rate whose validity window contains ``when``."""
    i = bisect.bisect_right(starts, when) - 1
    while i >= 0:
        vf, vt, value = entries[i]  # noqa: F841  (vf checked via bisect)
        if vt is None or vt > when:
            return value
        i -= 1
    return None


def _rate_at(
    methods: dict[str, list],
    starts_by_pm: dict[str, list[datetime]],
    when: datetime,
    pm: Optional[str],
) -> tuple[Optional[float], Optional[str]]:
    """Unit rate (p/kWh incl. VAT) valid at ``when``, plus which tier supplied it.

    Preference: exact ``pm`` match (``"exact"``) > single-price rate with no
    payment method (``"single"``) > any other method, highest value
    (``"fallback"``, so costs are never understated).
    """
    if pm:
        value = _first_covering(methods.get(pm, []), starts_by_pm.get(pm, []), when)
        if value is not None:
            return value, "exact"
    value = _first_covering(methods.get("", []), starts_by_pm.get("", []), when)
    if value is not None:
        return value, "single"
    best: Optional[float] = None
    for method, entries in methods.items():
        if method in ("", pm):
            continue
        value = _first_covering(entries, starts_by_pm[method], when)
        if value is not None and (best is None or value > best):
            best = value
    if best is not None:
        return best, "fallback"
    return None, None


async def _cost_series(
    fuel: str,
    point: dict[str, Any],
    rows: list[dict[str, Any]],
    period_from: str,
    period_to: str,
    group_by: str,
    notes: list[str],
    allow_sc_override: bool = True,
) -> dict[str, Any]:
    """Price half-hourly consumption rows against the meter point's tariff.

    ``rows`` must already be in kWh. Returns a full result dict with values in
    GBP, or an error dict.
    """
    s = get_settings()
    agreements = point.get("agreements") or []
    if not agreements:
        return {
            "error": "no_tariff",
            "message": (
                "No tariff agreement found for this meter point, so consumption "
                "cannot be priced."
            ),
        }

    t_from = shaping.parse_dt(period_from)
    covering = [
        a
        for a in agreements
        if a.get("valid_from")
        and shaping.parse_dt(a["valid_from"]) <= t_from
        and (a.get("valid_to") is None or shaping.parse_dt(a["valid_to"]) > t_from)
    ]
    if covering:
        ag = max(covering, key=lambda a: shaping.parse_dt(a["valid_from"]))
    else:
        ag = next((a for a in agreements if a.get("active")), agreements[-1])
    product_code = ag.get("product_code")
    tariff_code = ag.get("tariff_code")
    if not product_code or not tariff_code:
        return {
            "error": "no_tariff",
            "message": "Tariff agreement is missing product/tariff codes; cannot look up rates.",
        }

    rates, sc_rows = await _fetch_tariff_pricing(
        product_code, tariff_code, period_from, period_to
    )

    pm = _norm_pm(s.default_payment_method)
    methods = _rate_index(rates)
    if not methods:
        return {
            "error": "no_rates",
            "message": (
                "The tariff returned no unit rates for this period; consumption "
                "cannot be priced."
            ),
        }
    starts_by_pm = {m: [e[0] for e in entries] for m, entries in methods.items()}

    priced: list[dict[str, Any]] = []
    total_kwh = 0.0
    unpriced_kwh = 0.0
    used_fallback = False
    for r in rows:
        kwh = float(r["consumption"])
        when = shaping.parse_dt(r["interval_start"])
        total_kwh += kwh
        rate, tier = _rate_at(methods, starts_by_pm, when, pm)
        if rate is None:
            unpriced_kwh += kwh
            continue
        if tier == "fallback":
            used_fallback = True
        priced.append({**r, "consumption": kwh * rate / 100.0})
    if not priced:
        return {
            "error": "no_priced_intervals",
            "message": "No consumption interval fell inside a rate's validity window.",
        }

    series, stats = shaping.summarize(priced, period_from, period_to, group_by)
    if unpriced_kwh:
        notes.append(
            f"{round(unpriced_kwh, 3)} kWh had no matching rate and are excluded from the total."
        )
    if used_fallback:
        if pm:
            notes.append(
                f"No rate for payment method {pm} on some intervals; the available rate was used instead."
            )
        else:
            notes.append(
                "The tariff has multiple payment-method rates and DEFAULT_PAYMENT_METHOD "
                "is not set; the highest rate was used (conservative). Set it in .env to pin your rate."
            )
    # The standing charge is part of what the period cost, so it belongs in
    # the total even though it cannot be attributed to a consumption bucket.
    sc_override = (
        s.standing_charge_electricity if fuel == "electricity" else s.standing_charge_gas
    )
    if not allow_sc_override:
        sc_override = None  # an export tariff has no import standing charge
    sc_exc, sc_note, sc_source = shaping.sc_pence_for_period(
        sc_rows, period_from, period_to, sc_override, pm
    )
    if sc_note:
        notes.append(sc_note)
    unit_cost_gbp = round(stats["total"], 2)
    standing_charge_gbp = round(float(sc_exc) * 1.05 / 100, 2)
    total_gbp = round(unit_cost_gbp + standing_charge_gbp, 2)
    notes.append(
        "series and stats are unit cost only; total adds the standing charge. "
        "calculate_cost gives the invoice-accurate breakdown."
    )

    max_rows = s.max_rows_returned
    if len(series) > max_rows:
        series = series[:max_rows]
        notes.append(f"Series truncated to {max_rows} buckets; stats cover the full range.")
    return {
        "unit": "GBP",
        "group_by": group_by,
        "unit_cost_gbp": unit_cost_gbp,
        "standing_charge_gbp": standing_charge_gbp,
        "standing_charge_source": sc_source,
        "total": total_gbp,
        "total_gbp": total_gbp,
        "total_kwh": round(total_kwh, 3),
        "series": series,
        "stats": stats,
        "tariff_code": tariff_code,
        "product_code": product_code,
        "payment_method": pm,
        "notes": " ".join(notes) or None,
    }


# --- Tools ----------------------------------------------------------------


@mcp.tool()
def get_current_datetime() -> dict[str, Any]:
    """Current date and time in both UTC and Europe/London (UK local), each
    labelled with its timezone name and UTC offset.

    Use this to resolve relative dates ("today", "this week", "last month")
    before calling the consumption/rate tools, and to know which UTC offset
    UK local times use right now (GMT = UTC+0 in winter, BST = UTC+1 in
    summer). All other tools take and return UTC.
    """
    now_utc = datetime.now(timezone.utc)
    now_london = now_utc.astimezone(_LONDON_TZ)

    def _offset(dt: datetime) -> str:
        off = dt.strftime("%z")  # e.g. "0100"
        return f"{off[:3]}:{off[3:]}"

    return {
        "utc": {
            "date": now_utc.strftime("%Y-%m-%d"),
            "weekday": now_utc.strftime("%A"),
            "time": now_utc.strftime("%H:%M:%S"),
            "datetime": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "timezone": "UTC",
            "utc_offset": "+00:00",
        },
        "europe_london": {
            "date": now_london.strftime("%Y-%m-%d"),
            "weekday": now_london.strftime("%A"),
            "time": now_london.strftime("%H:%M:%S"),
            "datetime": now_london.isoformat(timespec="seconds"),
            "timezone": "Europe/London",
            "tz_abbreviation": now_london.strftime("%Z") or None,
            "utc_offset": _offset(now_london),
        },
        "notes": (
            "Europe/London follows UK DST: BST (UTC+1) from late March to late "
            "October, GMT (UTC+0) otherwise. period_from/period_to for the other "
            "tools must be UTC (trailing Z)."
        ),
    }


@mcp.tool()
async def list_meter_points(
    property_id: Annotated[
        Optional[str], Field(description="Only include this property id.")
    ] = None,
) -> dict[str, Any]:
    """List every meter point (electricity MPANs and gas MPRNs) with meters, region, and tariff agreements."""
    try:
        account = await get_account()
        points = list(iter_meter_points(account))
        if property_id:
            # The API's property id is an integer; comparing it to the string
            # the caller passes matched nothing.
            points = [p for p in points if str(p["property_id"]) == str(property_id)]
        return {
            "account": get_settings().octopus_account_number,
            "count": len(points),
            "meter_points": points,
        }
    except (OctopusAuthError, OctopusNotFoundError, OctopusAPIError) as e:
        return _err(e)


@mcp.tool()
async def get_agreements(
    mpan: Annotated[Optional[str], Field(description="Electricity MPAN to filter by.")] = None,
    mprn: Annotated[Optional[str], Field(description="Gas MPRN to filter by.")] = None,
    include_historical: Annotated[
        bool, Field(description="Include expired agreements.")
    ] = True,
) -> dict[str, Any]:
    """Tariff agreements (current + historical) per meter point, with the
    derived product_code needed to query rates."""
    try:
        account = await get_account()
        points = list(iter_meter_points(account))
        if mpan:
            points = [p for p in points if p.get("mpan") == mpan]
        if mprn:
            points = [p for p in points if p.get("mprn") == mprn]
        out = []
        for p in points:
            ags = p["agreements"]
            if not include_historical:
                ags = [a for a in ags if a["active"]]
            out.append(
                {
                    "fuel": p["fuel"],
                    "mpan": p.get("mpan"),
                    "mprn": p.get("mprn"),
                    "address": p["address"],
                    "region": p.get("region"),
                    "agreements": ags,
                }
            )
        return {"count": len(out), "meter_points": out}
    except (OctopusAuthError, OctopusNotFoundError, OctopusAPIError) as e:
        return _err(e)


@mcp.tool()
async def get_electricity_consumption(
    period_from: Annotated[
        str, Field(description=_PERIOD_FROM)
    ],
    period_to: Annotated[
        str, Field(description=_PERIOD_TO)
    ],
    group_by: Annotated[
        Literal["half_hour", "hour", "day", "week", "month"],
        Field(description="Bucket size; half_hour (30 min) is the finest granularity."),
    ] = "day",
    unit: Annotated[
        Literal["kWh", "GBP"],
        Field(
            description=(
                "kWh (energy used) or GBP (estimated cost, each half-hour interval "
                "joined to its tariff rate). Defaults to kWh."
            )
        ),
    ] = "kWh",
    mpan: Annotated[
        Optional[str], Field(description="13-digit MPAN; defaults to the configured/sole meter.")
    ] = None,
    serial: Annotated[
        Optional[str], Field(description="Meter serial; defaults to the first active meter.")
    ] = None,
    include_raw: Annotated[
        bool, Field(description="Return raw half-hourly rows (only sensible for small ranges).")
    ] = False,
) -> dict[str, Any]:
    """Electricity consumption for a meter over a period, aggregated with
    statistics. Finest granularity is half-hourly (30 min).

    With ``unit="GBP"`` each half-hour interval is priced at its tariff rate
    (payment method from ``DEFAULT_PAYMENT_METHOD`` in .env) and the buckets are
    in pounds; ``total_kwh`` is also returned alongside the cost.
    """
    try:
        period_from, period_to = shaping.normalise_period(period_from, period_to)
        points = list(iter_meter_points(await get_account()))
        resolved, problem = resolve_meter(points, "electricity", mpan=mpan, serial=serial)
        if problem:
            return problem
        idv = resolved["point"].get("mpan")
        serial_v = resolved["serial"]

        rows = [
            r
            for r in await _fetch_consumption("electricity", idv, serial_v, period_from, period_to)
            if r.get("consumption") is not None
        ]
        if not rows:
            return _no_data(group_by, unit)

        return await _consumption_result(
            "electricity", resolved["point"], "mpan", idv, serial_v, rows,
            period_from, period_to, group_by, unit, include_raw, [],
        )
    except (OctopusAuthError, OctopusNotFoundError, OctopusAPIError) as e:
        return _err(e)
    except (ValueError, KeyError) as e:
        return {"error": "bad_input", "message": str(e)}


@mcp.tool()
async def get_gas_consumption(
    period_from: Annotated[str, Field(description=_PERIOD_FROM)],
    period_to: Annotated[str, Field(description=_PERIOD_TO)],
    group_by: Annotated[
        Literal["half_hour", "hour", "day", "week", "month"],
        Field(description="Bucket size; half_hour is the finest."),
    ] = "day",
    unit: Annotated[
        Literal["kWh", "GBP"],
        Field(
            description=(
                "kWh (energy used) or GBP (estimated cost, each half-hour interval "
                "joined to its tariff rate). Defaults to kWh."
            )
        ),
    ] = "kWh",
    mprn: Annotated[
        Optional[str], Field(description="Gas MPRN; defaults to the configured/sole meter.")
    ] = None,
    serial: Annotated[
        Optional[str], Field(description="Meter serial; defaults to the first active meter.")
    ] = None,
    source_unit: Annotated[
        Optional[str],
        Field(
            description=(
                "What the meter reports: kWh (SMETS1) or m3 (SMETS2). Defaults to "
                "OCTOPUS_GAS_UNITS from .env, then to kWh with a warning -- the "
                "account payload does not say which it is."
            )
        ),
    ] = None,
    calorific_value: Annotated[
        Optional[float],
        Field(
            description=(
                "Gas calorific value in MJ/m3 for the m3 -> kWh conversion, from "
                "your bill. Defaults to GAS_CALORIFIC_VALUE (39.5, ~11.22 kWh/m3)."
            )
        ),
    ] = None,
    kwh_per_m3: Annotated[
        Optional[float],
        Field(description="Explicit m3 -> kWh factor, overriding calorific_value."),
    ] = None,
    include_raw: Annotated[
        bool, Field(description="Return raw half-hourly rows (small ranges only).")
    ] = False,
) -> dict[str, Any]:
    """Gas consumption for a meter over a period, aggregated with statistics.
    Meters that report m3 (SMETS2) are converted to kWh when the unit is known.

    With ``unit="GBP"`` each half-hour interval is priced at its tariff rate
    (payment method from ``DEFAULT_PAYMENT_METHOD`` in .env) and the buckets are
    in pounds; ``total_kwh`` is also returned alongside the cost.
    """
    try:
        period_from, period_to = shaping.normalise_period(period_from, period_to)
        points = list(iter_meter_points(await get_account()))
        resolved, problem = resolve_meter(points, "gas", mprn=mprn, serial=serial)
        if problem:
            return problem
        idv = resolved["point"].get("mprn")
        serial_v = resolved["serial"]

        rows = [
            r
            for r in await _fetch_consumption("gas", idv, serial_v, period_from, period_to)
            if r.get("consumption") is not None
        ]
        if not rows:
            return _no_data(group_by, unit)

        notes: list[str] = []
        rows = _gas_to_kwh(
            rows,
            resolved["point"],
            serial_v,
            notes,
            period_from,
            period_to,
            source_unit=source_unit,
            calorific_value=calorific_value,
            kwh_per_m3=kwh_per_m3,
        )
        return await _consumption_result(
            "gas", resolved["point"], "mprn", idv, serial_v, rows,
            period_from, period_to, group_by, unit, include_raw, notes,
        )
    except (OctopusAuthError, OctopusNotFoundError, OctopusAPIError) as e:
        return _err(e)
    except (ValueError, KeyError) as e:
        return {"error": "bad_input", "message": str(e)}


@mcp.tool()
async def get_export_consumption(
    period_from: Annotated[
        str,
        Field(description=_PERIOD_FROM),
    ],
    period_to: Annotated[
        str, Field(description=_PERIOD_TO)
    ],
    group_by: Annotated[
        Literal["half_hour", "hour", "day", "week", "month"],
        Field(description="Bucket size; buckets are UK local days/weeks/months."),
    ] = "day",
    unit: Annotated[
        Literal["kWh", "GBP"],
        Field(
            description=(
                "kWh (energy exported) or GBP (earnings, each half-hour priced at "
                "the export tariff's rate)."
            )
        ),
    ] = "kWh",
    mpan: Annotated[
        Optional[str],
        Field(description="Export MPAN; defaults to OCTOPUS_EXPORT_MPAN or the sole export meter."),
    ] = None,
    serial: Annotated[Optional[str], Field(description="Meter serial.")] = None,
    include_raw: Annotated[
        bool, Field(description="Return raw half-hourly rows (short ranges only).")
    ] = False,
) -> dict[str, Any]:
    """Electricity exported to the grid from solar or a battery, aggregated with statistics.

    Export sits on its own MPAN (flagged ``is_export`` on the account) with its
    own tariff -- Outgoing, Agile Outgoing or a Flux export band. With
    ``unit="GBP"`` the half-hours are priced at that tariff to give earnings;
    no standing charge applies to an export meter.
    """
    try:
        period_from, period_to = shaping.normalise_period(period_from, period_to)
        points = list(iter_meter_points(await get_account()))
        resolved, problem = resolve_meter(
            points, "electricity", mpan=mpan, serial=serial, direction="EXPORT"
        )
        if problem:
            if problem.get("error") == "no_meter":
                problem["message"] = (
                    "No export meter point on this account. Export needs a second "
                    "MPAN flagged is_export -- solar or battery export registered "
                    "with Octopus."
                )
            return problem
        idv = resolved["point"].get("mpan")
        serial_v = resolved["serial"]

        rows = [
            r
            for r in await _fetch_consumption("electricity", idv, serial_v, period_from, period_to)
            if r.get("consumption") is not None
        ]
        if not rows:
            return _no_data(group_by, unit)

        notes = ["Values are export (energy sent to the grid), not consumption."]
        result = await _consumption_result(
            "electricity", resolved["point"], "mpan", idv, serial_v, rows,
            period_from, period_to, group_by, unit, include_raw, notes,
            allow_sc_override=False,
        )
        if "error" not in result:
            result["direction"] = "EXPORT"
            if unit == "GBP":
                result["earnings_gbp"] = result.get("total")
        return result
    except (OctopusAuthError, OctopusNotFoundError, OctopusAPIError) as e:
        return _err(e)
    except (ValueError, KeyError) as e:
        return {"error": "bad_input", "message": str(e)}


def _product_row(p: dict[str, Any]) -> dict[str, Any]:
    """One catalogue row, trimmed to what picks a product out of a list."""
    row = {
        "product_code": p.get("code"),
        "display_name": p.get("display_name"),
        "direction": p.get("direction"),
        "brand": p.get("brand"),
        "available_from": p.get("available_from"),
        "available_to": p.get("available_to"),
    }
    flags = [k[3:] for k in ("is_variable", "is_green", "is_tracker", "is_prepay", "is_business")
             if p.get(k)]
    if flags:
        row["flags"] = flags
    if p.get("term"):
        row["term_months"] = p["term"]
    return row


@mcp.tool()
async def list_products(
    query: Annotated[
        Optional[str],
        Field(description='Substring of the code or display name, e.g. "go", "cosy", "agile".'),
    ] = None,
    direction: Annotated[
        Literal["IMPORT", "EXPORT", "ANY"],
        Field(description="IMPORT for tariffs you buy on, EXPORT for what you are paid for."),
    ] = "IMPORT",
    available_only: Annotated[
        bool, Field(description="Only products still open to new customers.")
    ] = True,
    include_business: Annotated[bool, Field(description="Include business tariffs.")] = False,
    is_variable: Annotated[Optional[bool], Field(description="Filter on variable pricing.")] = None,
    is_green: Annotated[Optional[bool], Field(description="Filter on green tariffs.")] = None,
    is_tracker: Annotated[Optional[bool], Field(description="Filter on tracker tariffs.")] = None,
    is_prepay: Annotated[Optional[bool], Field(description="Filter on prepayment tariffs.")] = None,
    available_at: Annotated[
        Optional[str],
        Field(description="Show the catalogue as it stood at this date (ISO-8601 or YYYY-MM-DD)."),
    ] = None,
) -> dict[str, Any]:
    """Browse the Octopus tariff catalogue: every product and its code.

    This is the entry point for any question about what tariffs exist. The
    catalogue is public and needs no credentials. Use the ``product_code`` it
    returns with get_product for the rates, or with compare_tariffs to price a
    candidate against this account's own consumption.
    """
    try:
        s = get_settings()
        params: dict[str, Any] = {"page_size": 100}
        if available_at:
            params["available_at"] = shaping.to_utc_z(available_at)
        rows = await rest().get_all(
            "/products/", params=params, auth=False, ttl=s.cache_ttl_products, max_pages=10
        )

        needle = (query or "").strip().lower()
        out = []
        for p in rows:
            if direction != "ANY" and p.get("direction") != direction:
                continue
            if not include_business and p.get("is_business"):
                continue
            if available_only and p.get("available_to") is not None:
                continue
            if needle and needle not in f"{p.get('code','')} {p.get('display_name','')}".lower():
                continue
            for flag, want in (("is_variable", is_variable), ("is_green", is_green),
                               ("is_tracker", is_tracker), ("is_prepay", is_prepay)):
                if want is not None and bool(p.get(flag)) != want:
                    break
            else:
                out.append(_product_row(p))

        out.sort(key=lambda r: r["product_code"] or "")
        notes = [
            "Codes from here go to get_product (rates by region) or compare_tariffs "
            "(priced against your own consumption)."
        ]
        if available_only:
            notes.append("Only products still open to new customers; pass available_only=false for the rest.")
        return {
            "count": len(out),
            "catalogue_size": len(rows),
            "products": _truncate(out, notes),
            "notes": " ".join(notes),
        }
    except (OctopusAuthError, OctopusNotFoundError, OctopusAPIError) as e:
        return _err(e)
    except ValueError as e:
        return {"error": "bad_input", "message": str(e)}


_REGISTER_LABELS = {
    "single_register_electricity_tariffs": "electricity_single_register",
    "dual_register_electricity_tariffs": "electricity_dual_register",
    "four_rate_ev_electricity_tariffs": "electricity_four_rate_ev",
    "single_register_gas_tariffs": "gas_single_register",
    "dual_register_gas_tariffs": "gas_dual_register",
}


async def _catalogue_direction(product_code: str) -> Optional[str]:
    """The product detail payload omits ``direction``; the catalogue row carries it."""
    try:
        s = get_settings()
        rows = await rest().get_all(
            "/products/", params={"page_size": 100}, auth=False,
            ttl=s.cache_ttl_products, max_pages=10,
        )
    except (OctopusAuthError, OctopusNotFoundError, OctopusAPIError):
        return None
    for p in rows:
        if p.get("code") == product_code:
            return p.get("direction")
    return None


async def _account_region() -> Optional[str]:
    """The GSP region of this account's meters, when it can be determined."""
    try:
        for point in iter_meter_points(await get_account()):
            if point.get("region"):
                return point["region"]
    except (OctopusAuthError, OctopusNotFoundError, OctopusAPIError):
        return None
    return None


@mcp.tool()
async def get_product(
    product_code: Annotated[
        str, Field(description="Product code from list_products, e.g. GO-VAR-22-10-14.")
    ],
    region: Annotated[
        Optional[str],
        Field(description="GSP region letter A-P. Defaults to this account's region."),
    ] = None,
    tariffs_active_at: Annotated[
        Optional[str], Field(description="Rates as they stood at this date.")
    ] = None,
) -> dict[str, Any]:
    """A product's details and its tariff codes and headline rates for one region.

    Prices differ by region, so this answers for yours unless you name another.
    The unit rate shown is the product's headline figure: for a time-of-use
    tariff such as Agile or Go it does not describe the cheap window, so use
    get_unit_rates on the tariff code for the actual per-slot prices.
    """
    try:
        s = get_settings()
        params: dict[str, Any] = {}
        if tariffs_active_at:
            params["tariffs_active_at"] = shaping.to_utc_z(tariffs_active_at)
        product = await rest().get(
            f"/products/{product_code}/", params=params, auth=False, ttl=s.cache_ttl_products
        )

        groups = {k: v for k, v in product.items() if k.endswith("_tariffs") and v}
        regions = sorted({r.lstrip("_") for g in groups.values() for r in g})
        notes: list[str] = []

        wanted = (region or "").strip().upper().lstrip("_") or None
        if wanted is None:
            wanted = await _account_region()
            if wanted:
                notes.append(f"Showing region {wanted}, this account's region.")
        if wanted is None:
            return {
                "error": "region_required",
                "message": "Could not determine your region; pass one.",
                "available_regions": regions,
            }
        if regions and wanted not in regions:
            return {
                "error": "region_not_offered",
                "message": f"{product_code} is not offered in region {wanted}.",
                "available_regions": regions,
            }

        tariffs: dict[str, Any] = {}
        for group, by_region in groups.items():
            entry = by_region.get(f"_{wanted}")
            if not isinstance(entry, dict):
                continue
            for rate_type, t in entry.items():
                if not isinstance(t, dict) or not t.get("code"):
                    continue
                tariffs.setdefault(_REGISTER_LABELS.get(group, group), {})[rate_type] = {
                    "tariff_code": t["code"],
                    "standing_charge_p_day_inc_vat": t.get("standing_charge_inc_vat"),
                    "unit_rate_p_kwh_inc_vat": t.get("standard_unit_rate_inc_vat"),
                    "unit_rate_p_kwh_exc_vat": t.get("standard_unit_rate_exc_vat"),
                    "exit_fees_inc_vat": t.get("exit_fees_inc_vat") or None,
                }
        if not tariffs:
            return {
                "error": "no_tariffs",
                "message": f"{product_code} publishes no tariffs for region {wanted}.",
                "available_regions": regions,
            }
        if product.get("is_variable"):
            notes.append(
                "Variable product: the unit rate above is the headline figure. For a "
                "time-of-use tariff (Agile, Go, Cosy) call get_unit_rates on the tariff "
                "code for the per-slot prices and the cheap window."
            )

        description = (product.get("description") or "").strip()
        direction = product.get("direction") or await _catalogue_direction(product_code)
        return {
            "product_code": product.get("code"),
            "display_name": product.get("display_name"),
            "full_name": product.get("full_name"),
            "description": description[:400] + ("…" if len(description) > 400 else ""),
            "brand": product.get("brand"),
            "direction": direction,
            "term_months": product.get("term"),
            "available_from": product.get("available_from"),
            "available_to": product.get("available_to"),
            "flags": [k[3:] for k in ("is_variable", "is_green", "is_tracker", "is_prepay", "is_business")
                      if product.get(k)],
            "region": wanted,
            "available_regions": regions,
            "tariffs": tariffs,
            "notes": " ".join(notes) or None,
        }
    except (OctopusAuthError, OctopusNotFoundError, OctopusAPIError) as e:
        return _err(e)
    except ValueError as e:
        return {"error": "bad_input", "message": str(e)}


@mcp.tool()
async def get_unit_rates(
    product_code: Annotated[
        str, Field(description="Product code, e.g. AGILE-18-02-21 (from get_agreements).")
    ],
    tariff_code: Annotated[
        str, Field(description="Full tariff code, e.g. E-1R-AGILE-18-02-21-C (from get_agreements).")
    ],
    period_from: Annotated[
        Optional[str], Field(description=_PERIOD_FROM + " Omit for the full history.")
    ] = None,
    period_to: Annotated[Optional[str], Field(description=_PERIOD_TO)] = None,
    payment_method: Annotated[
        Optional[str],
        Field(
            description=(
                "DIRECT_DEBIT or NON_DIRECT_DEBIT. Defaults to DEFAULT_PAYMENT_METHOD "
                "from .env; if neither is set, all payment methods are returned."
            )
        ),
    ] = None,
) -> dict[str, Any]:
    """Unit rates (p/kWh) for a tariff over a period. Works for fixed and Agile (half-hourly) tariffs.

    Variable tariffs often price by payment method (a direct-debit discount). Pass
    ``payment_method`` or set ``DEFAULT_PAYMENT_METHOD`` in .env to get just your
    rate; otherwise both are returned, each labelled with its ``payment_method``.
    """
    try:
        s = get_settings()
        fuel = shaping.fuel_from_tariff(tariff_code)
        path = shaping.tariff_path(fuel)
        period_from = shaping.to_utc_z(period_from) if period_from else None
        period_to = shaping.to_utc_z(period_to, end_of_day=True) if period_to else None
        url = f"/products/{product_code}/{path}/{tariff_code}/standard-unit-rates/"
        params = {"page_size": 1500}
        if period_from:
            params["period_from"] = period_from
        if period_to:
            params["period_to"] = period_to

        rows = await rest().get_all(
            url,
            params=params,
            auth=False,
            ttl=_rates_ttl(product_code, period_to or ""),
            max_pages=40,
        )
        rates = [
            {
                "valid_from": r.get("valid_from"),
                "valid_to": r.get("valid_to"),
                # Variable tariffs price by payment method (e.g. a direct-debit
                # discount); keep this so the two rates don't read as duplicates.
                "payment_method": r.get("payment_method"),
                "value_exc_vat": round(float(r.get("value_exc_vat") or 0), 2),
                "value_inc_vat": round(float(r.get("value_inc_vat") or 0), 2),
            }
            for r in rows
        ]
        notes: list[str] = []
        pm = _norm_pm(payment_method) or s.default_payment_method
        if pm:
            kept = [
                r for r in rates if r["payment_method"] is None or _norm_pm(r["payment_method"]) == pm
            ]
            if kept:
                rates = kept
            else:
                notes.append(f"No rates for payment method {pm}; all rates shown.")
        values = [r["value_inc_vat"] for r in rates]
        stats = {}
        if values:
            cheapest = min(rates, key=lambda r: r["value_inc_vat"])
            peak = max(rates, key=lambda r: r["value_inc_vat"])
            stats = {
                "count": len(rates),
                "min": min(values),
                "max": max(values),
                "mean": round(sum(values) / len(values), 2),
                "cheapest_slot": cheapest["valid_from"],
                "peak_slot": peak["valid_from"],
            }

        max_rows = s.max_rows_returned
        truncated = len(rates) > max_rows
        if truncated:
            rates = rates[:max_rows]
        if len(rows) > 48:
            notes.append("Half-hourly tariff (e.g. Agile): one rate per 30-min slot.")
        if truncated:
            notes.append(f"Truncated to {max_rows} rows; stats cover the full set.")
        return {
            "fuel": fuel,
            "product_code": product_code,
            "tariff_code": tariff_code,
            "unit": "p/kWh",
            "payment_method": pm,
            "rates": rates,
            "stats": stats,
            "notes": " ".join(notes) or None,
        }
    except (OctopusAuthError, OctopusNotFoundError, OctopusAPIError) as e:
        return _err(e)
    except ValueError as e:
        return {"error": "bad_input", "message": str(e)}


def env_name_hint(fuel: str) -> str:
    """The .env name of the standing-charge override for a fuel."""
    return "STANDING_CHARGE_ELECTRICITY" if fuel == "electricity" else "STANDING_CHARGE_GAS"


@mcp.tool()
async def get_standing_charges(
    product_code: Annotated[str, Field(description="Product code (from get_agreements).")],
    tariff_code: Annotated[str, Field(description="Full tariff code (from get_agreements).")],
    period_from: Annotated[Optional[str], Field(description=_PERIOD_FROM)] = None,
    period_to: Annotated[Optional[str], Field(description=_PERIOD_TO)] = None,
    payment_method: Annotated[
        Optional[str],
        Field(
            description=(
                "DIRECT_DEBIT or NON_DIRECT_DEBIT. Defaults to DEFAULT_PAYMENT_METHOD "
                "from .env; if neither is set, every payment method is returned."
            )
        ),
    ] = None,
) -> dict[str, Any]:
    """Standing charge history for a tariff, in pence per day, ex and inc VAT.

    Variable tariffs publish one charge per payment method (direct debit is
    usually cheaper), so pass ``payment_method`` or set
    ``DEFAULT_PAYMENT_METHOD`` to get just yours.
    """
    try:
        s = get_settings()
        fuel = shaping.fuel_from_tariff(tariff_code)
        path = shaping.tariff_path(fuel)
        period_from = shaping.to_utc_z(period_from) if period_from else None
        period_to = shaping.to_utc_z(period_to, end_of_day=True) if period_to else None
        url = f"/products/{product_code}/{path}/{tariff_code}/standing-charges/"
        params = {"page_size": 1000}
        if period_from:
            params["period_from"] = period_from
        if period_to:
            params["period_to"] = period_to

        rows = await rest().get_all(url, params=params, auth=False, ttl=s.cache_ttl_rates)
        notes: list[str] = []
        all_charges: list[dict[str, Any]] = []
        for r in rows:
            exc = shaping.sc_value_exc_vat(r)
            if exc is None:
                continue
            inc = r.get("value_inc_vat")
            all_charges.append(
                {
                    "valid_from": r.get("valid_from"),
                    "valid_to": r.get("valid_to"),
                    "payment_method": r.get("payment_method"),
                    "value_exc_vat": round(float(exc), 4),
                    "value_inc_vat": round(
                        float(inc) if inc is not None else float(exc) * 1.05, 4
                    ),
                    "unit": "p/day",
                }
            )

        pm = _norm_pm(payment_method) or s.default_payment_method
        charges = all_charges
        if pm:
            kept = [
                c
                for c in all_charges
                if c["payment_method"] is None or _norm_pm(c["payment_method"]) == pm
            ]
            if kept:
                charges = kept
            else:
                notes.append(f"No standing charge for payment method {pm}; all are shown.")

        configured = (
            s.standing_charge_electricity if fuel == "electricity" else s.standing_charge_gas
        )
        published = any(c["value_exc_vat"] for c in charges)
        if not published:
            env_name = (
                "STANDING_CHARGE_ELECTRICITY" if fuel == "electricity" else "STANDING_CHARGE_GAS"
            )
            if configured is not None:
                notes.append(
                    "This tariff publishes no standing charge, so "
                    f"configured_standing_charge_p_per_day_inc_vat ({configured} p/day, "
                    f"pinned as {env_name}) is the account's charge."
                )
            else:
                notes.append(
                    "This tariff publishes no standing charge. If your bill shows one, "
                    f"pin it with {env_name} (VAT-inclusive pence per day)."
                )
        elif configured is not None:
            notes.append(
                f"{env_name_hint(fuel)} is pinned at {configured} p/day inc VAT and is "
                "what cost calculations use, in preference to the published charge "
                "above; clear it to use the published one."
            )

        out: dict[str, Any] = {
            "fuel": fuel,
            "product_code": product_code,
            "tariff_code": tariff_code,
            "unit": "p/day",
            "payment_method": pm,
            "standing_charges": charges,
            "notes": " ".join(notes) if notes else None,
        }
        if configured is not None:
            out["configured_standing_charge_p_per_day_inc_vat"] = configured
        return out
    except (OctopusAuthError, OctopusNotFoundError, OctopusAPIError) as e:
        return _err(e)
    except ValueError as e:
        return {"error": "bad_input", "message": str(e)}


# --- Billing-accurate cost (spec §1.2) -----------------------------------


def _active_agreement(
    point: dict[str, Any], at: Optional[datetime] = None
) -> Optional[dict[str, Any]]:
    """The meter point's best tariff agreement.

    The latest agreement covering ``at`` when given, else the active one,
    else the last on record.
    """
    agreements = point.get("agreements") or []
    if not agreements:
        return None
    if at is not None:
        covering = [
            a
            for a in agreements
            if a.get("valid_from")
            and shaping.parse_dt(a["valid_from"]) <= at
            and (a.get("valid_to") is None or shaping.parse_dt(a["valid_to"]) > at)
        ]
        if covering:
            return max(covering, key=lambda a: shaping.parse_dt(a["valid_from"]))
    return next((a for a in agreements if a.get("active")), agreements[-1])


def _rates_ttl(product_code: str, period_to: str) -> int:
    """Cache TTL for a tariff's unit rates.

    Historical rates are immutable, so they keep the long TTL. Agile and Agile
    Outgoing republish every afternoon for the following day, so a query that
    reaches into the future expires at the next publish instead of holding
    yesterday's answer for 24 hours.
    """
    s = get_settings()
    code = (product_code or "").upper()
    if not (code.startswith("AGILE") or code.startswith("OUTGOING-AGILE")):
        return s.cache_ttl_rates
    try:
        if shaping.parse_dt(period_to) <= datetime.now(timezone.utc):
            return s.cache_ttl_rates
    except ValueError:
        pass
    return min(s.cache_ttl_rates, shaping.seconds_until_next_publish())


async def _fetch_tariff_pricing(
    product_code: str,
    tariff_code: str,
    period_from: str,
    period_to: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Unit rates + standing-charge rows for a tariff over a period (public endpoints)."""
    s = get_settings()
    path = shaping.tariff_path(shaping.fuel_from_tariff(tariff_code))
    base = f"/products/{product_code}/{path}/{tariff_code}/"
    rates = await rest().get_all(
        base + "standard-unit-rates/",
        params={
            "page_size": 1500,
            "period_from": period_from,
            "period_to": period_to,
        },
        auth=False,
        ttl=_rates_ttl(product_code, period_to),
        max_pages=40,
    )
    sc_rows = await rest().get_all(
        base + "standing-charges/",
        params={
            "page_size": 1000,
            "period_from": period_from,
            "period_to": period_to,
        },
        auth=False,
        ttl=s.cache_ttl_rates,
    )
    return rates, sc_rows


def _cost_pence(b: dict[str, Any]) -> dict[str, int]:
    return {
        "unit_cost": b["unit_cost_pence"],
        "standing_charge": b["standing_charge_pence"],
        "subtotal_ex_vat": b["subtotal_ex_vat_pence"],
        "vat_5pct": b["vat_5pct_pence"],
        "total_inc_vat": b["total_inc_vat_pence"],
    }


_GAS_UNIT_NOTE = (
    "Gas source unit is not pinned, so kWh was assumed (what SMETS1 meters "
    "report). SMETS2 meters report m3 instead: set OCTOPUS_GAS_UNITS=m3, or "
    "pass source_unit, to have readings converted."
)


def _resolve_gas_unit(
    point: dict[str, Any], serial_v: str, source_unit: Optional[str]
) -> tuple[str, str]:
    """The unit a gas meter reports in, as ``(unit, basis)``.

    An explicit argument wins, then ``OCTOPUS_GAS_UNITS``, then the meter's
    ``smets`` field if the payload carries one. The REST ``/accounts/``
    response does not, which is why this cannot be detected: SMETS1 reports
    kWh and SMETS2 reports m3, and nothing in the payload says which it is.
    Falls back to kWh (the documented default) with basis ``"assumed"`` so the
    caller can warn.
    """
    v = (source_unit or "").strip().lower().replace("\u00b3", "3")
    if v in ("kwh", "m3"):
        return v, "argument"
    configured = get_settings().octopus_gas_units
    if configured:
        return configured, "configured"
    for m in point.get("meters", []):
        if m.get("serial_number") == serial_v and m.get("smets"):
            return ("m3" if m["smets"] == "SMETS2" else "kwh"), "smets"
    return "kwh", "assumed"


def _looks_like_m3(rows: list[dict[str, Any]], period_from: str, period_to: str) -> bool:
    """True when the daily mean is too low to be gas measured in kWh."""
    try:
        days = (
            shaping.parse_dt(period_to) - shaping.parse_dt(period_from)
        ).total_seconds() / 86400.0
        if days < 1:
            return False
        return (sum(float(r["consumption"]) for r in rows) / days) < 8.0
    except (ValueError, KeyError, ZeroDivisionError):
        return False


def _gas_to_kwh(
    rows: list[dict[str, Any]],
    point: dict[str, Any],
    serial_v: str,
    notes: list[str],
    period_from: str,
    period_to: str,
    source_unit: Optional[str] = None,
    calorific_value: Optional[float] = None,
    kwh_per_m3: Optional[float] = None,
) -> list[dict[str, Any]]:
    """Gas rows in kWh, converting from m3 when that is what the meter reports.

    Returns new rows rather than editing the ones passed in: they may be
    cached upstream, and rewriting them in place compounds the conversion on
    every later read of the same window.
    """
    unit, basis = _resolve_gas_unit(point, serial_v, source_unit)
    if unit == "kwh":
        if basis == "assumed":
            notes.append(_GAS_UNIT_NOTE)
            if _looks_like_m3(rows, period_from, period_to):
                notes.append(
                    "These readings average under 8 a day, which looks like m3 "
                    "rather than kWh -- confirm the unit before trusting the totals."
                )
        elif basis == "smets":
            notes.append("SMETS1 meter: readings are already in kWh.")
        return rows
    factor = kwh_per_m3 or shaping.m3_to_kwh_factor(
        calorific_value or get_settings().gas_calorific_value
    )
    notes.append(
        f"Meter reports m3 ({basis}); converted to kWh at {round(factor, 4)} kWh/m3."
    )
    return [{**r, "consumption": float(r["consumption"]) * factor} for r in rows]


async def _consume_rows(
    fuel: str,
    point: dict[str, Any],
    idv: str,
    serial_v: str,
    period_from: str,
    period_to: str,
    notes: list[str],
) -> list[dict[str, Any]]:
    rows = [
        r
        for r in await _fetch_consumption(fuel, idv, serial_v, period_from, period_to)
        if r.get("consumption") is not None
    ]
    if fuel == "gas":
        rows = _gas_to_kwh(rows, point, serial_v, notes, period_from, period_to)
    return rows


@mcp.tool()
async def calculate_cost(
    period_from: Annotated[
        str, Field(description=_PERIOD_FROM)
    ],
    period_to: Annotated[
        str, Field(description=_PERIOD_TO)
    ],
    mpan: Annotated[
        Optional[str],
        Field(description="13-digit MPAN (electricity); defaults to the configured/sole meter."),
    ] = None,
    mprn: Annotated[
        Optional[str],
        Field(description="Gas MPRN; when set, prices the gas meter instead of electricity."),
    ] = None,
    serial: Annotated[
        Optional[str], Field(description="Meter serial; defaults to the first active meter.")
    ] = None,
    tariff_code: Annotated[
        Optional[str],
        Field(
            description=(
                "Full tariff code to price at, e.g. E-1R-AGILE-18-02-21-C. Defaults to "
                "the meter's current agreement."
            )
        ),
    ] = None,
    payment_method: Annotated[
        Optional[str],
        Field(
            description="DIRECT_DEBIT or NON_DIRECT_DEBIT; defaults to DEFAULT_PAYMENT_METHOD from .env."
        ),
    ] = None,
) -> dict[str, Any]:
    """Billing-accurate cost of a meter's consumption over a period, as a pence breakdown.

    Prices half-hourly consumption against the tariff's unit rates and applies
    Octopus's billing rounding (spec §1.2): each interval's kWh rounded to 0.01
    (banker's rounding), priced at the exc-VAT rate, cost rounded to the penny;
    the standing charge is added, then 5% VAT on top. Closer to the actual
    invoice than the unit="GBP" estimate of the consumption tools.
    """
    try:
        s = get_settings()
        period_from, period_to = shaping.normalise_period(period_from, period_to)
        fuel = "gas" if mprn else "electricity"
        id_key = "mpan" if fuel == "electricity" else "mprn"
        points = list(iter_meter_points(await get_account()))
        resolved, problem = resolve_meter(points, fuel, mpan=mpan, mprn=mprn, serial=serial)
        if problem:
            return problem
        point = resolved["point"]
        idv = point.get(id_key)
        serial_v = resolved["serial"]

        notes: list[str] = []
        rows = await _consume_rows(fuel, point, idv, serial_v, period_from, period_to, notes)
        if not rows:
            return {
                "error": "no_data",
                "message": (
                    "No consumption data returned for this range. Settlement data can lag "
                    "by up to a day; try an earlier period_to."
                ),
            }

        if tariff_code:
            product_code = shaping.parse_tariff_code(tariff_code)["product_code"]
            if not product_code:
                return {"error": "bad_input", "message": f"Cannot parse tariff_code {tariff_code!r}."}
        else:
            ag = _active_agreement(point, shaping.parse_dt(period_from))
            if not ag or not ag.get("product_code") or not ag.get("tariff_code"):
                return {
                    "error": "no_tariff",
                    "message": (
                        "No tariff agreement found for this meter point, so "
                        "consumption cannot be priced."
                    ),
                }
            product_code = ag["product_code"]
            tariff_code = ag["tariff_code"]

        rates, sc_rows = await _fetch_tariff_pricing(product_code, tariff_code, period_from, period_to)
        pm = _norm_pm(payment_method) or s.default_payment_method
        sc_override = (
            s.standing_charge_electricity if fuel == "electricity" else s.standing_charge_gas
        )
        b = shaping.bill_cost(rows, rates, period_from, period_to, pm, sc_rows, sc_override)
        if "error" in b:
            return b

        out: dict[str, Any] = {
            "fuel": fuel,
            "serial": serial_v,
            "product_code": product_code,
            "tariff_code": tariff_code,
            "region": point.get("region"),
            "period_from": period_from,
            "period_to": period_to,
            "payment_method": pm,
            "total_kwh": b["total_kwh"],
            "billed_kwh": b["billed_kwh"],
            "effective_p_kwh_exc_vat": b["effective_p_kwh_exc_vat"],
            "rate_bands": b.get("rate_bands"),
            "cost_pence": _cost_pence(b),
            "total_gbp": b["total_gbp"],
            "standing_charge_source": b["sc_source"],
            "unpriced_kwh": b["unpriced_kwh"] or None,
            "notes": " ".join(notes + b["notes"]) or None,
        }
        out[id_key] = idv
        return out
    except (OctopusAuthError, OctopusNotFoundError, OctopusAPIError) as e:
        return _err(e)
    except (ValueError, KeyError) as e:
        return {"error": "bad_input", "message": str(e)}


# The Octopus product API groups a product's tariffs by register type and,
# within each, by region suffix:
#   product["single_register_electricity_tariffs"]["_C"]["varying"]["code"]
# The tariff object carries no region of its own — the region is the dict key,
# the register is the parent key.
_REGISTER_PRODUCT_KEYS = {
    ("electricity", "1R"): "single_register_electricity_tariffs",
    ("electricity", "2R"): "dual_register_electricity_tariffs",
    ("electricity", "3R"): "four_rate_ev_electricity_tariffs",
    ("electricity", "4R"): "four_rate_ev_electricity_tariffs",
    ("gas", "1R"): "single_register_gas_tariffs",
    ("gas", "2R"): "dual_register_gas_tariffs",
}


def _parse_tariff_code(tc: Optional[str]) -> tuple[str, str, Optional[str]]:
    """Split a tariff code like ``E-1R-VAR-22-11-01-A`` into
    ``(fuel, register, region)``. Missing parts fall back to electricity /
    single-register / no region."""
    parts = (tc or "").split("-")
    fuel = "gas" if (parts and parts[0].upper() == "G") else "electricity"
    register = "1R"
    if len(parts) > 1 and parts[1].upper().endswith("R"):
        register = parts[1].upper()
    region = parts[-1] if parts else None
    return fuel, register, region


def _candidate_tariff_code(
    product: dict[str, Any], fuel: str, register: str, region: Optional[str]
) -> Optional[str]:
    """The candidate product's tariff code for ``(register, region)``, or
    ``None`` if it doesn't offer one. The tariff object may sit directly on
    the entry or be nested under a rate-type key (e.g. ``"varying"``)."""
    if not region or not isinstance(product, dict):
        return None
    key = _REGISTER_PRODUCT_KEYS.get((fuel, register))
    if not key:
        return None
    entry = (product.get(key) or {}).get(f"_{region}")
    if not isinstance(entry, dict):
        return None
    if isinstance(entry.get("code"), str):
        return entry["code"]
    for v in entry.values():
        if isinstance(v, dict) and isinstance(v.get("code"), str):
            return v["code"]
    return None


def _rate_summary(
    rates: list[dict[str, Any]], pm: Optional[str]
) -> Optional[dict[str, float]]:
    """The published exc-VAT rate range for ``pm``.

    Only the range: the mean of published rate *rows* is meaningless across
    tariffs, because Agile publishes 1,488 rows a month and a fixed tariff
    publishes one. The comparable figure is effective_p_kwh_exc_vat, which is
    this account's cost divided by its kWh.
    """
    values = [
        float(r["value_exc_vat"])
        for r in rates
        if r.get("value_exc_vat") is not None
        and (pm is None or (r.get("payment_method") or "").strip().upper() == pm)
    ]
    if not values:
        values = [
            float(r["value_exc_vat"]) for r in rates if r.get("value_exc_vat") is not None
        ]
    if not values:
        return None
    return {"min": round(min(values), 4), "max": round(max(values), 4)}


@mcp.tool()
async def compare_tariffs(
    candidate_product_codes: Annotated[
        list[str],
        Field(
            description=(
                "Product codes to compare, e.g. [\"AGILE-18-02-21\", \"VAR-22-11-01\"]. "
                "The meter's current product (from get_agreements) is used as the baseline."
            )
        ),
    ],
    period_from: Annotated[str, Field(description=_PERIOD_FROM)],
    period_to: Annotated[str, Field(description=_PERIOD_TO)],
    mpan: Annotated[
        Optional[str],
        Field(description="13-digit MPAN (electricity); defaults to the configured/sole meter."),
    ] = None,
    mprn: Annotated[
        Optional[str],
        Field(description="Gas MPRN; when set, compares gas tariffs on the gas meter."),
    ] = None,
    serial: Annotated[
        Optional[str], Field(description="Meter serial; defaults to the first active meter.")
    ] = None,
    payment_method: Annotated[
        Optional[str],
        Field(
            description="DIRECT_DEBIT or NON_DIRECT_DEBIT; defaults to DEFAULT_PAYMENT_METHOD from .env."
        ),
    ] = None,
) -> dict[str, Any]:
    """Price candidate tariffs against this account's actual consumption and rank them by cost.

    Each candidate is priced with the same billing-accurate method as
    calculate_cost (billing rounding, standing charge, 5% VAT), so the totals
    are comparable to the invoice. The meter's current tariff is included as
    the baseline with ``delta_gbp_vs_baseline`` (negative = candidate is
    cheaper). Results are sorted cheapest first; candidates that can't be
    priced for the region/period appear at the end with an error.
    """
    try:
        s = get_settings()
        period_from, period_to = shaping.normalise_period(period_from, period_to)
        products: dict[str, dict[str, Any]] = {}
        product_errors: dict[str, str] = {}
        for code in candidate_product_codes:
            try:
                products[code] = await rest().get(
                    f"/products/{code}/", auth=False, ttl=s.cache_ttl_products
                )
            except OctopusNotFoundError:
                product_errors[code] = "not_found"
            except (OctopusAuthError, OctopusAPIError) as e:
                product_errors[code] = _err(e)["error"]

        # Fuel: explicit selection wins, else whatever the account actually has.
        if mprn:
            fuel = "gas"
        elif mpan:
            fuel = "electricity"
        else:
            fuels = {p.get("fuel") for p in iter_meter_points(await get_account())}
            fuel = "electricity" if "electricity" in fuels else "gas"
        id_key = "mpan" if fuel == "electricity" else "mprn"

        points = list(iter_meter_points(await get_account()))
        resolved, problem = resolve_meter(points, fuel, mpan=mpan, mprn=mprn, serial=serial)
        if problem:
            return problem
        point = resolved["point"]
        idv = point.get(id_key)
        serial_v = resolved["serial"]

        # The account's active tariff fixes the (register, region) each candidate is
        # priced at. The region lives in the tariff code suffix (e.g. ``...-C``), not
        # on the meter point (which often reports ``region: null``).
        at = shaping.parse_dt(period_from)
        ag = _active_agreement(point, at)
        if not ag or not ag.get("tariff_code") or not ag.get("product_code"):
            return {
                "error": "no_tariff",
                "message": (
                    "No active tariff agreement on this meter; cannot determine the "
                    "region/register to compare candidates against."
                ),
            }
        _, register, region = _parse_tariff_code(ag["tariff_code"])

        notes: list[str] = []
        rows = await _consume_rows(fuel, point, idv, serial_v, period_from, period_to, notes)
        if not rows:
            return {
                "error": "no_data",
                "message": (
                    "No consumption data returned for this range. Settlement data can lag "
                    "by up to a day; try an earlier period_to."
                ),
            }
        total_kwh = round(sum(float(r["consumption"]) for r in rows), 3)

        pm = _norm_pm(payment_method) or s.default_payment_method
        sc_override = (
            s.standing_charge_electricity if fuel == "electricity" else s.standing_charge_gas
        )

        def price(
            rates: list[dict[str, Any]],
            sc_rows: list[dict[str, Any]],
            override: Optional[float] = None,
        ) -> dict[str, Any]:
            # A tariff that only covers part of the period cannot be ranked
            # against one that covers all of it: the unpriced kWh would count
            # as free. A product launched mid-period would win every time.
            # The STANDING_CHARGE_* override corrects *this* account's current
            # tariff against its bill; applying it to a candidate would price
            # every alternative with the same daily charge and reduce the
            # comparison to unit rates alone. Candidates use what they publish.
            b = shaping.bill_cost(
                rows, rates, period_from, period_to, pm, sc_rows, override
            )
            if "error" in b:
                return {"error": b["error"], "message": b.get("message")}
            unpriced = b["unpriced_kwh"]
            if unpriced and total_kwh and unpriced > total_kwh * 0.01:
                covered = 1 - unpriced / total_kwh
                return {
                    "error": "incomplete_rate_coverage",
                    "message": (
                        f"This tariff publishes rates for only {covered:.0%} of the period "
                        f"({unpriced} of {total_kwh} kWh unpriced) -- usually because the "
                        "product launched partway through it. Not ranked: the total would "
                        "treat the rest as free. Compare over a period it fully covers."
                    ),
                    "priced_share": round(covered, 4),
                    "unpriced_kwh": unpriced,
                }
            candidate_notes = list(b["notes"])
            bands = b.get("rate_bands")
            if bands:
                share = bands["share_in_cheapest_band"]
                candidate_notes.append(
                    f"Time-of-use tariff: {share:.0%} of your kWh fell in its cheapest "
                    f"band ({bands['min_p_kwh_exc_vat']}p/kWh exc VAT) on your current "
                    "usage pattern. Shifting load into that window would improve this; "
                    "the figure assumes you do not."
                )
            return {
                "total_kwh": b["total_kwh"],
                "billed_kwh": b["billed_kwh"],
                "cost_pence": _cost_pence(b),
                "total_gbp": b["total_gbp"],
                "effective_p_kwh_exc_vat": b["effective_p_kwh_exc_vat"],
                "standing_charge_source": b["sc_source"],
                "published_rate_range_p_kwh_exc_vat": _rate_summary(rates, pm),
                "rate_bands": bands,
                "unpriced_kwh": b["unpriced_kwh"] or None,
                "notes": " ".join(candidate_notes) or None,
            }

        candidates: list[dict[str, Any]] = []
        for code in candidate_product_codes:
            if code in product_errors:
                candidates.append({"product_code": code, "error": product_errors[code]})
                continue
            tc = _candidate_tariff_code(products[code], fuel, register, region)
            if not tc:
                candidates.append(
                    {
                        "product_code": code,
                        "error": "no_tariff_for_region",
                        "register": register,
                        "region": region,
                    }
                )
                continue
            try:
                rates, sc_rows = await _fetch_tariff_pricing(code, tc, period_from, period_to)
            except (OctopusAuthError, OctopusNotFoundError, OctopusAPIError) as e:
                candidates.append(
                    {"product_code": code, "tariff_code": tc, "error": _err(e)["error"]}
                )
                continue
            priced = price(rates, sc_rows)
            if "error" in priced:
                candidates.append({"product_code": code, "tariff_code": tc, **priced})
                continue
            candidates.append({"product_code": code, "tariff_code": tc, **priced})

        ranked = sorted((c for c in candidates if "error" not in c), key=lambda c: c["total_gbp"])
        for i, c in enumerate(ranked, 1):
            c["rank"] = i
        ordered = ranked + [c for c in candidates if "error" in c]

        bpc, btc = ag["product_code"], ag["tariff_code"]
        baseline: dict[str, Any]
        try:
            rates, sc_rows = await _fetch_tariff_pricing(bpc, btc, period_from, period_to)
        except (OctopusAuthError, OctopusNotFoundError, OctopusAPIError) as e:
            baseline = {"product_code": bpc, "tariff_code": btc, "error": _err(e)["error"]}
        else:
            priced = price(rates, sc_rows, sc_override)
            baseline = {"product_code": bpc, "tariff_code": btc, **priced}

        if "total_gbp" in baseline:
            for c in ranked:
                c["delta_gbp_vs_baseline"] = round(c["total_gbp"] - baseline["total_gbp"], 2)
            baseline["delta_gbp_vs_baseline"] = 0.0

        if not ranked:
            notes.append("None of the candidate tariffs could be priced for this region/period.")
        if sc_override is not None and ranked:
            notes.append(
                "Candidates are priced at their own published standing charges; the "
                "baseline uses the pinned STANDING_CHARGE_* value."
            )
        out: dict[str, Any] = {
            "fuel": fuel,
            "serial": serial_v,
            "region": region,
            "period_from": period_from,
            "period_to": period_to,
            "payment_method": pm,
            "total_kwh": total_kwh,
            "baseline": baseline,
            "candidates": ordered,
            "notes": " ".join(notes) or None,
        }
        out[id_key] = idv
        return out
    except (OctopusAuthError, OctopusNotFoundError, OctopusAPIError) as e:
        return _err(e)
    except (ValueError, KeyError) as e:
        return {"error": "bad_input", "message": str(e)}


def _env_list(name: str) -> list[str]:
    """A comma-separated environment variable as a list of trimmed values."""
    return [v.strip() for v in os.environ.get(name, "").split(",") if v.strip()]


def _transport_security() -> TransportSecuritySettings:
    """Host/Origin allow-list for the HTTP transport.

    The MCP endpoint has no authentication of its own: anything that can reach
    it can read the whole account. Two of the three ways to reach it are closed
    here -- a browser on another origin, and a DNS-rebinding attack against a
    name that resolves to this host. The third, the network the port is
    published on, is closed in compose by binding to loopback.

    NGINX passes the client's Host through, so these values are what a client
    dials, not what the container binds. Loopback on any port is allowed;
    reaching the server by any other name (a tailnet name, say) means adding it
    to MCP_ALLOWED_HOSTS. A rejected request is logged with the exact Host it
    carried, which is the value to add.
    """
    hosts = ["localhost", "127.0.0.1", "localhost:*", "127.0.0.1:*", *_env_list("MCP_ALLOWED_HOSTS")]
    origins = [
        "http://localhost:*",
        "http://127.0.0.1:*",
        "https://localhost:*",
        "https://127.0.0.1:*",
        *_env_list("MCP_ALLOWED_ORIGINS"),
    ]
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=hosts,
        allowed_origins=origins,
    )


def main() -> None:
    # Fail fast on missing configuration with an actionable message.
    try:
        settings = get_settings()
    except Exception as exc:  # pydantic.ValidationError on missing required fields
        print(
            "Configuration error: set OCTOPUS_API_KEY and OCTOPUS_ACCOUNT_NUMBER "
            "(via environment or a .env file). See .env.example.",
            file=sys.stderr,
        )
        if hasattr(exc, "errors"):
            fields = [",".join(str(x) for x in err.get("loc", [])) for err in exc.errors()]
            print("Missing/invalid fields: " + ", ".join(fields), file=sys.stderr)
        raise SystemExit(2)

    observability.configure_logging(settings.octopus_api_key)

    if "--stdio" in sys.argv[1:]:
        observability.event("server.start", version=__version__, transport="stdio")
        mcp.run(transport="stdio")
        return

    host = os.environ.get("MCP_HOST", "127.0.0.1")
    port = int(os.environ.get("MCP_PORT", "8000"))
    security = _transport_security()
    observability.event(
        "server.start",
        version=__version__,
        transport="streamable-http",
        host=host,
        port=port,
        allowed_hosts=security.allowed_hosts,
    )
    mcp.run(
        transport="streamable-http",
        host=host,
        port=port,
        transport_security=security,
    )


if __name__ == "__main__":
    main()
