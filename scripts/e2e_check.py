"""E2E check: full MCP client session through the NGINX front.

Usage: python scripts/e2e_check.py [url]
Default URL is the compose stack: http://127.0.0.1:8080/mcp

Exercises every tool against the live Octopus API. Fails if any call
returns an error — the server reports API errors as a normal result with
an "error" key, not as a protocol error, so both shapes are checked.
"""

import asyncio
import json
import sys

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8080/mcp"

PERIOD_FROM = "2026-08-31T00:00:00Z"
PERIOD_TO = "2026-09-03T00:00:00Z"


async def call(session, name: str, args: dict):
    res = await session.call_tool(name, args)
    if res.is_error:
        raise SystemExit(f"FAIL {name}: isError — {res.content[0].text[:300]}")
    payload = json.loads(res.content[0].text)
    if "error" in payload:
        raise SystemExit(f"FAIL {name}: {payload['error']}")
    return payload


def first_tariff(ags: dict) -> dict:
    for p in ags["meter_points"]:
        list_ = p["agreements"]
        if not list_:
            continue
        return next((a for a in list_ if a.get("active")), list_[0])
    raise SystemExit("FAIL: get_agreements returned no agreements")


async def main() -> None:
    async with streamable_http_client(URL) as (read, write):
        async with ClientSession(read, write) as session:
            info = await session.initialize()
            print(f"initialize OK: {info.server_info.name} v{info.server_info.version}")

            tools = await session.list_tools()
            names = [t.name for t in tools.tools]
            print(f"list_tools OK: {len(names)} tools: {', '.join(names)}")
            assert "get_current_datetime" in names

            dt = await call(session, "get_current_datetime", {})
            if "utc" not in dt or "europe_london" not in dt:
                raise SystemExit(f"FAIL get_current_datetime: missing utc/europe_london: {dt}")
            print(
                f"get_current_datetime OK: utc={dt['utc']['datetime']} "
                f"london={dt['europe_london']['datetime']} ({dt['europe_london']['utc_offset']})"
            )

            pts = await call(session, "list_meter_points", {})
            print(f"list_meter_points OK: {pts['count']} meter point(s)")

            ags = await call(session, "get_agreements", {})
            ag = first_tariff(ags)
            print(
                f"get_agreements OK: {ags['count']} meter point(s); "
                f"tariff {ag['tariff_code']} (product {ag['product_code']})"
            )

            cons = await call(
                session,
                "get_electricity_consumption",
                {
                    "period_from": PERIOD_FROM,
                    "period_to": PERIOD_TO,
                    "group_by": "day",
                },
            )
            print(f"get_electricity_consumption OK: keys={sorted(cons.keys())}")

            sc = await call(
                session,
                "get_standing_charges",
                {
                    "product_code": ag["product_code"],
                    "tariff_code": ag["tariff_code"],
                },
            )
            cfg_sc = sc.get("configured_standing_charge_p_per_day")
            extra = f", configured SC={cfg_sc} p/day" if cfg_sc is not None else ""
            print(f"get_standing_charges OK: keys={sorted(sc.keys())}{extra}")

            rates = await call(
                session,
                "get_unit_rates",
                {
                    "product_code": ag["product_code"],
                    "tariff_code": ag["tariff_code"],
                    "period_from": PERIOD_FROM,
                    "period_to": PERIOD_TO,
                },
            )
            stats = rates.get("stats") or {}
            print(
                f"get_unit_rates OK: {stats.get('count')} rate(s), "
                f"mean={stats.get('mean')} p/kWh inc VAT"
            )

            cost = await call(
                session,
                "calculate_cost",
                {"period_from": PERIOD_FROM, "period_to": PERIOD_TO},
            )
            if "total_gbp" not in cost:
                raise SystemExit(f"FAIL calculate_cost: not priced — {cost}")
            print(
                f"calculate_cost OK: {cost.get('total_kwh')} kWh total, "
                f"{cost.get('billed_kwh')} kWh billed, {cost.get('total_gbp')} GBP "
                f"(SC via {cost.get('standing_charge_source')})"
            )
            if cost.get("notes"):
                print(f"  note: {cost['notes']}")

            cmp_res = await call(
                session,
                "compare_tariffs",
                {
                    "candidate_product_codes": ["AGILE-18-02-21", ag["product_code"]],
                    "period_from": PERIOD_FROM,
                    "period_to": PERIOD_TO,
                },
            )
            candidates = cmp_res.get("candidates", [])
            priced = [c for c in candidates if "total_gbp" in c]
            if not priced:
                raise SystemExit(f"FAIL compare_tariffs: no candidate priced — {cmp_res}")
            baseline = cmp_res.get("baseline", {})
            base_price = (
                f"{baseline['total_gbp']} GBP"
                if "total_gbp" in baseline
                else f"({baseline.get('error')})"
            )
            print(
                f"compare_tariffs OK: {len(priced)}/{len(candidates)} priced; "
                f"baseline {baseline.get('product_code')} = {base_price}"
            )
            for c in sorted(priced, key=lambda x: x.get("rank", 99)):
                print(
                    f"  rank {c.get('rank')}: {c['product_code']} "
                    f"({c.get('tariff_code')}) = {c['total_gbp']} GBP, "
                    f"delta_vs_baseline={c.get('delta_gbp_vs_baseline')}"
                )

    print("E2E PASS")


if __name__ == "__main__":
    asyncio.run(main())
