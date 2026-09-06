"""Smoke tests: the server imports, all tools register, and their input
schemas are valid objects. No network is touched here."""

import asyncio
import os
from datetime import datetime, timedelta

os.environ.setdefault("OCTOPUS_API_KEY", "dummy")
os.environ.setdefault("OCTOPUS_ACCOUNT_NUMBER", "A-TEST123")
# Tests must not inherit the operator's .env defaults (pydantic loads .env
# for Settings); empty string is read as "unset" by the validators.
os.environ["DEFAULT_PAYMENT_METHOD"] = ""
os.environ["STANDING_CHARGE_ELECTRICITY"] = ""
os.environ["STANDING_CHARGE_GAS"] = ""

from octopus_mcp.server import mcp  # noqa: E402  (env must be set before import)

EXPECTED_TOOLS = {
    "get_current_datetime",
    "get_electricity_consumption",
    "get_gas_consumption",
    "get_unit_rates",
    "get_standing_charges",
    "list_meter_points",
    "get_agreements",
    "calculate_cost",
    "compare_tariffs",
    "list_products",
    "get_product",
}


def _tools():
    return asyncio.run(mcp.list_tools())


def test_all_tools_registered():
    names = {t.name for t in _tools()}
    missing = EXPECTED_TOOLS - names
    assert not missing, f"missing tools: {missing}"


def test_input_schemas_are_objects():
    for t in _tools():
        # mcp 2.x: Tool.inputSchema renamed to Tool.input_schema
        assert isinstance(t.input_schema, dict), t.name
        assert t.input_schema.get("type") == "object", t.name
        assert t.description, f"{t.name} has no description"


def test_consumption_schema_has_required_period():
    by_name = {t.name: t for t in _tools()}
    schema = by_name["get_electricity_consumption"].input_schema
    assert "period_from" in schema.get("required", [])
    assert "period_to" in schema.get("required", [])
    group_by = schema["properties"]["group_by"]
    assert "half_hour" in group_by.get("enum", [])
    assert group_by.get("default") == "day"


def test_unit_rates_preserve_payment_method(monkeypatch):
    """Variable tariffs price by payment method; both rates must survive and be
    labelled, not collapsed into apparent duplicates."""
    import octopus_mcp.server as srv

    class FakeRest:
        async def get_all(self, *a, **k):
            return [
                {"valid_from": "2026-06-30T23:00:00Z", "valid_to": None,
                 "value_exc_vat": 25.1251, "value_inc_vat": 26.381355,
                 "payment_method": "DIRECT_DEBIT"},
                {"valid_from": "2026-06-30T23:00:00Z", "valid_to": None,
                 "value_exc_vat": 26.5227, "value_inc_vat": 27.848835,
                 "payment_method": "NON_DIRECT_DEBIT"},
            ]

    monkeypatch.setattr(srv, "rest", lambda: FakeRest())
    out = asyncio.run(
        srv.get_unit_rates(
            "VAR-22-11-01", "E-1R-VAR-22-11-01-A",
            "2026-06-01T00:00:00Z", "2026-09-01T00:00:00Z",
        )
    )
    assert len(out["rates"]) == 2
    assert {r["payment_method"] for r in out["rates"]} == {
        "DIRECT_DEBIT",
        "NON_DIRECT_DEBIT",
    }


# --- Payment method default -------------------------------------------------


def _both_rate_rows():
    return [
        {"valid_from": "2026-06-30T23:00:00Z", "valid_to": None,
         "value_exc_vat": 25.1251, "value_inc_vat": 26.38,
         "payment_method": "DIRECT_DEBIT"},
        {"valid_from": "2026-06-30T23:00:00Z", "valid_to": None,
         "value_exc_vat": 26.5227, "value_inc_vat": 27.85,
         "payment_method": "NON_DIRECT_DEBIT"},
    ]


def test_settings_payment_method_validation():
    import pytest

    from octopus_mcp import config

    base = {"octopus_api_key": "x", "octopus_account_number": "A-TEST123"}
    assert config.Settings(**base).default_payment_method is None
    got = config.Settings(default_payment_method=" direct_debit ", **base)
    assert got.default_payment_method == "DIRECT_DEBIT"
    with pytest.raises(ValueError):
        config.Settings(default_payment_method="CHEQUE", **base)


def test_unit_rates_explicit_payment_method(monkeypatch):
    import octopus_mcp.server as srv

    class FakeRest:
        async def get_all(self, *a, **k):
            return _both_rate_rows()

    monkeypatch.setattr(srv, "rest", lambda: FakeRest())
    out = asyncio.run(
        srv.get_unit_rates(
            "VAR-22-11-01", "E-1R-VAR-22-11-01-A",
            "2026-06-01T00:00:00Z", "2026-09-01T00:00:00Z",
            payment_method="direct_debit",
        )
    )
    assert out["payment_method"] == "DIRECT_DEBIT"
    assert [r["payment_method"] for r in out["rates"]] == ["DIRECT_DEBIT"]
    assert out["rates"][0]["value_inc_vat"] == 26.38


def test_unit_rates_payment_method_defaults_from_settings(monkeypatch):
    import octopus_mcp.server as srv
    from octopus_mcp import config

    settings = config.Settings(
        octopus_api_key="x", octopus_account_number="A-TEST123",
        default_payment_method="NON_DIRECT_DEBIT",
    )

    class FakeRest:
        async def get_all(self, *a, **k):
            return _both_rate_rows()

    monkeypatch.setattr(srv, "get_settings", lambda: settings)
    monkeypatch.setattr(srv, "rest", lambda: FakeRest())
    out = asyncio.run(
        srv.get_unit_rates(
            "VAR-22-11-01", "E-1R-VAR-22-11-01-A",
            "2026-06-01T00:00:00Z", "2026-09-01T00:00:00Z",
        )
    )
    assert out["payment_method"] == "NON_DIRECT_DEBIT"
    assert [r["payment_method"] for r in out["rates"]] == ["NON_DIRECT_DEBIT"]
    assert out["rates"][0]["value_inc_vat"] == 27.85


# --- Period boundary ---------------------------------------------------------


