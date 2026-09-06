# Octopus Energy MCP Server (MVP)

A **read-only** MCP server exposing Octopus Energy (UK) consumer data:
half-hourly electricity & gas consumption, and tariff unit rates + standing
charges. Built per `Octopus MCP Spec.md` — this is the Phase 1 (REST-only) MVP.

- **No GraphQL, no token management** yet (REST is stable, cacheable, and needs
  no token for price/tariff data).
- **Strictly read-only.** The account API key grants full account access, so the
  server exposes *no* mutation tools (no tariff changes, payments, points, etc.).
- **Response shaping built in.** Half-hourly data is aggregated + summarised
  (stats, missing-interval counts) rather than dumped as thousands of rows.

## Tools

| Tool | Purpose |
|---|---|
| `get_current_datetime` | Current date/time in UTC **and** Europe/London (UK), with weekday + UTC offset — for resolving "today"/"this week" |
| `get_electricity_consumption` | Electricity usage, **half-hour (30 min) floor** up to monthly, with stats, bucketed by UK local day |
| `get_gas_consumption` | Gas usage; converted from m³ to kWh when the meter reports m³ (see `OCTOPUS_GAS_UNITS`) |
| `get_export_consumption` | Solar/battery export on the export MPAN, in kWh or as earnings (`unit="GBP"`) |
| `get_unit_rates` | Tariff unit rates in p/kWh — fixed or Agile (half-hourly) |
| `get_standing_charges` | Standing charge history in p/day, ex and inc VAT, per payment method |
| `list_meter_points` | Discover MPANs/MPRNs, meters, region, and active tariffs |
| `get_agreements` | Current + historical tariff agreements (gives `product_code`/`tariff_code`) |
| `calculate_cost` | Invoice-accurate cost over a period as a pence breakdown: kWh rounded per half-hour, priced at the exc-VAT rate, standing charge added, 5% VAT on top. Reconciles to within a penny of a real monthly bill |
| `compare_tariffs` | Prices candidate products against this account's actual consumption and ranks them cheapest-first, with `delta_gbp_vs_baseline` vs your current tariff (region resolved from the active tariff) |

A typical flow: `list_meter_points` / `get_agreements` to find your MPAN and
active tariff, then `get_electricity_consumption` for usage and
`get_unit_rates` + `get_standing_charges` for the tariff price. For an
invoice-accurate total over a period, use `calculate_cost`; to check whether
switching products would save money, use `compare_tariffs`.

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate        # Windows   (venv/bin/activate on macOS/Linux)
pip install -e ".[dev]"
```

Create a `.env` (copy `.env.example`):

```
OCTOPUS_API_KEY=...
OCTOPUS_ACCOUNT_NUMBER=A-...
```

## Run

```bash
octopus-mcp             # streamable-HTTP (default) on 127.0.0.1:8000
octopus-mcp --stdio     # stdio transport for local MCP clients
# or
python -m octopus_mcp   # same as the console script
```

HTTP bind is configurable via `MCP_HOST` / `MCP_PORT` (see `.env.example`).

## MCP client config (examples)

stdio (the client launches the server):

```json
{
  "mcpServers": {
    "octopus": {
      "command": "C:/path/to/project/.venv/Scripts/octopus-mcp.exe",
      "args": ["--stdio"],
      "env": {
        "OCTOPUS_API_KEY": "...",
        "OCTOPUS_ACCOUNT_NUMBER": "A-..."
      }
    }
  }
}
```

streamable-HTTP (server already running, e.g. the Docker stack below):

```json
{
  "mcpServers": {
    "octopus": {
      "url": "http://localhost:8080/mcp"
    }
  }
}
```

## Docker deployment

Both containers run on **Chainguard** base images, nonroot. The `mcp` image
is the CI build from GHCR — `compose.yaml` sets `pull_policy: always`, so
every `docker compose up` fetches the latest one:

| Container | Image | Role |
|---|---|---|
| `mcp` | `ghcr.io/ngfw-automation/octopus-energy-mcp-server:latest` | the MCP server, streamable-HTTP on `:8000` |
| `nginx` | `chainguard/nginx` | front on `:8080`, proxies `/mcp` → `mcp:8000`; SSE buffering off, CORS for MCP clients |

TLS is a placeholder (`:443` with `ssl_certificate` paths) — swap in real
certificates in `deploy/nginx.conf` before exposing publicly.

```bash
docker compose up -d    # pulls the latest GHCR image, then starts
docker compose ps               # mcp container reports (healthy)
docker compose logs -f          # follow logs
```

Point MCP clients at `http://<host>:8080/mcp`.

E2E check against a running stack (needs real credentials in `.env`):

```bash
.venv\Scripts\python scripts\e2e_check.py   # default: http://127.0.0.1:8080/mcp
```

