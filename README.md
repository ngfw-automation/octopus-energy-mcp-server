# Octopus Energy MCP Server

Read your own Octopus Energy account from an AI chat app. Point Claude — or
any other app that speaks MCP — at this server and ask about your electricity
and gas in plain English:

> *"How much electricity did I use last month, and what did it cost?"*
> *"Which day last week was my most expensive, and why?"*
> *"What tariffs is Octopus offering right now?"*
> *"I'm getting an EV. Would Octopus Go actually be cheaper for me?"*
> *"When is electricity cheapest tomorrow?"*

The server fetches half-hourly consumption, tariff rates and standing charges
straight from Octopus's public API, and does the billing arithmetic itself —
per-half-hour rounding, standing charge, 5% VAT — so a monthly total lands
within a penny of the real bill.

It is **strictly read-only**. There is no tool here that can change your
tariff, spend your money, or touch your account in any way.

## Contents

The three parts below are written for three different readers. Start at the
one that sounds like you; each is self-contained.

- **[Part 1 — For everyone: get it working](#part-1--for-everyone-get-it-working)**
  You use a chat app and would like it to answer questions about your energy.
  No programming needed.
  [What you'll need](#what-youll-need) ·
  [Set it up](#set-it-up) ·
  [Connect your chat app](#connect-your-chat-app) ·
  [First questions](#first-questions-to-try) ·
  [When something goes wrong](#when-something-goes-wrong) ·
  [What it can and can't do](#what-it-can-and-cant-do)
- **[Part 2 — For the tinkerer: run it properly](#part-2--for-the-tinkerer-run-it-properly)**
  You are comfortable with a terminal, a JSON config file and environment
  variables, and you want to know what the knobs do.
  [Tools](#tools) ·
  [Transports](#transports-and-client-config) ·
  [Configuration](#configuration) ·
  [Docker](#docker-deployment) ·
  [Exposure](#exposure) ·
  [Remote access](#remote-access-without-publishing-anything) ·
  [Behaviour and caveats](#behaviour-and-caveats)
- **[Part 3 — For the ninja: how it works inside](#part-3--for-the-ninja-how-it-works-inside)**
  You want to read the code, change it, or borrow from it.
  [Architecture](#architecture) ·
  [The billing arithmetic](#the-billing-arithmetic) ·
  [HTTP layer](#the-http-layer-cache-single-flight-retries) ·
  [Transport security](#transport-security-internals) ·
  [Observability](#observability) ·
  [Adding a tool](#adding-a-tool) ·
  [Tests and CI](#tests-and-ci) ·
  [Known limits and roadmap](#known-limits-and-roadmap)

And, wherever you are on that scale: **[Feedback](#feedback)** — this has only
ever been tested on one account, and that is a problem — and
**[Contributing](#contributing)**.

---

# Part 1 — For everyone: get it working

You do not need to be a developer. You need about twenty minutes, the ability
to copy commands into a terminal, and the willingness to edit one small text
file. If that sounds fine, read on.

## What you'll need

1. **An Octopus Energy account with a smart meter** sending half-hourly
   readings. Without a smart meter there is very little for the server to read.
2. **An API key.** Sign in at
   [octopus.energy/dashboard](https://octopus.energy/dashboard/new/accounts/personal-details)
   and open **Developer settings**. Copy the API key (it starts with `sk_live_`)
   and your **account number** (looks like `A-1A2B3C4D`, and is on every bill).
3. **Docker Desktop** — [download here](https://www.docker.com/products/docker-desktop/).
   Install it and let it start. This is the easy path; Part 2 has a route
   without Docker.

> **About that API key.** It grants full access to your Octopus account —
> more than this server uses. Keep it in the `.env` file described below,
> don't paste it into chats or issues, and if it ever leaks, generate a new
> one from the same Developer settings page. That instantly kills the old one.

## Set it up

**1. Download this project.** Click **Code → Download ZIP** on the GitHub
page, unzip it somewhere sensible, and open a terminal in the unzipped folder.
(On Windows: right-click inside the folder → *Open in Terminal*. On macOS:
right-click the folder → *Services* → *New Terminal at Folder*.)

If you have git, this does the same thing:

```bash
git clone https://github.com/ngfw-automation/octopus-energy-mcp-server.git
cd octopus-energy-mcp-server
```

**2. Create your settings file.** The project contains a file called
`.env.example`. Make a copy of it named exactly `.env` — no other name works:

```bash
cp .env.example .env          # Windows: copy .env.example .env
```

Open `.env` in any text editor (Notepad is fine) and fill in the two lines
that are not commented out:

```
OCTOPUS_API_KEY=sk_live_your_key_here
OCTOPUS_ACCOUNT_NUMBER=A-1A2B3C4D
```

Save it. Everything else in that file is optional — leave it alone. The
server works out your meters, region, tariff and prices from the API by
itself.

**3. Start it.**

```bash
docker compose up -d
```

The first run downloads two small images and takes a minute or so. Then check
it came up:

```bash
docker compose ps
```

You want the `mcp` container to say **healthy**. If it doesn't, `docker
compose logs` will usually say why in plain English — most often a typo in
the API key.

The server is now listening on `http://localhost:8080/mcp`, **on this machine
only**. Nothing is exposed to your home network or the internet.

Useful later:

```bash
docker compose down                          # stop it
docker compose pull && docker compose up -d  # update to the latest version
```

## Connect your chat app

MCP servers are configured with a small block of JSON. Where that JSON lives
depends on your app — for **Claude Desktop** it is a file called
`claude_desktop_config.json`:

| | |
|---|---|
| Windows | `%APPDATA%\Claude\claude_desktop_config.json` |
| macOS | `~/Library/Application Support/Claude/claude_desktop_config.json` |

Paste in this, keeping any servers already listed there:

```json
{
  "mcpServers": {
    "octopus": {
      "url": "http://localhost:8080/mcp"
    }
  }
}
```

Save, then quit the app completely and reopen it — not just close the window.
The Octopus tools should now appear in its tool list.

Other apps (Claude Code, VS Code, Cursor, LM Studio, Open WebUI and friends)
take the same shape of config somewhere in their own settings; check their
docs for where. Part 2 covers the alternative setup where the app starts the
server itself.

## First questions to try

Start with something simple, so you can tell it is working at all:

- *"What Octopus meters can you see on my account?"*
- *"What tariff am I on, and what am I paying per kWh?"*
- *"How much electricity did I use yesterday?"*

Then the interesting ones:

- *"Break my electricity use down by day for last month and tell me what it cost."*
- *"What's my standing charge, and how much of my monthly bill is it?"*
- *"List the Octopus tariffs available to new customers."*
- *"Compare Octopus Go and Cosy Octopus against what I actually used last month."*
- *"On Agile, which hours tomorrow are cheapest?"*

Ask about a month rather than a year. A year of half-hourly readings is
35,000 numbers; the server summarises rather than dumping them, but a narrower
question gets a better answer.

## When something goes wrong

**No Octopus tools appear in the app.** Quit the app fully and reopen it. Then
check the config file is valid JSON — one missing comma silently breaks the
whole file — and that you saved it in the right place.

**"Connection refused", or the app can't reach the server.** The stack isn't
running: `docker compose ps`. Or you used the wrong port — 8080 for the Docker
setup.

**Errors mentioning 401, unauthorised or authentication.** The API key is
wrong, has a stray space or a missing character, or has been regenerated. Copy
it again from Developer settings, save `.env`, and run `docker compose up -d`
to pick up the change.

**"421 Misdirected Request" or "403 Forbidden".** You reached the server by a
name it doesn't recognise. It only answers to `localhost` and `127.0.0.1` on
purpose — see [Exposure](#exposure).

**It asks you which meter you mean.** Your account has more than one meter for
that fuel. Say the MPAN in your question, or pin one in `.env`. It won't guess,
deliberately: guessing wrong would silently give you someone else's numbers.

**Gas figures look about eleven times too small.** Your meter reports cubic
metres rather than kWh, and nothing in the API says which yours does. Put
`OCTOPUS_GAS_UNITS=m3` in `.env` and restart.

**Yesterday isn't there yet.** Settlement data usually arrives the following
morning, sometimes later, and the odd half-hour goes missing entirely. Every
response tells you how many intervals were missing from the range.

**The total doesn't exactly match my bill.** It should land within a penny or
two over a month. A bigger gap usually means one of: the payment method (direct
debit rates are cheaper — set `DEFAULT_PAYMENT_METHOD=DIRECT_DEBIT`), a period
that doesn't line up with the billing period, or credits, discounts and
adjustments that appear on the bill but not in the meter data.

## What it can and can't do

**It can read** half-hourly electricity, gas and export consumption; your
meters, region and tariff agreements; unit rates and standing charges for any
Octopus tariff; the whole public product catalogue; and cost calculations
built from those.

**It cannot** change your tariff, make payments, spend or redeem OctoPoints,
book anything, or alter your account in any way — no such tool exists in it.
It also can't see 10-second live data from a Home Mini (that needs a different
Octopus API this server doesn't use), and it can't see your bills or
statements, so "how many OctoPoints do I have" is out of reach.

**One thing to understand about safety.** The server itself has no password on
it. That is fine while it only listens on your own machine, which is how it
ships. If you are ever tempted to make it reachable from elsewhere, read
[Exposure](#exposure) first — there is a safe way to do it, and an unsafe one.

---

# Part 2 — For the tinkerer: run it properly

Everything here assumes you are comfortable with a terminal, environment
variables and a JSON config file.

## Tools

| Tool | Purpose |
|---|---|
| `get_current_datetime` | Current date/time in UTC **and** Europe/London (UK), with weekday + UTC offset — for resolving "today"/"this week" |
| `get_electricity_consumption` | Electricity usage, **half-hour (30 min) floor** up to monthly, with stats, bucketed by UK local day |
| `get_gas_consumption` | Gas usage; converted from m³ to kWh when the meter reports m³ (see `OCTOPUS_GAS_UNITS`) |
| `get_export_consumption` | Solar/battery export on the export MPAN, in kWh or as earnings (`unit="GBP"`) |
| `get_unit_rates` | Tariff unit rates in p/kWh — fixed or Agile (half-hourly) |
| `get_standing_charges` | Standing charge history in p/day, ex and inc VAT, per payment method |
| `list_products` | Browse the public Octopus product catalogue — every tariff and its `product_code`, filterable by name, direction, availability and flags (variable, green, tracker, prepay) |
| `get_product` | One product's details plus its tariff codes and headline rates for your region (or one you name) |
| `list_meter_points` | Discover MPANs/MPRNs, meters, region, and active tariffs |
| `get_agreements` | Current + historical tariff agreements (gives `product_code`/`tariff_code`) |
| `calculate_cost` | Invoice-accurate cost over a period as a pence breakdown: kWh rounded per half-hour, priced at the exc-VAT rate, standing charge added, 5% VAT on top. Reconciles to within a penny of a real monthly bill |
| `compare_tariffs` | Prices candidate products against this account's actual consumption and ranks them cheapest-first, with `delta_gbp_vs_baseline` vs your current tariff (region resolved from the active tariff) |

A typical flow: `list_meter_points` / `get_agreements` to find your MPAN and
active tariff, then `get_electricity_consumption` for usage and
`get_unit_rates` + `get_standing_charges` for the tariff price. For an
invoice-accurate total over a period, use `calculate_cost`. To find out what
else is on offer, `list_products` then `get_product`; to check whether
switching would save money, feed those codes to `compare_tariffs`.

Built per `Octopus MCP Spec.md`. Phase 1 is REST-only: no GraphQL and no token
management, because REST is stable, cacheable, and needs no token at all for
price and tariff data.

## Transports and client config

The server speaks two transports.

**Streamable HTTP** (the default) — the server runs as a long-lived process
and clients dial a URL. This is what the Docker stack gives you, and what you
want if more than one client, or a client on another device, will use it.

```bash
octopus-mcp                    # 127.0.0.1:8000 by default; MCP_HOST / MCP_PORT to change
```

```json
{
  "mcpServers": {
    "octopus": { "url": "http://localhost:8080/mcp" }
  }
}
```

Use `:8000` if you ran `octopus-mcp` yourself rather than the Docker stack,
which fronts it with nginx on `:8080`.

**stdio** — the client launches the server as a child process and talks to it
over pipes. No ports, no network surface at all, and the credentials live in
the client's config rather than a `.env`. Simplest and safest for a single
desktop client:

```json
{
  "mcpServers": {
    "octopus": {
      "command": "C:/path/to/project/.venv/Scripts/octopus-mcp.exe",
      "args": ["--stdio"],
      "env": {
        "OCTOPUS_API_KEY": "sk_live_...",
        "OCTOPUS_ACCOUNT_NUMBER": "A-1A2B3C4D"
      }
    }
  }
}
```

macOS/Linux: `/path/to/project/.venv/bin/octopus-mcp`.

Installing without Docker, for either transport:

```bash
python -m venv .venv
.venv\Scripts\activate         # macOS/Linux: source .venv/bin/activate
pip install -e .
```

Python 3.10+ is required; CI tests against 3.10 and 3.13.

## Configuration

**Anything the Octopus API publishes is read from the API.** Tariffs, unit
rates, standing charges, meters, regions and agreements are all discovered at
run time — none of them can be pinned in the environment, because a pinned
copy goes stale silently and then lies to you with total confidence. What
remains below is credentials, deployment knobs, and the two or three facts the
REST API genuinely does not expose. `.env.example` documents every one of
them with the same reasoning.

| Setting | Required | What it's for |
|---|---|---|
| `OCTOPUS_API_KEY` | yes | Your key from Developer settings |
| `OCTOPUS_ACCOUNT_NUMBER` | yes | e.g. `A-1A2B3C4D` |
| `DEFAULT_PAYMENT_METHOD` | no | `DIRECT_DEBIT` or `NON_DIRECT_DEBIT`. Variable tariffs price differently per payment method and the account payload does not say which you are on; unset, the server uses the dearer rate and says so in the response |
| `OCTOPUS_GAS_UNITS` | no | `m3` if your gas meter reports cubic metres. Not detectable from the API |
| `GAS_CALORIFIC_VALUE` | no | MJ/m³ for the m³→kWh conversion, printed on your gas bill. Default 39.5 |
| `OCTOPUS_ELECTRICITY_MPAN` / `_SERIAL`, `OCTOPUS_GAS_MPRN` / `_SERIAL`, `OCTOPUS_EXPORT_MPAN` | no | Pick a default meter when a fuel has more than one. An MPAN or serial that isn't on the account is an error, never a silent fall back |
| `MCP_HOST` / `MCP_PORT` | no | HTTP bind address and port (`127.0.0.1:8000`; the container sets `0.0.0.0`) |
| `MCP_ALLOWED_HOSTS` / `MCP_ALLOWED_ORIGINS` | no | Extra names the server answers to — see [Exposure](#exposure) |
| `CACHE_TTL_PRODUCTS` / `_RATES` / `_CONSUMPTION` / `_ACCOUNT` | no | Cache lifetimes in seconds (86400 / 86400 / 1800 / 3600) |
| `MAX_ROWS_RETURNED` | no | Cap on aggregated buckets or rate rows in one response (500) |
| `MAX_RAW_DAYS` | no | Widest range `include_raw` will answer for (2 days ≈ 96 rows) |
| `LOG_LEVEL` | no | `DEBUG`, `INFO` (default), `WARNING`, `ERROR` |

## Docker deployment

Both containers run on **Chainguard** base images, nonroot, and both track
`:latest` with `pull_policy: always` — the MCP image so `docker compose up`
gets the current CI build, the nginx image so Chainguard's continuous CVE
rebuilds actually reach you rather than being frozen by a digest pin.

| Container | Image | Role |
|---|---|---|
| `mcp` | `ghcr.io/ngfw-automation/octopus-energy-mcp-server:latest` | the MCP server, streamable-HTTP on `:8000` |
| `nginx` | `cgr.dev/chainguard/nginx:latest` | front on `:8080`, proxies `/mcp` → `mcp:8000`; SSE buffering off, CORS for MCP clients |

NGINX listens on plain HTTP, which is right for a loopback-only deployment.
`deploy/nginx/conf.d/mcp.conf` carries commented instructions for switching it
to `:443` with real certificates — though read [Exposure](#exposure) first,
because TLS solves eavesdropping and not the fact that the endpoint has no
authentication.

```bash
docker compose up -d            # pulls the latest images, then starts
docker compose ps               # mcp container reports (healthy)
docker compose logs -f          # follow logs
```

End-to-end check against a running stack (needs real credentials in `.env`):

```bash
.venv\Scripts\python scripts\e2e_check.py   # default: http://127.0.0.1:8080/mcp
```

GitHub Actions builds the `mcp` image from `Dockerfile` on every push to
`main` and publishes it to GHCR — `:latest` plus SHA tags.

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

## Behaviour and caveats

- **Finest granularity is half-hourly (30 min).** Anything finer (10-second
  live telemetry) requires a Home Mini and the GraphQL API — not in Phase 1.
- **Settlement data lags.** Half-hourly consumption usually lands the morning
  after, and gaps happen; `stats.missing_intervals` tells you how many
  half-hours are absent from the range.
- **Dates and timezone.** Pass a plain date (`2026-08-01`) and it is read as
  UK local — `period_to` covers that whole day — or a UTC timestamp
  (`2026-08-01T00:00:00Z`) for exact instants. **Buckets are UK local**
  (Europe/London), so a "day" is the day your bill charges for, and the
  clocks-change days are correctly 23 and 25 hours long. Series timestamps
  carry their offset. Bad or reversed ranges are rejected before any request
  is made.
- **Gas units are not detectable.** SMETS1 meters report kWh, SMETS2 meters
  report m³, and the account payload says which for neither. Set
  `OCTOPUS_GAS_UNITS=m3` (or pass `source_unit`) if yours reports m³;
  otherwise kWh is assumed and every gas response says so. The conversion is
  `m³ × CV × 1.02264 / 3.6` with `GAS_CALORIFIC_VALUE` (39.5 → ~11.22 kWh/m³);
  your bill gives the exact calorific value for your area and period.
- **Standing charge.** Always the charge the tariff publishes, matched to your
  payment method and weighted across any rate change inside the period. It is
  not configurable. A tariff publishing none contributes zero and the response
  says so, rather than the server substituting a figure of its own. Where a
  published charge looks like an unmaintained placeholder — some products
  still carry a row dated years ago — the response flags its age.
- **Payment method.** Where a tariff publishes both direct-debit and
  non-direct-debit rates and none is pinned, the dearer is used and the
  response says so. Pin `DEFAULT_PAYMENT_METHOD` to get your own.
- **Multiple meters.** If a fuel has more than one meter point and you haven't
  pinned or supplied one, the tool returns a disambiguation list rather than
  guessing.
- **Export meters** are identified by the account's `is_export` flag, kept out
  of the import tools, and read by `get_export_consumption`.
- **Partial tariff coverage.** `compare_tariffs` refuses to rank a product
  whose published rates cover less than 99% of the period — a product launched
  mid-period would otherwise win every comparison, because the unpriced
  remainder counts as free. It comes back as `incomplete_rate_coverage` with
  the share it could price.
- **Raw rows.** `include_raw` is capped at `MAX_RAW_DAYS` (default 2) — a month
  of half-hours is ~1,500 rows. Use a coarser `group_by` for wider ranges.
- **Logs.** One JSON line per MCP request and upstream call, on stderr
  (`LOG_LEVEL`, default INFO). The API key is redacted by value and
  account/meter identifiers are masked out of URLs.

---

# Part 3 — For the ninja: how it works inside

## Architecture

Five modules, ~3,300 lines, no framework beyond the MCP SDK and httpx.

| Module | Lines | Responsibility |
|---|---|---|
| `server.py` | ~2,150 | The 12 `@mcp.tool()` definitions, meter resolution, response assembly, transport bootstrap |
| `shaping.py` | ~575 | Pure functions: bucketing, billing arithmetic, unit conversion, note generation. No I/O, no globals — everything it needs arrives as an argument |
| `rest.py` | ~300 | GET-only async Octopus client: TTL+LRU cache, single-flight, bounded retries, pagination |
| `observability.py` | ~175 | JSON-lines logging, secret redaction, identifier masking, the MCP middleware |
| `config.py` | ~110 | `pydantic-settings` model, validators, `.env` loading |

The dependency direction is strict: `server` → `shaping` + `rest` + `config`,
`rest` → `observability`. `shaping` imports nothing from the project, which is
why the billing logic is testable without a network, a fake or a fixture file.

**Request path.** A tool call arrives → `LoggingMiddleware` starts a timer →
the tool resolves its `ServerContext` (settings + REST client) from a
`ContextVar`, falling back to a process-wide one → it resolves a meter point
(pinned, sole, or a disambiguation error) → it fetches through `rest()`, which
may answer from cache → the rows go to `shaping` for bucketing and pricing →
the tool assembles a result dict, truncates to `MAX_ROWS_RETURNED`, and
attaches accumulated notes → the middleware logs `mcp.request` with the
outcome.

**The `ServerContext` seam.** Settings and the HTTP client are reached through
a `ContextVar` rather than module globals. One process still serves one
account today, but the tools no longer *assume* that, so a per-request
credential (a multi-tenant deployment, or OAuth) is a change to how the
context is set rather than a rewrite of every tool.

**Error convention.** Tools never raise across the MCP boundary. Every failure
is a dict with an `error` key and a `message` written for the model to relay:
`bad_input`, `no_data`, `no_rates`, `no_priced_intervals`,
`incomplete_rate_coverage`, `multiple_meter_points`, `unknown_meter`,
`unknown_serial`, `no_tariff`, `no_tariff_for_region`, `region_required`,
`region_not_offered`, `range_too_wide_for_raw`, and the upstream ones mapped
from `OctopusAuthError` / `OctopusNotFoundError` / `OctopusAPIError`.

## The billing arithmetic

This is the part worth reading, because getting it wrong is silent. Octopus
bills like this, and `shaping.bill_cost` reproduces it exactly:

1. Each half-hourly kWh figure is rounded to **0.01 kWh, banker's rounding**
   (`ROUND_HALF_EVEN`) — per interval, before anything is priced.
2. Each rounded interval is multiplied by the **ex-VAT** unit rate valid at
   that instant, and kept at 0.0001p precision. Rounding each interval to the
   penny instead would cost ~1% on a real month.
3. The interval costs are summed and the total is rounded to the penny **once**.
4. The standing charge for the period is added, still ex-VAT, weighted by each
   published charge's overlap with the period.
5. **5% VAT is applied once, to the subtotal.** Not to the unit cost and the
   standing charge separately, and never to a figure that already includes it.

Rate matching walks validity windows, so a tariff whose rates change mid-period
prices correctly on both sides of the change, and Agile's 48 daily slots are
just the general case. Intervals with no matching rate are excluded from the
total and reported as `unpriced_kwh` rather than counted as free.

Everything is `Decimal`. Not one float multiplication touches money.

The output carries `rate_bands` (kWh per distinct rate, plus
`share_in_cheapest_band` for time-of-use tariffs),
`effective_p_kwh_exc_vat`, `sc_source`, `unpriced_kwh` and a `notes` list
explaining every assumption the calculation made.

**Timezone handling** matters as much as the rounding. `bucket_start` and
`bucket_end` floor and advance in **Europe/London wall time**, not UTC, so a
"day" is the day the bill charges for; the 23-hour and 25-hour clocks-change
days come out at the right length instead of quietly dropping or doubling an
hour.

## The HTTP layer: cache, single-flight, retries

`OctopusREST` is GET-only by construction — there is no method on it that can
issue anything else.

**Cache.** `TTLCache` is a monotonic-clock TTL cache with an LRU bound (512
entries, swept on write). `get` returns a **deep copy**, because callers walk
the rows they are handed — the gas m³→kWh conversion used to rewrite them in
place, which compounded the edit on every later cache hit.

**Cache key** is `(credential fingerprint | "public", full URL, sorted params)`.
The fingerprint is `sha256(api_key)[:12]`, so two accounts in one process
cannot read each other's cached data, and the key never holds the key itself.
Public catalogue calls are keyed as `"public"` and shared.

**Single-flight.** Concurrent callers asking for the same key wait on one
`asyncio.Lock` and one upstream request, then all read the filled cache. A
model that fires six tool calls in parallel over the same month costs one
fetch, not six.

**Retries** are bounded by attempt count (5) *and* a shared 90-second deadline:
each backoff checks whether sleeping would blow the deadline, and gives up
rather than exceeding it. `Retry-After` is honoured when present, with jitter
otherwise. Retryable statuses are 429, 500, 502, 503, 504; 401 and 404 map to
typed exceptions immediately.

**TTLs** are tuned to how often the data actually changes: products and rates
86400s, account 3600s, consumption 1800s. Agile publishes tomorrow's rates
around 16:00 UK, and `seconds_until_next_publish` exists to expire the cache
against that boundary rather than a fixed clock.

## Transport security internals

`_transport_security()` builds the SDK's `TransportSecuritySettings` with DNS
rebinding protection on:

```python
hosts   = ["localhost", "127.0.0.1", "localhost:*", "127.0.0.1:*", *env("MCP_ALLOWED_HOSTS")]
origins = ["http://localhost:*", "http://127.0.0.1:*",
           "https://localhost:*", "https://127.0.0.1:*", *env("MCP_ALLOWED_ORIGINS")]
```

Both header checks matter and they catch different attacks. A forged `Host`
gets 421; a foreign `Origin` gets 403. Verified end-to-end: 200 for a normal
call, 421 for a forged Host, 403 for a foreign Origin, 200 again for a name
added to `MCP_ALLOWED_HOSTS`.

This closes two of the three ways to reach the endpoint. The third — something
already on your machine or tailnet — is not closed, because the endpoint has
no caller authentication at all. See [Known limits](#known-limits-and-roadmap).

## Observability

One JSON object per line on stderr, so container logs and `jq` both work.

- `mcp.request` — `method`, `tool`, `ms`, `outcome`. Emitted by
  `LoggingMiddleware` on the SDK middleware chain, so it covers tool calls,
  listings and the handshake without any tool remembering to. It unwraps
  `{"error": ...}` out of `structuredContent` so a tool that *returned* an
  error is logged as an error, not a success.
- `upstream.request` — `url`, `status`, `ms`, plus retry context.
- `server.start` — version and transport.

**Redaction is by value, not by field name.** `RedactingFilter` scrubs the API
key wherever it appears in a record, whatever built the message, and drops
`authorization` / `api_key` / `token` fields outright. `mask_identifiers()`
rewrites `/accounts/<account>` and `/meters/<serial>` in URLs and blanks any
9–15 digit run, so MPANs and MPRNs don't reach the log either. `httpx`'s own
logger is silenced and the SDK's `mcp` logger is routed through the same
handler with `propagate = False`, so nothing sneaks out unformatted.

## Adding a tool

1. Write the pure part in `shaping.py` first, with a test. If it needs I/O,
   it belongs in the tool, not in `shaping`.
2. Add the `@mcp.tool()` in `server.py`. Annotate every parameter with
   `Annotated[T, Field(description=...)]` — those descriptions are the entire
   interface the model sees, so write them for a reader who has never seen an
   MPAN.
3. Return a plain dict. On failure return an `error`/`message` dict; do not
   raise.
4. Docstring first line is what the model reads when choosing between tools.
   Say what it answers, not how it works.
5. Add the tool name to `EXPECTED_TOOLS` in `tests/test_server.py` and cover
   it with a fake REST client — no test touches the network.
6. Add a row to the tools table in Part 2.

## Tests and CI

```bash
pip install -e ".[dev]"
pytest -q          # 89 tests, no network, ~3s
ruff check .
```

Tests use hand-built fake REST clients rather than recorded fixtures, and the
fixture identifiers are deliberately fake (`1234567890123`, `Z1A0000001`,
`A-AAAA1111`). Real MPANs and meter serials must never enter this repository —
they are the identifiers of someone's home.

CI (`.github/workflows/build-and-push.yml`) runs ruff and pytest on Python
**3.10 and 3.13** as a matrix, and that job gates the build. The image build
then attaches an **SBOM** and **SLSA provenance** (`provenance: mode=max`) via
buildx, publishes `:latest` plus SHA tags to GHCR, and — only on a public
repository, only outside pull requests — adds a GitHub provenance attestation
and a **keyless cosign signature**. Both of those are gated on
`github.event.repository.visibility` because GitHub refuses attestations on
user-owned private repositories, and keyless signing publishes repository
identity to a public transparency log.

Dependabot watches pip, GitHub Actions and Docker, batching minor and patch
updates into one PR per ecosystem and leaving majors alone. The Chainguard
Python base in the `Dockerfile` *is* digest-pinned; Dependabot's docker
ecosystem is what keeps that digest moving.

## Known limits and roadmap

Honest list of what is not done.

- **No caller authentication.** Anything that can reach the endpoint reads the
  whole account. Loopback binding and Host/Origin validation close the network
  paths; they do not authenticate a caller. OAuth 2.1 resource-server support
  exists in the SDK (`TokenVerifier` / `AuthSettings`) and is the intended fix.
- **REST only.** No GraphQL, so no Home Mini live telemetry, no bills or
  statements, no OctoPoints, and no way to read the account's actual payment
  method — which is why `DEFAULT_PAYMENT_METHOD` still exists as a setting.
- **`missing_intervals` is `expected − received`.** It counts the shortfall
  rather than identifying which half-hours are absent, so a range that both
  gains and loses intervals can under-report.
- **Unit rates are rounded to 2 dp on output** while the arithmetic keeps full
  precision internally. Fine for display, wrong if you re-derive a total from
  the displayed rates.
- **No MCP tool `annotations`** (`readOnlyHint`, `idempotentHint`), no
  resources, no prompts. Every tool here is read-only and idempotent, and
  saying so in the protocol would let clients act on it.
- **Single account, single process.** The `ServerContext` seam is in place for
  multi-tenancy; nothing above it uses that yet.
- **Tested against one account.** See below.

---

# Feedback

**This has only ever been tested on one account, and that account is not very
interesting.** Specifically, everything you read above was verified against:

- a single property, single electricity meter, **no gas**;
- a single-register import meter, **no export/solar**;
- one supply region (**A**, Eastern England);
- a standard variable tariff on **direct debit**;
- Windows + Docker Desktop, with Claude as the client.

Everything else — the gas path, dual fuel, multiple meters, multiple
properties, multiple accounts, export meters, Economy 7 dual-register meters,
prepayment, non-direct-debit pricing, the other thirteen regions — is written
from the API documentation and reasoning, and has **never been run against
real data**. Some of it is certainly wrong.

**So: if your account is more interesting than mine, please try it and tell me
what broke.** Particularly valuable:

- **Gas meters.** Does `get_gas_consumption` return sane numbers? Is the
  m³/kWh detection guidance right for your meter, and does the calorific-value
  conversion land near your bill?
- **Dual fuel.** Do the two fuels stay separate, and does `calculate_cost`
  reconcile for each?
- **Multiple meters or properties on one account.** Does the disambiguation
  list actually let you pick, and do the pinning variables do what they say?
- **Multiple accounts.** Currently one account per server process — is that
  workable for you, or does it need to be a per-call parameter?
- **Export / solar.** `get_export_consumption` and its `unit="GBP"` earnings
  path are entirely untested against a real export MPAN.
- **Economy 7 and other dual-register meters.** Day/night register handling is
  the thinnest part of the code.
- **Other regions.** Region is derived from your tariff code's suffix; if
  yours resolves wrongly, that is a bug worth knowing about.
- **Other MCP clients.** If it works — or doesn't — in LM Studio, Open WebUI,
  Cursor, VS Code, Continue, or anything else, that is genuinely useful to
  document.
- **A bill that doesn't reconcile.** If `calculate_cost` is more than a couple
  of pence off your real monthly bill, that is the most valuable bug report
  there is.

**How to report.** Open an issue with what you asked, what came back, and what
you expected. Please **redact your MPAN, MPRN, meter serial, account number,
address and API key** — the server masks them in its own logs, but tool output
pasted into an issue is not masked. `A-XXXX1111` and `1234567890123` are
perfectly good stand-ins; what matters is the shape of the data, not the
identifiers.

No telemetry of any kind is collected. Nobody sees anything you don't send.

# Contributing

Pull requests are welcome, including documentation-only ones — if a step in
Part 1 didn't work on your machine, fixing that sentence is a real
contribution.

**Getting set up.**

```bash
git clone https://github.com/ngfw-automation/octopus-energy-mcp-server.git
cd octopus-energy-mcp-server
python -m venv .venv && .venv\Scripts\activate    # or source .venv/bin/activate
pip install -e ".[dev]"
pytest -q && ruff check .
```

**House rules.**

- **Both checks pass before you push.** CI runs ruff and pytest on 3.10 and
  3.13, and the build won't start until they're green.
- **No network in tests.** Use a fake REST client, as the existing tests do.
- **No real identifiers, ever** — not in tests, fixtures, docs, commit
  messages or issue text. No MPANs, MPRNs, meter serials, account numbers,
  addresses or API keys, yours or anyone's.
- **Read-only stays read-only.** This server issues `GET` and nothing else. A
  tool that writes to an Octopus account will not be merged here, whatever it
  does. That constraint is the reason it is safe to hand a full-access API key
  to a language model.
- **Pure logic goes in `shaping.py`, with a test.** If it can be a function of
  its arguments, it should be.
- **Nothing the API publishes becomes a setting.** If you find yourself adding
  an environment variable for a value Octopus already returns, fetch it
  instead. Pinned copies go stale silently, and a wrong number stated
  confidently is worse than no number.
- **Money is `Decimal`.** Never float.
- **Explain the "why" in the commit message.** The interesting part of a
  billing fix is what was wrong before and how you knew.

**Especially wanted:** anything from the [Feedback](#feedback) list above,
verified against a real account — a gas-meter fix from someone with a gas
meter beats a careful guess from someone without one.

# Licence

MIT — see [LICENSE](LICENSE).