def test_fetch_consumption_excludes_interval_starting_at_period_to(monkeypatch):
    """The API treats period_to as inclusive and returns the interval starting
    exactly at period_to; the server must clamp to [from, to) so adjacent
    periods tile without double-counting. (Live check vs portal data, Aug 2026:
    the API also returns local-offset timestamps like +01:00.)"""
    import octopus_mcp.server as srv
    from octopus_mcp import config

    settings = config.Settings(
        octopus_api_key="x", octopus_account_number="A-TEST123",
    )

    rows = [
        {"interval_start": "2026-08-01T01:00:00+01:00", "interval_end": "2026-08-01T01:30:00+01:00",
         "consumption": 1.0},  # 00:00Z, inside
        {"interval_start": "2026-08-01T01:30:00+01:00", "interval_end": "2026-08-01T02:00:00+01:00",
         "consumption": 2.0},  # 00:30Z, inside
        {"interval_start": "2026-08-01T02:00:00+01:00", "interval_end": "2026-08-01T02:30:00+01:00",
         "consumption": 9.0},  # 01:00Z == period_to, must be dropped
    ]

    class FakeRest:
        async def get_all(self, *a, **k):
            return rows

    monkeypatch.setattr(srv, "get_settings", lambda: settings)
    monkeypatch.setattr(srv, "rest", lambda: FakeRest())
    out = asyncio.run(
        srv._fetch_consumption(
            "electricity", "IDV-1", "S-123",
            "2026-08-01T00:00:00Z", "2026-08-01T01:00:00Z",
        )
    )
    assert [r["consumption"] for r in out] == [1.0, 2.0]


# --- GBP (cost) mode ---------------------------------------------------------

FAKE_ACCOUNT = {
    "properties": [
        {
            "id": "P-1",
            "address": {"line1": "1 Test St", "town": "Testville", "postcode": "TV1 1AA"},
            "electricity_meter_points": [
                {
                    "mpan": "0001234567890",
                    "region": "C",
                    "direction": "IMPORT",
                    "meters": [
                        {"serial_number": "S-123", "smets": "SMETS1",
                         "meter_status": "ACTIVE", "registers": []},
                    ],
                    "agreements": [
                        {
                            "tariff_code": "E-1R-TEST-26-01-01-C",
                            "product_code": "TEST-26-01-01",
                            "valid_from": "2026-01-01T00:00:00Z",
                            "valid_to": None,
                            "active": True,
                        }
                    ],
                }
            ],
        }
    ]
}

CONSUMPTION_ROWS = [
    {"interval_start": "2026-08-01T00:00:00Z", "interval_end": "2026-08-01T00:30:00Z",
     "consumption": 1.0},
    {"interval_start": "2026-08-01T00:30:00Z", "interval_end": "2026-08-01T01:00:00Z",
     "consumption": 2.0},
]

TWO_RATE_TARIFF = [
    {"valid_from": "2026-07-01T00:00:00Z", "valid_to": None,
     "value_inc_vat": 20.0, "payment_method": "DIRECT_DEBIT"},
    {"valid_from": "2026-07-01T00:00:00Z", "valid_to": None,
     "value_inc_vat": 30.0, "payment_method": "NON_DIRECT_DEBIT"},
]


def _run_gbp(monkeypatch, rates, default_pm=None, sc_rows=()):
    import octopus_mcp.server as srv
    from octopus_mcp import config

    settings = config.Settings(
        octopus_api_key="x", octopus_account_number="A-TEST123",
        default_payment_method=default_pm,
    )

    class FakeRest:
        async def get_all(self, url, **k):
            if "/consumption/" in url:
                return CONSUMPTION_ROWS
            if "standing-charges" in url:
                return list(sc_rows)
            return rates

    async def fake_account():
        return FAKE_ACCOUNT

    monkeypatch.setattr(srv, "get_settings", lambda: settings)
    monkeypatch.setattr(srv, "get_account", fake_account)
    monkeypatch.setattr(srv, "rest", lambda: FakeRest())
    return asyncio.run(
        srv.get_electricity_consumption(
            "2026-08-01T00:00:00Z", "2026-08-01T01:00:00Z",
            group_by="half_hour", unit="GBP",
        )
    )


def test_gbp_mode_uses_configured_payment_method(monkeypatch):
    out = _run_gbp(monkeypatch, TWO_RATE_TARIFF, default_pm="DIRECT_DEBIT")
    assert out["unit"] == "GBP"
    # 1.0 kWh * 20p + 2.0 kWh * 20p, no standing charge published
    assert out["unit_cost_gbp"] == 0.60
    assert out["standing_charge_gbp"] == 0.0
    assert out["total"] == 0.60
    assert out["total_kwh"] == 3.0
    assert out["payment_method"] == "DIRECT_DEBIT"
    assert out["tariff_code"] == "E-1R-TEST-26-01-01-C"
    assert "unit cost only" in out["notes"]
    assert [b["value"] for b in out["series"]] == [0.20, 0.40]


def test_gbp_mode_total_includes_the_standing_charge(monkeypatch):
    """F-04: the headline total is what the period cost, standing charge and
    all -- the series stays unit cost, because a daily charge does not belong
    to any half-hour bucket."""
    sc = [{"valid_from": "2026-07-01T00:00:00Z", "valid_to": None,
           "value_exc_vat": 48.0, "value_inc_vat": 50.4, "payment_method": "DIRECT_DEBIT"}]
    out = _run_gbp(monkeypatch, TWO_RATE_TARIFF, default_pm="DIRECT_DEBIT", sc_rows=sc)
    # One hour of a 50.4p/day inc-VAT charge = 2p
    assert out["standing_charge_gbp"] == 0.02
    assert out["unit_cost_gbp"] == 0.60
    assert out["total"] == 0.62
    assert out["total_gbp"] == 0.62
    assert out["standing_charge_source"] == "api"


def test_gbp_mode_without_default_uses_highest_rate(monkeypatch):
    out = _run_gbp(monkeypatch, TWO_RATE_TARIFF)
    # Conservative: 1.0 * 30p + 2.0 * 30p
    assert out["total"] == 0.90
    assert out["unit_cost_gbp"] == 0.90
    assert out["total_kwh"] == 3.0
    assert "DEFAULT_PAYMENT_METHOD" in out["notes"]