GitHub Actions builds the `mcp` image from `Dockerfile` on every push to
`main` and publishes it to GHCR — `:latest` plus SHA tags — at
`ghcr.io/ngfw-automation/octopus-energy-mcp-server`.

## Exposure

**The MCP endpoint has no authentication.** Anything that can reach it can read
your consumption, tariff, meter identifiers and address — your API key never
leaves the server, but everything it fetches is readable. Two things follow:

- **The published port is bound to loopback** (`127.0.0.1:8080:8080`). Changing
  that to `8080:8080` hands your energy data to every device on your network,
  including guest Wi-Fi. Don't, unless you have put authentication in front of
  it yourself.
- **Host and Origin headers are validated** (DNS-rebinding protection). Without
  it, a web page you visit could `fetch()` your account out of `localhost` in
  the background. `localhost` and `127.0.0.1` on any port are allowed; anything
  else — a tailnet name, a LAN hostname — has to be added to
  `MCP_ALLOWED_HOSTS`. A rejected request is logged with the exact `Host` it
  carried, which is the value to add.

The API key itself grants **full account access**, including the mutations this
server deliberately does not expose. Treat `.env` as a secret, and rotate the
key if it has ever been shared.

### Remote access, without publishing anything

To use the server from another machine or a phone, leave the port on loopback
and put [Tailscale](https://tailscale.com/docs/features/tailscale-serve) in
front of it. Nothing is exposed to your LAN or the internet — only devices in
your own tailnet can connect, and they get a real TLS certificate.

On the machine running the stack (Windows: an Administrator terminal, no `sudo`):

```bash
sudo tailscale serve --bg 8080
# Available within your tailnet:
# https://<device>.<tailnet>.ts.net
# |-- / proxy http://127.0.0.1:8080
```

Then tell the server to answer to that name and restart it:

```
MCP_ALLOWED_HOSTS=<device>.<tailnet>.ts.net
```

Point remote MCP clients at `https://<device>.<tailnet>.ts.net/mcp`.
`tailscale serve off` stops it.

Two warnings. `tailscale funnel` is the same thing **published to the whole
internet** — with no authentication on this endpoint, that is equivalent to
posting your energy data publicly; don't use it here. And a tailnet is a trust
boundary, not an authentication mechanism: every device on it can read the
account, so this is right for your own devices and wrong for anything shared.

## Notes & caveats

- **Finest granularity is half-hourly (30 min).** Anything finer (10-second
  live telemetry) requires a Home Mini and the GraphQL API — not in this MVP.
- **Settlement data lags.** Half-hourly consumption usually lands the morning
  after, and gaps happen; the `stats.missing_intervals` field tells you how
  many half-hours are missing in the range.
- **Gas units are not detectable.** SMETS1 meters report kWh, SMETS2 meters
  report m³, and the account payload says which for neither. Set
  `OCTOPUS_GAS_UNITS=m3` (or pass `source_unit`) if yours reports m³;
  otherwise kWh is assumed and every gas response says so. The conversion is
  `m³ × CV × 1.02264 / 3.6` with `GAS_CALORIFIC_VALUE` (39.5 → ~11.22 kWh/m³);
  your bill gives the exact calorific value.
- **Dates and timezone:** pass a plain date (`2026-08-01`) and it is read as
  UK local — `period_to` covers that whole day — or a UTC timestamp
  (`2026-08-01T00:00:00Z`) if you want exact instants. **Buckets are UK local**
  (Europe/London), so a "day" is the day your bill charges for and the clocks-
  change days are correctly 23 and 25 hours long. Series timestamps carry their
  offset. Bad or reversed ranges are rejected before any request is made.
- **Standing charge:** taken from the tariff's published charge, matched to
  your payment method. If your bill shows a different figure, pin it with
  `STANDING_CHARGE_ELECTRICITY` / `STANDING_CHARGE_GAS` in **VAT-inclusive
  pence per day**, exactly as the dashboard shows it; a pinned value takes
  precedence and the response says so.
- **Multiple meters:** if a fuel has more than one meter point and you haven't
  pinned/supplied one, the tool returns a disambiguation list instead of guessing.
  `OCTOPUS_ELECTRICITY_MPAN` / `_SERIAL`, `OCTOPUS_GAS_MPRN` / `_SERIAL` and
  `OCTOPUS_EXPORT_MPAN` pin the default; an MPAN or serial that isn't on the
  account is an error rather than a silent fall back to another meter.
- **Export meters** are identified by the account's `is_export` flag, kept out
  of the import tools, and read by `get_export_consumption`.
- **Raw rows:** `include_raw` is capped at `MAX_RAW_DAYS` (default 2) — a month
  of half-hours is ~1,500 rows. Use a coarser `group_by` for wider ranges.
- **Logs:** one JSON line per MCP request and upstream call on stderr
  (`LOG_LEVEL`, default INFO). The API key is redacted and account/meter
  identifiers are masked.

## Tests

```bash
pip install -e ".[dev]"
pytest -q
```