def test_gbp_mode_unpriced_intervals_are_excluded_and_noted(monkeypatch):
    partial = [
        {"valid_from": "2026-08-01T00:00:00Z", "valid_to": "2026-08-01T00:30:00Z",
         "value_inc_vat": 10.0, "payment_method": "DIRECT_DEBIT"},
    ]
    out = _run_gbp(monkeypatch, partial, default_pm="DIRECT_DEBIT")
    # Only the 00:00 interval (1.0 kWh) is priced at 10p
    assert out["total"] == 0.10
    assert out["total_kwh"] == 3.0
    assert "2.0 kWh had no matching rate" in out["notes"]


def test_gbp_mode_single_price_tariff(monkeypatch):
    fixed = [
        {"valid_from": "2026-07-01T00:00:00Z", "valid_to": None,
         "value_inc_vat": 25.0, "payment_method": None},
    ]
    out = _run_gbp(monkeypatch, fixed)
    assert out["total"] == 0.75  # 3.0 kWh * 25p
    assert out["payment_method"] is None
    assert "unit cost only" in out["notes"]


# --- Current date/time ---------------------------------------------------


def test_current_datetime_shape():
    import re

    from octopus_mcp.server import get_current_datetime

    out = get_current_datetime()
    assert set(out) >= {"utc", "europe_london", "notes"}
    weekdays = {"Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"}
    for part in (out["utc"], out["europe_london"]):
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", part["date"])
        assert re.fullmatch(r"\d{2}:\d{2}:\d{2}", part["time"])
        assert part["weekday"] in weekdays
        # must parse as ISO 8601
        datetime.fromisoformat(part["datetime"].replace("Z", "+00:00"))
    utc = datetime.fromisoformat(out["utc"]["datetime"].replace("Z", "+00:00"))
    london = datetime.fromisoformat(out["europe_london"]["datetime"])
    assert london - utc == timedelta(0)  # same instant, two zones
    assert out["utc"]["utc_offset"] == "+00:00"
    assert out["europe_london"]["utc_offset"] in ("+00:00", "+01:00")  # GMT or BST


# --- Standing charge override --------------------------------------------


def _run_standing_charges(
    monkeypatch, api_rows: list, tariff="E-1R-VAR-22-11-01-A", payment_method=None, **settings_kw
):
    import octopus_mcp.server as srv
    from octopus_mcp import config

    settings = config.Settings(
        octopus_api_key="x", octopus_account_number="A-TEST123", **settings_kw
    )

    class FakeRest:
        async def get_all(self, *a, **k):
            return api_rows

    monkeypatch.setattr(srv, "get_settings", lambda: settings)
    monkeypatch.setattr(srv, "rest", lambda: FakeRest())
    return asyncio.run(
        srv.get_standing_charges("VAR-22-11-01", tariff, payment_method=payment_method)
    )


SC_API_ROWS = [
    {"valid_from": "2026-06-30T23:00:00Z", "valid_to": None,
     "value_exc_vat": 48.2442, "value_inc_vat": 50.65641,
     "payment_method": "DIRECT_DEBIT"},
    {"valid_from": "2026-06-30T23:00:00Z", "valid_to": None,
     "value_exc_vat": 56.4891, "value_inc_vat": 59.313555,
     "payment_method": "NON_DIRECT_DEBIT"},
]


def test_standing_charge_reads_the_real_fields(monkeypatch):
    """F-01: the endpoint publishes value_exc_vat / value_inc_vat. Reading a
    key called `value` made every tariff look like it had no standing charge."""
    out = _run_standing_charges(monkeypatch, SC_API_ROWS)
    assert len(out["standing_charges"]) == 2
    first = out["standing_charges"][0]
    assert first["value_exc_vat"] == 48.2442
    assert first["value_inc_vat"] == 50.6564
    assert first["unit"] == "p/day"
    assert "pin it" not in (out["notes"] or "")


def test_standing_charge_filters_by_payment_method(monkeypatch):
    out = _run_standing_charges(monkeypatch, SC_API_ROWS, payment_method="DIRECT_DEBIT")
    assert [c["payment_method"] for c in out["standing_charges"]] == ["DIRECT_DEBIT"]
    assert out["payment_method"] == "DIRECT_DEBIT"


def test_standing_charge_configured_override(monkeypatch):
    out = _run_standing_charges(
        monkeypatch, [{"value_exc_vat": 0.0}], standing_charge_electricity=50.88
    )
    assert out["configured_standing_charge_p_per_day_inc_vat"] == 50.88
    assert "configured_standing_charge_p_per_day_inc_vat" in (out["notes"] or "")


def test_standing_charge_zero_warns_when_unconfigured(monkeypatch):
    out = _run_standing_charges(monkeypatch, [{"value_exc_vat": 0.0}])
    assert "configured_standing_charge_p_per_day_inc_vat" not in out
    assert "STANDING_CHARGE_ELECTRICITY" in (out["notes"] or "")


def test_standing_charge_gas_override(monkeypatch):
    out = _run_standing_charges(
        monkeypatch, [{"value_exc_vat": 0.0}], tariff="G-1R-TEST-26-01-01-A",
        standing_charge_gas=40.0,
    )
    assert out["fuel"] == "gas"
    assert out["configured_standing_charge_p_per_day_inc_vat"] == 40.0
    assert "STANDING_CHARGE_GAS" in (out["notes"] or "")


def test_standing_charge_shows_published_and_pinned_together(monkeypatch):
    out = _run_standing_charges(
        monkeypatch, SC_API_ROWS, standing_charge_electricity=50.88
    )
    assert out["standing_charges"][0]["value_exc_vat"] == 48.2442
    assert out["configured_standing_charge_p_per_day_inc_vat"] == 50.88
    assert "in preference to the published charge" in (out["notes"] or "")


def test_settings_standing_charge_validation():
    import pytest

    from octopus_mcp import config

    base = {"octopus_api_key": "x", "octopus_account_number": "A-TEST123"}
    assert config.Settings(**base).standing_charge_electricity is None
    assert (
        config.Settings(standing_charge_electricity="50.88", **base).standing_charge_electricity
        == 50.88
    )
    assert config.Settings(standing_charge_gas="", **base).standing_charge_gas is None
    with pytest.raises(ValueError, match="pence per day"):
        config.Settings(standing_charge_electricity=-1, **base)


# --- calculate_cost / compare_tariffs (billing-accurate, spec §1.2) --------


def _run_calculate_cost(monkeypatch, rates_by_tariff, sc_rows=(), tariff_code=None):
    import octopus_mcp.server as srv
    from octopus_mcp import config

    settings = config.Settings(
        octopus_api_key="x", octopus_account_number="A-TEST123",
        default_payment_method="DIRECT_DEBIT",
    )

    class FakeRest:
        async def get_all(self, url, **k):
            if "/consumption/" in url:
                return CONSUMPTION_ROWS
            if "standard-unit-rates" in url:
                for tc, rows in rates_by_tariff.items():
                    if tc in url:
                        return rows
                return []
            if "standing-charges" in url:
                return list(sc_rows)
            return []

    async def fake_account():
        return FAKE_ACCOUNT

    monkeypatch.setattr(srv, "get_settings", lambda: settings)
    monkeypatch.setattr(srv, "get_account", fake_account)
    monkeypatch.setattr(srv, "rest", lambda: FakeRest())
    kwargs = {"tariff_code": tariff_code} if tariff_code else {}
    return asyncio.run(
        srv.calculate_cost("2026-08-01T00:00:00Z", "2026-09-01T00:00:00Z", **kwargs)
    )


def test_calculate_cost_penny_breakdown(monkeypatch):
    out = _run_calculate_cost(
        monkeypatch,
        rates_by_tariff={
            "E-1R-TEST-26-01-01-C": [
                {"valid_from": "2026-07-01T00:00:00Z", "valid_to": None,
                 "value_exc_vat": 20.0, "payment_method": "DIRECT_DEBIT"},
            ]
        },
        sc_rows=[
            {"valid_from": "2026-07-01T00:00:00Z", "valid_to": None,
             "value": 45.0, "unit": "p/day"},
        ],
    )
    assert out["product_code"] == "TEST-26-01-01"
    assert out["tariff_code"] == "E-1R-TEST-26-01-01-C"
    assert out["mpan"] == "0001234567890"
    assert out["payment_method"] == "DIRECT_DEBIT"
    assert out["total_kwh"] == 3.0
    assert out["standing_charge_source"] == "api"
    # 3.0 kWh * 20p = 60p; SC 31d * 45p = 1395p; subtotal 1455p;
    # VAT round(72.75) = 73p; total 1528p
    assert out["cost_pence"] == {
        "unit_cost": 60,
        "standing_charge": 1395,
        "subtotal_ex_vat": 1455,
        "vat_5pct": 73,
        "total_inc_vat": 1528,
    }
    assert out["total_gbp"] == 15.28


def test_calculate_cost_tariff_override(monkeypatch):
    out = _run_calculate_cost(
        monkeypatch,
        rates_by_tariff={
            "E-1R-TEST-26-01-01-C": [],
            "E-1R-OTHER-26-01-01-A": [
                {"valid_from": "2026-07-01T00:00:00Z", "valid_to": None,
                 "value_exc_vat": 10.0, "payment_method": "DIRECT_DEBIT"},
            ],
        },
        tariff_code="E-1R-OTHER-26-01-01-A",
    )
    assert out["tariff_code"] == "E-1R-OTHER-26-01-01-A"
    assert out["product_code"] == "OTHER-26-01-01"
    assert out["cost_pence"]["unit_cost"] == 30
    assert out["total_gbp"] == 0.32  # 30p + VAT round(1.5) = 2p


def test_calculate_cost_gas(monkeypatch):
    import octopus_mcp.server as srv
    from octopus_mcp import config

    gas_account = {
        "properties": [
            {
                "id": "P-1",
                "address": {"line1": "1 Test St", "town": "Testville", "postcode": "TV1 1AA"},
                "gas_meter_points": [
                    {
                        "mprn": "1000000000001",
                        "region": "A",
                        "meters": [
                            {"serial_number": "G-1", "smets": "SMETS1",
                             "meter_status": "ACTIVE", "registers": []},
                        ],
                        "agreements": [
                            {"tariff_code": "G-1R-GASTAR-26-01-01-A",
                             "product_code": "GASTAR-26-01-01",
                             "valid_from": "2026-01-01T00:00:00Z",
                             "valid_to": None, "active": True},
                        ],
                    }
                ],
            }
        ]
    }
    settings = config.Settings(
        octopus_api_key="x", octopus_account_number="A-TEST123",
        default_payment_method="DIRECT_DEBIT",
    )

    class FakeRest:
        async def get_all(self, url, **k):
            if "/consumption/" in url:
                return CONSUMPTION_ROWS
            if "standard-unit-rates" in url:
                return [
                    {"valid_from": "2026-07-01T00:00:00Z", "valid_to": None,
                     "value_exc_vat": 10.0, "payment_method": "DIRECT_DEBIT"},
                ]
            if "standing-charges" in url:
                return [
                    {"valid_from": "2026-07-01T00:00:00Z", "valid_to": None,
                     "value": 40.0, "unit": "p/day"},
                ]
            return []

    async def fake_account():
        return gas_account

    monkeypatch.setattr(srv, "get_settings", lambda: settings)
    monkeypatch.setattr(srv, "get_account", fake_account)
    monkeypatch.setattr(srv, "rest", lambda: FakeRest())
    out = asyncio.run(
        srv.calculate_cost(
            "2026-08-01T00:00:00Z", "2026-09-01T00:00:00Z", mprn="1000000000001"
        )
    )
    assert out["fuel"] == "gas"
    assert out["mprn"] == "1000000000001"
    assert out["tariff_code"] == "G-1R-GASTAR-26-01-01-A"
    # 3.0 kWh * 10p = 30p; SC 31d * 40p = 1240p; subtotal 1270p;
    # VAT round(63.5) = 64p; total 1334p
    assert out["cost_pence"] == {
        "unit_cost": 30,
        "standing_charge": 1240,
        "subtotal_ex_vat": 1270,
        "vat_5pct": 64,
        "total_inc_vat": 1334,
    }
    assert out["total_gbp"] == 13.34


def _run_compare(monkeypatch, products, rates_by_tariff, candidates=None, sc_rows=()):
    import octopus_mcp.server as srv
    from octopus_mcp import config

    settings = config.Settings(
        octopus_api_key="x", octopus_account_number="A-TEST123",
        default_payment_method="DIRECT_DEBIT",
    )

    class FakeRest:
        async def get(self, url, **k):
            for code, definition in products.items():
                if code in url:
                    return definition
            raise srv.OctopusNotFoundError("No such product")

        async def get_all(self, url, **k):
            if "/consumption/" in url:
                return CONSUMPTION_ROWS
            if "standard-unit-rates" in url:
                for tc, rows in rates_by_tariff.items():
                    if tc in url:
                        return rows
                return []
            if "standing-charges" in url:
                return list(sc_rows)
            return []

    async def fake_account():
        return FAKE_ACCOUNT

    monkeypatch.setattr(srv, "get_settings", lambda: settings)
    monkeypatch.setattr(srv, "get_account", fake_account)
    monkeypatch.setattr(srv, "rest", lambda: FakeRest())
    return asyncio.run(
        srv.compare_tariffs(
            candidates or list(products),
            "2026-08-01T00:00:00Z", "2026-08-02T00:00:00Z",
        )
    )


def test_compare_tariffs_ranks_candidates(monkeypatch):
    dd = {"valid_from": "2026-07-01T00:00:00Z", "valid_to": None,
          "payment_method": "DIRECT_DEBIT"}
    products = {
        "FLX-24-01-01": {
            "code": "FLX-24-01-01",
            "single_register_electricity_tariffs": {
                "_C": {"varying": {"code": "E-1R-FLX-24-01-01-C"}},
            },
        },
        "AGILE-18-02-21": {
            "code": "AGILE-18-02-21",
            "single_register_electricity_tariffs": {
                "_C": {"varying": {"code": "E-1R-AGILE-18-02-21-C"}},
            },
        },
    }
    rates_by_tariff = {
        "E-1R-FLX-24-01-01-C": [dict(dd, value_exc_vat=15.0)],
        "E-1R-AGILE-18-02-21-C": [dict(dd, value_exc_vat=20.0)],
        "E-1R-TEST-26-01-01-C": [dict(dd, value_exc_vat=25.0)],  # baseline
    }
    out = _run_compare(monkeypatch, products, rates_by_tariff)
    assert [c["product_code"] for c in out["candidates"]] == [
        "FLX-24-01-01", "AGILE-18-02-21",
    ]
    assert [c["rank"] for c in out["candidates"]] == [1, 2]
    # 3.0 kWh: FLX 45p+2p VAT = 47p; AGILE 60p+3p = 63p; baseline 75p+4p = 79p
    assert out["candidates"][0]["total_gbp"] == 0.47
    assert out["candidates"][1]["total_gbp"] == 0.63
    assert out["candidates"][0]["delta_gbp_vs_baseline"] == -0.32
    assert out["candidates"][1]["delta_gbp_vs_baseline"] == -0.16
    assert out["baseline"]["tariff_code"] == "E-1R-TEST-26-01-01-C"
    assert out["baseline"]["total_gbp"] == 0.79
    assert out["total_kwh"] == 3.0


def test_compare_tariffs_unpricable_candidates_listed(monkeypatch):
    dd = {"valid_from": "2026-07-01T00:00:00Z", "valid_to": None,
          "payment_method": "DIRECT_DEBIT"}
    products = {
        "NORTH-ONLY-26-01-01": {
            "code": "NORTH-ONLY-26-01-01",
            "single_register_electricity_tariffs": {
                "_A": {"varying": {"code": "E-1R-NORTH-ONLY-26-01-01-A"}},
            },
        },
    }
    rates_by_tariff = {"E-1R-TEST-26-01-01-C": [dict(dd, value_exc_vat=25.0)]}
    out = _run_compare(
        monkeypatch, products, rates_by_tariff,
        candidates=["NORTH-ONLY-26-01-01", "GHOST-26-01-01"],
    )
    errors = {c["product_code"]: c["error"] for c in out["candidates"]}
    assert errors == {
        "NORTH-ONLY-26-01-01": "no_tariff_for_region",
        "GHOST-26-01-01": "not_found",
    }
    assert out["baseline"]["total_gbp"] == 0.79
    assert "None of the candidate tariffs" in (out["notes"] or "")


# --- Gas units (F-05) and non-mutating conversion (F-06) ------------------

GAS_ACCOUNT_NO_SMETS = {
    "properties": [
        {
            "id": 42,
            "address_line_1": "9 Test Street",
            "town": "TESTVILLE",
            "postcode": "TV1 1AA",
            "gas_meter_points": [
                {
                    "mprn": "1000000000001",
                    # The real /accounts/ payload carries no smets field --
                    # this is what makes the unit undetectable.
                    "meters": [{"serial_number": "G-1", "registers": []}],
                    "agreements": [
                        {"tariff_code": "G-1R-GASTAR-26-01-01-A",
                         "product_code": "GASTAR-26-01-01",
                         "valid_from": "2026-01-01T00:00:00Z",
                         "valid_to": None, "active": True},
                    ],
                }
            ],
        }
    ]
}


def _run_gas(monkeypatch, rows, **settings_kw):
    import octopus_mcp.server as srv
    from octopus_mcp import config

    settings = config.Settings(
        octopus_api_key="x", octopus_account_number="A-TEST123", **settings_kw
    )

    class FakeRest:
        async def get_all(self, url, **k):
            if "/consumption/" in url:
                return rows  # the same objects every call, as a cache would
            return []

    async def fake_account():
        return GAS_ACCOUNT_NO_SMETS

    monkeypatch.setattr(srv, "get_settings", lambda: settings)
    monkeypatch.setattr(srv, "get_account", fake_account)
    monkeypatch.setattr(srv, "rest", lambda: FakeRest())

    def call():
        return asyncio.run(
            srv.get_gas_consumption(
                "2026-08-01T00:00:00Z", "2026-08-01T01:00:00Z", group_by="day"
            )
        )

    return call


def test_gas_m3_converted_without_mutating_the_source_rows(monkeypatch):
    """F-05 pins the unit; F-06 makes sure converting it doesn't rewrite the
    cached rows -- doing that compounded the factor on every later read."""
    from octopus_mcp import shaping

    rows = [dict(r) for r in CONSUMPTION_ROWS]  # 1.0 + 2.0
    call = _run_gas(monkeypatch, rows, octopus_gas_units="m3")
    first, second = call(), call()

    factor = shaping.m3_to_kwh_factor(39.5)
    assert first["total"] == round(3.0 * factor, 3)
    assert second["total"] == first["total"]  # not 11x larger the second time
    assert [r["consumption"] for r in rows] == [1.0, 2.0]  # untouched
    assert "converted to kWh" in first["notes"]


def test_gas_calorific_value_setting_changes_the_factor(monkeypatch):
    from octopus_mcp import shaping

    rows = [dict(r) for r in CONSUMPTION_ROWS]
    out = _run_gas(
        monkeypatch, rows, octopus_gas_units="m3", gas_calorific_value=38.0
    )()
    assert out["total"] == round(3.0 * shaping.m3_to_kwh_factor(38.0), 3)


def test_gas_warns_when_the_unit_is_unknown(monkeypatch):
    """Unpinned, the reading is left alone (the documented kWh default) and
    the response says so -- silently multiplying by ~11 would be worse."""
    rows = [dict(r) for r in CONSUMPTION_ROWS]
    out = _run_gas(monkeypatch, rows)()
    assert out["total"] == 3.0
    assert "OCTOPUS_GAS_UNITS" in out["notes"]


# --- F-07 / F-08 / F-09 / F-12: account shape, export meters, pinning -----

# Shaped like the real /accounts/ payload -- address fields on the property, no
# nested "address" object, no region/smets/direction, export flagged by
# is_export -- but with invented identifiers and address. Nothing in this repo
# should carry a real meter point: an MPAN plus half-hourly consumption is a
# record of when a specific home is occupied.
REAL_SHAPE_ACCOUNT = {
    "properties": [
        {
            "id": 40000001,
            "address_line_1": "1, EXAMPLE ROAD",
            "address_line_2": "",
            "town": "TESTBURY",
            "county": "",
            "postcode": "TE1 1ST",
            "electricity_meter_points": [
                {
                    "mpan": "1234567890123",
                    "is_export": False,
                    "profile_class": 1,
                    "consumption_standard": 5647,
                    "meters": [{"serial_number": "IMPORT-1", "registers": []}],
                    "agreements": [
                        {"tariff_code": "E-1R-VAR-22-11-01-A", "valid_from": "2026-04-08T00:00:00+01:00",
                         "valid_to": None},
                    ],
                },
                {
                    "mpan": "1234567890124",
                    "is_export": True,
                    "meters": [{"serial_number": "EXPORT-1", "registers": []}],
                    "agreements": [
                        {"tariff_code": "E-1R-OUTGOING-FIX-12M-19-05-13-A",
                         "valid_from": "2026-04-08T00:00:00+01:00", "valid_to": None},
                    ],
                },
            ],
        }
    ]
}


def _points(account=None):
    import octopus_mcp.server as srv

    return list(srv.iter_meter_points(account or REAL_SHAPE_ACCOUNT))


def _settings(monkeypatch, **kw):
    import octopus_mcp.server as srv
    from octopus_mcp import config

    settings = config.Settings(octopus_api_key="x", octopus_account_number="A-TEST123", **kw)
    monkeypatch.setattr(srv, "get_settings", lambda: settings)
    return settings


def test_meter_points_read_the_real_address_and_region(monkeypatch):
    _settings(monkeypatch)
    imp = _points()[0]
    assert imp["address"] == "1, EXAMPLE ROAD, TESTBURY, TE1 1ST"
    assert imp["region"] == "A"  # from the tariff code suffix; the API sends none
    assert imp["direction"] == "IMPORT"
    assert imp["consumption_standard"] == 5647
    assert imp["profile_class"] == 1


def test_export_meter_is_labelled_and_kept_out_of_import_tools(monkeypatch):
    import octopus_mcp.server as srv

    _settings(monkeypatch)
    points = _points()
    assert [p["direction"] for p in points] == ["IMPORT", "EXPORT"]

    # Two electricity meter points, but only one is an import meter: this used
    # to be an ambiguous "multiple_meter_points" or, worse, the export meter.
    resolved, problem = srv.resolve_meter(points, "electricity")
    assert problem is None
    assert resolved["point"]["mpan"] == "1234567890123"

    resolved, problem = srv.resolve_meter(points, "electricity", direction="EXPORT")
    assert problem is None
    assert resolved["point"]["mpan"] == "1234567890124"
    assert resolved["serial"] == "EXPORT-1"


def test_no_export_meter_explains_itself(monkeypatch):
    import octopus_mcp.server as srv

    _settings(monkeypatch)
    account = {"properties": [dict(REAL_SHAPE_ACCOUNT["properties"][0])]}
    account["properties"][0]["electricity_meter_points"] = [
        REAL_SHAPE_ACCOUNT["properties"][0]["electricity_meter_points"][0]
    ]
    resolved, problem = srv.resolve_meter(_points(account), "electricity", direction="EXPORT")
    assert resolved is None
    assert problem["error"] == "no_meter"


def test_pinned_mpan_resolves_instead_of_asking(monkeypatch):
    """The OCTOPUS_*_MPAN / _SERIAL settings were documented everywhere and
    read nowhere, so pinning a meter did nothing."""
    import octopus_mcp.server as srv

    two_imports = {
        "properties": [
            {
                "id": 1,
                "address_line_1": "1 Test St",
                "postcode": "TV1 1AA",
                "electricity_meter_points": [
                    {"mpan": "1111111111111", "is_export": False,
                     "meters": [{"serial_number": "A-1", "registers": []}], "agreements": []},
                    {"mpan": "2222222222222", "is_export": False,
                     "meters": [{"serial_number": "B-1", "registers": []}], "agreements": []},
                ],
            }
        ]
    }
    _settings(monkeypatch)
    resolved, problem = srv.resolve_meter(_points(two_imports), "electricity")
    assert resolved is None and problem["error"] == "multiple_meter_points"

    _settings(monkeypatch, octopus_electricity_mpan="2222222222222")
    resolved, problem = srv.resolve_meter(_points(two_imports), "electricity")
    assert problem is None
    assert resolved["point"]["mpan"] == "2222222222222"


def test_unknown_serial_is_an_error_not_a_silent_swap(monkeypatch):
    import octopus_mcp.server as srv

    _settings(monkeypatch)
    resolved, problem = srv.resolve_meter(_points(), "electricity", serial="NOT-MINE")
    assert resolved is None
    assert problem["error"] == "unknown_serial"
    assert problem["options"] == ["IMPORT-1"]


def test_unknown_mpan_is_an_error(monkeypatch):
    import octopus_mcp.server as srv

    _settings(monkeypatch)
    resolved, problem = srv.resolve_meter(_points(), "electricity", mpan="9999999999999")
    assert resolved is None
    assert problem["error"] == "unknown_meter"


def test_property_id_filter_accepts_a_string(monkeypatch):
    """The API's property id is an integer; the tool takes a string."""
    import octopus_mcp.server as srv

    _settings(monkeypatch)

    async def fake_account():
        return REAL_SHAPE_ACCOUNT

    monkeypatch.setattr(srv, "get_account", fake_account)
    out = asyncio.run(srv.list_meter_points(property_id="40000001"))
    assert out["count"] == 2


def _run_consumption(monkeypatch, rows, **kwargs):
    import octopus_mcp.server as srv

    class FakeRest:
        async def get_all(self, url, **k):
            return rows if "/consumption/" in url else []

    async def fake_account():
        return REAL_SHAPE_ACCOUNT

    monkeypatch.setattr(srv, "get_account", fake_account)
    monkeypatch.setattr(srv, "rest", lambda: FakeRest())
    return asyncio.run(srv.get_electricity_consumption(**kwargs))


def test_include_raw_refuses_a_range_that_would_dump_thousands_of_rows(monkeypatch):
    _settings(monkeypatch)
    out = _run_consumption(
        monkeypatch, CONSUMPTION_ROWS,
        period_from="2026-08-01T00:00:00Z", period_to="2026-08-31T00:00:00Z",
        group_by="half_hour", include_raw=True,
    )
    assert out["error"] == "range_too_wide_for_raw"
    assert "MAX_RAW_DAYS" in out["message"]


def test_include_raw_allows_a_short_range_and_caps_the_rows(monkeypatch):
    _settings(monkeypatch, max_rows_returned=1)
    out = _run_consumption(
        monkeypatch, CONSUMPTION_ROWS,
        period_from="2026-08-01T00:00:00Z", period_to="2026-08-01T01:00:00Z",
        group_by="half_hour", include_raw=True,
    )
    assert out["include_raw"] is True
    assert len(out["series"]) == 1  # capped by MAX_ROWS_RETURNED
    assert "truncated" in out["notes"]
    assert out["stats"]["n"] == 2  # stats still cover everything


def test_plain_dates_are_accepted_and_buckets_are_uk_local(monkeypatch):
    _settings(monkeypatch)
    out = _run_consumption(
        monkeypatch, CONSUMPTION_ROWS,
        period_from="2026-08-01", period_to="2026-08-01", group_by="day",
    )
    assert out["timezone"] == "Europe/London"
    assert out["series"][0]["start"].endswith("+01:00")


# --- S-01: transport security is on, and configurable --------------------


def test_dns_rebinding_protection_is_on_by_default(monkeypatch):
    """It was switched off wholesale because NGINX rewrites Host. That let any
    web page fetch() the account out of localhost."""
    import octopus_mcp.server as srv

    monkeypatch.delenv("MCP_ALLOWED_HOSTS", raising=False)
    monkeypatch.delenv("MCP_ALLOWED_ORIGINS", raising=False)
    security = srv._transport_security()
    assert security.enable_dns_rebinding_protection is True
    assert "localhost:*" in security.allowed_hosts
    assert "127.0.0.1:*" in security.allowed_hosts
    assert "http://localhost:*" in security.allowed_origins


def test_extra_hosts_come_from_the_environment(monkeypatch):
    """Reaching the server by any other name -- a tailnet name, say -- is
    opt-in rather than wide open."""
    import octopus_mcp.server as srv

    monkeypatch.setenv("MCP_ALLOWED_HOSTS", "brown.tailnet.ts.net, mcp:8000 ,")
    monkeypatch.setenv("MCP_ALLOWED_ORIGINS", "https://brown.tailnet.ts.net")
    security = srv._transport_security()
    assert "brown.tailnet.ts.net" in security.allowed_hosts
    assert "mcp:8000" in security.allowed_hosts
    assert "" not in security.allowed_hosts
    assert "https://brown.tailnet.ts.net" in security.allowed_origins


def test_host_validation_accepts_loopback_and_rejects_anything_else(monkeypatch):
    """Exercise the SDK's own validator with our settings, so this test fails
    if the allow-list stops doing what it is supposed to."""
    from mcp.server.transport_security import TransportSecurityMiddleware

    import octopus_mcp.server as srv

    monkeypatch.delenv("MCP_ALLOWED_HOSTS", raising=False)
    monkeypatch.delenv("MCP_ALLOWED_ORIGINS", raising=False)
    mw = TransportSecurityMiddleware(srv._transport_security())

    assert mw._validate_host("localhost:8080")
    assert mw._validate_host("127.0.0.1:8000")
    assert not mw._validate_host("evil.example.com")
    assert not mw._validate_host("brown.tailnet.ts.net")

    assert mw._validate_origin(None)  # non-browser clients send no Origin
    assert mw._validate_origin("http://localhost:8080")
    assert not mw._validate_origin("https://evil.example.com")


# --- F-17: a tariff that only covers part of the period must not be ranked


def test_partial_rate_coverage_is_not_ranked(monkeypatch):
    """A product that launched mid-period publishes no rates for the days
    before it existed. Totalling only what it can price ranked it first, as
    though the rest of the month were free."""
    products = {
        "FULL-26-01-01": {
            "single_register_electricity_tariffs": {"_C": {"varying": {"code": "E-1R-FULL-26-01-01-C"}}}
        },
        "LATE-26-08-01": {
            "single_register_electricity_tariffs": {"_C": {"varying": {"code": "E-1R-LATE-26-08-01-C"}}}
        },
    }
    rates = {
        "E-1R-FULL-26-01-01-C": [
            {"valid_from": "2026-07-01T00:00:00Z", "valid_to": None,
             "value_exc_vat": 30.0, "payment_method": "DIRECT_DEBIT"},
        ],
        # Launched halfway through: nothing before 00:30, so the 00:00 interval
        # cannot be priced. Cheap enough that it would win if it were ranked.
        "E-1R-LATE-26-08-01-C": [
            {"valid_from": "2026-08-01T00:30:00Z", "valid_to": None,
             "value_exc_vat": 1.0, "payment_method": "DIRECT_DEBIT"},
        ],
    }
    out = _run_compare(monkeypatch, products, rates)
    by_code = {c["product_code"]: c for c in out["candidates"]}

    late = by_code["LATE-26-08-01"]
    assert late["error"] == "incomplete_rate_coverage"
    assert "rank" not in late
    assert abs(late["priced_share"] - 2 / 3) < 0.01
    assert "launched partway" in late["message"]

    full = by_code["FULL-26-01-01"]
    assert full["rank"] == 1  # the only candidate that could be priced at all


# --- list_products / get_product: the tariff catalogue ---------------------

CATALOGUE = [
    {"code": "GO-VAR-22-10-14", "display_name": "Octopus Go", "direction": "IMPORT",
     "brand": "OCTOPUS_ENERGY", "is_variable": True, "is_green": False, "is_business": False,
     "is_prepay": False, "is_tracker": False, "available_from": "2022-10-14", "available_to": None},
    {"code": "VAR-22-11-01", "display_name": "Flexible Octopus", "direction": "IMPORT",
     "brand": "OCTOPUS_ENERGY", "is_variable": True, "is_green": True, "is_business": False,
     "is_prepay": False, "is_tracker": False, "available_from": "2022-11-01", "available_to": None},
    {"code": "GONE-20-01-01", "display_name": "Withdrawn Tariff", "direction": "IMPORT",
     "brand": "OCTOPUS_ENERGY", "is_variable": False, "is_green": False, "is_business": False,
     "is_prepay": False, "is_tracker": False, "available_from": "2020-01-01",
     "available_to": "2021-01-01"},
    {"code": "BIZ-24-01-01", "display_name": "Business Fixed", "direction": "IMPORT",
     "brand": "OCTOPUS_ENERGY", "is_business": True, "available_from": "2024-01-01",
     "available_to": None},
    {"code": "OUTGOING-VAR-24-10-26", "display_name": "Outgoing Octopus", "direction": "EXPORT",
     "brand": "OCTOPUS_ENERGY", "is_business": False, "available_from": "2024-10-26",
     "available_to": None},
]

PRODUCT_DETAIL = {
    "code": "GO-VAR-22-10-14",
    "display_name": "Octopus Go",
    "full_name": "Octopus Go October 2022 v1",
    "description": "Cheap overnight electricity for EV drivers.",
    "brand": "OCTOPUS_ENERGY",
    "direction": "IMPORT",
    "term": None,
    "is_variable": True,
    "available_from": "2022-10-14",
    "available_to": None,
    "single_register_electricity_tariffs": {
        "_A": {"direct_debit_monthly": {
            "code": "E-1R-GO-VAR-22-10-14-A",
            "standing_charge_exc_vat": 51.3256, "standing_charge_inc_vat": 53.89188,
            "standard_unit_rate_exc_vat": 29.6946, "standard_unit_rate_inc_vat": 31.17933,
            "exit_fees_inc_vat": 0.0}},
        "_C": {"direct_debit_monthly": {
            "code": "E-1R-GO-VAR-22-10-14-C",
            "standing_charge_inc_vat": 49.0, "standard_unit_rate_inc_vat": 30.0}},
    },
    "dual_register_electricity_tariffs": {},
}


def _run_catalogue(monkeypatch, tool, account=None, **kwargs):
    import octopus_mcp.server as srv

    class FakeRest:
        async def get_all(self, url, **k):
            return list(CATALOGUE)

        async def get(self, url, **k):
            return dict(PRODUCT_DETAIL)

    async def fake_account():
        if account is None:
            raise srv.OctopusAuthError("no account")
        return account

    _settings(monkeypatch)
    monkeypatch.setattr(srv, "get_account", fake_account)
    monkeypatch.setattr(srv, "rest", lambda: FakeRest())
    return asyncio.run(getattr(srv, tool)(**kwargs))


def test_list_products_defaults_to_available_domestic_import(monkeypatch):
    out = _run_catalogue(monkeypatch, "list_products")
    codes = [p["product_code"] for p in out["products"]]
    assert codes == ["GO-VAR-22-10-14", "VAR-22-11-01"]
    assert out["catalogue_size"] == 5          # withdrawn, business and export filtered out
    assert "GONE-20-01-01" not in codes
    assert "BIZ-24-01-01" not in codes
    assert "OUTGOING-VAR-24-10-26" not in codes


def test_list_products_query_and_filters(monkeypatch):
    assert [p["product_code"] for p in
            _run_catalogue(monkeypatch, "list_products", query="go")["products"]] == ["GO-VAR-22-10-14"]
    assert [p["product_code"] for p in
            _run_catalogue(monkeypatch, "list_products", direction="EXPORT")["products"]] \
        == ["OUTGOING-VAR-24-10-26"]
    assert "GONE-20-01-01" in [p["product_code"] for p in
                               _run_catalogue(monkeypatch, "list_products",
                                              available_only=False)["products"]]
    assert [p["product_code"] for p in
            _run_catalogue(monkeypatch, "list_products", is_green=True)["products"]] == ["VAR-22-11-01"]


def test_get_product_defaults_to_the_accounts_region(monkeypatch):
    out = _run_catalogue(monkeypatch, "get_product", account=REAL_SHAPE_ACCOUNT,
                         product_code="GO-VAR-22-10-14")
    assert out["region"] == "A"                      # derived from the account's tariff code
    assert "this account's region" in out["notes"]
    tariff = out["tariffs"]["electricity_single_register"]["direct_debit_monthly"]
    assert tariff["tariff_code"] == "E-1R-GO-VAR-22-10-14-A"
    assert tariff["standing_charge_p_day_inc_vat"] == 53.89188
    assert out["available_regions"] == ["A", "C"]
    assert "get_unit_rates" in out["notes"]          # headline rate is not the cheap window


def test_get_product_explicit_region_and_unknown_region(monkeypatch):
    out = _run_catalogue(monkeypatch, "get_product", account=REAL_SHAPE_ACCOUNT,
                         product_code="GO-VAR-22-10-14", region="c")
    assert out["region"] == "C"
    assert out["tariffs"]["electricity_single_register"]["direct_debit_monthly"]["tariff_code"] \
        == "E-1R-GO-VAR-22-10-14-C"

    missing = _run_catalogue(monkeypatch, "get_product", account=REAL_SHAPE_ACCOUNT,
                             product_code="GO-VAR-22-10-14", region="P")
    assert missing["error"] == "region_not_offered"
    assert missing["available_regions"] == ["A", "C"]


def test_get_product_asks_for_a_region_when_it_cannot_tell(monkeypatch):
    out = _run_catalogue(monkeypatch, "get_product", product_code="GO-VAR-22-10-14")
    assert out["error"] == "region_required"
    assert out["available_regions"] == ["A", "C"]
