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

---

## Part 1 — Getting it running

You do not need to be a developer. You need about twenty minutes, and you need
to be comfortable copying commands into a terminal and editing one small text
file. If that sounds fine, read on.

### What you'll need

1. **An Octopus Energy account with a smart meter** sending half-hourly
   readings. Without a smart meter there is very little for the server to read.
2. **An API key.** Sign in at
   [octopus.energy/dashboard](https://octopus.energy/dashboard/new/accounts/personal-details)
   and open **Developer settings**. Copy the API key (it starts with `sk_live_`)
   and your **account number** (looks like `A-1A2B3C4D`, and is on every bill).
3. **Docker Desktop** — [download here](https://www.docker.com/products/docker-desktop/).
   This is the easy path. If you would rather not install Docker, there is a
   Python route further down.

> **About that API key.** It grants full access to your Octopus account —
> more than this server uses. Keep it in the `.env` file described below,
> don't paste it into chats or issues, and if it ever leaks, generate a new
> one from the same Developer settings page (that instantly kills the old one).

### Setup with Docker

**1. Download this project.** Either use git:

```bash
git clone https://github.com/ngfw-automation/octopus-energy-mcp-server.git
cd octopus-energy-mcp-server
```

…or click **Code → Download ZIP** on the GitHub page and unzip it, then open a
terminal in the unzipped folder.

**2. Create your settings file.** Copy `.env.example` to a new file called
`.env` in the same folder:

```bash
cp .env.example .env          # Windows: copy .env.example .env
```

Open `.env` in any text editor and fill in the two required lines:

```
OCTOPUS_API_KEY=sk_live_your_key_here
OCTOPUS_ACCOUNT_NUMBER=A-1A2B3C4D
```

Everything else in that file is optional — leave it commented out. The server
discovers your meters, region, tariff and prices from the API on its own.

**3. Start it.**

```bash
docker compose up -d
```

The first run downloads two small images and takes a minute. Check it came up:

```bash
docker compose ps       # the mcp container should say "healthy"
docker compose logs -f  # live logs; Ctrl-C to stop watching
```

The server is now listening on `http://localhost:8080/mcp`, **on this machine
only** — nothing is exposed to your network or the internet.

To stop it: `docker compose down`. To update it later: `docker compose pull &&
docker compose up -d`.

### Setup without Docker

You need [Python 3.10 or newer](https://www.python.org/downloads/). In the
project folder:

```bash
python -m venv .venv
.venv\Scripts\activate         # macOS/Linux: source .venv/bin/activate
pip install -e .
```

Create the same `.env` file as above, then run:

```bash
octopus-mcp                    # HTTP on 127.0.0.1:8000
octopus-mcp --stdio            # or let your chat app launch it (see below)
```

### Connecting your chat app

MCP servers are configured with a small block of JSON. Where that JSON lives
depends on your app — for **Claude Desktop** it is a file called
`claude_desktop_config.json`:

| | |
|---|---|
| Windows | `%APPDATA%\Claude\claude_desktop_config.json` |
| macOS | `~/Library/Application Support/Claude/claude_desktop_config.json` |

Other apps (Claude Code, VS Code, Cursor, LM Studio, Open WebUI and friends)
take the same shape of config in their own settings — check their docs for
where, then use one of the blocks below.

**If you started the Docker stack** (or ran `octopus-mcp` yourself), point the
app at the running server:

```json
{
  "mcpServers": {
    "octopus": {
      "url": "http://localhost:8080/mcp"
    }
  }
}
```

Use `http://localhost:8000/mcp` instead if you ran `octopus-mcp` directly
without Docker.

**If you'd rather your chat app started the server itself** (no Docker, no
ports — this is the simplest setup if you took the Python route):

```json
{
  "mcpServers": {
    "octopus": {
      "command": "C:/path/to/project/.venv/Scripts/octopus-mcp.exe",
      "args": ["--stdio"],
      "env": {
        "OCTOPUS_API_KEY": "sk_live_your_key_here",
        "OCTOPUS_ACCOUNT_NUMBER": "A-1A2B3C4D"
      }
    }
  }
}
```

On macOS or Linux the command is `/path/to/project/.venv/bin/octopus-mcp`.

Restart the app after editing the config. You should see the Octopus tools
listed among its available tools.

### First questions to try

Start simple, so you can tell it is working at all:

- *"What Octopus meters can you see on my account?"*
- *"What tariff am I on, and what am I paying per kWh?"*
- *"How much electricity did I use yesterday?"*

Then the interesting ones:

- *"Break my electricity use down by day for last month and tell me what it cost."*
- *"What's my standing charge, and how much of my bill is it?"*
- *"List the Octopus tariffs available to new customers."*
- *"Compare Octopus Go and Cosy Octopus against what I actually used last month."*
- *"On Agile, which hours tomorrow are cheapest?"*

Ask for a whole month rather than a whole year — a year of half-hourly data is
a lot to reason about, and the server will summarise rather than dump it.

### When something goes wrong

**No Octopus tools appear in the app.** Restart the app fully. Then check the
JSON is valid (a missing comma silently breaks the whole file), and that the
path in `command` really exists.

**"Connection refused" or the app can't reach the server.** The stack isn't
running — `docker compose ps` — or you used the wrong port: 8080 for Docker,
8000 for `octopus-mcp` run directly.

**Errors mentioning 401 or authentication.** The API key is wrong, has a stray
space, or was regenerated. Copy it again from Developer settings, and restart
the stack (`docker compose up -d` picks up an edited `.env`).

**"421 Misdirected Request" or "403".** You reached the server by a name it
doesn't recognise. It only answers to `localhost` and `127.0.0.1` unless you
add the name to `MCP_ALLOWED_HOSTS` — see [Exposure](#exposure).

**It asks which meter you mean.** Your account has more than one meter for
that fuel. Tell it the MPAN in the question, or pin one in `.env` with
`OCTOPUS_ELECTRICITY_MPAN`. The server won't guess.

**Gas figures look about eleven times too small.** Your meter reports cubic
metres, not kWh, and nothing in the API says so. Set `OCTOPUS_GAS_UNITS=m3`
in `.env` and restart.

**Yesterday's data isn't there yet.** Settlement data usually lands the
following morning, sometimes later, and occasional half-hours go missing
altogether. Every response tells you how many intervals were missing.

**The total doesn't exactly match my bill.** It should be within a penny or
two over a month. Bigger gaps usually mean a payment method mismatch (direct
debit rates are cheaper — set `DEFAULT_PAYMENT_METHOD=DIRECT_DEBIT`), a period
that doesn't line up with the billing period, or credits and discounts that
appear on the bill but not in the meter data.

### What it can and can't do

It can read: half-hourly electricity, gas and export consumption; your meters,
region and tariff agreements; unit rates and standing charges for any Octopus
tariff; the full public product catalogue; and cost calculations built from
all of that.

It cannot: change your tariff, make payments, redeem or spend OctoPoints, book
anything, or alter your account in any way. No tool exists for it. It also
can't see 10-second live data from a Home Mini — that needs a different
Octopus API this server doesn't use — and it can't see your bills or
statements, so a figure like "how many OctoPoints do I have" is out of reach.

---

## Part 2 — Reference

### Tools

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

### Configuration

**Anything the Octopus API publishes is read from the API.** Tariffs, unit
rates, standing charges, meters, regions and agreements are all discovered at
run time — none of them can be pinned in the environment, because a pinned
copy goes stale silently and lies to you. The settings below are credentials,
deployment knobs, and the two or three facts the REST API genuinely does not
expose. `.env.example` documents them all.

| Setting | Required | What it's for |
|---|---|---|
| `OCTOPUS_API_KEY` | yes | Your key from Developer settings |
| `OCTOPUS_ACCOUNT_NUMBER` | yes | e.g. `A-1A2B3C4D` |
| `DEFAULT_PAYMENT_METHOD` | no | `DIRECT_DEBIT` or `NON_DIRECT_DEBIT`. Variable tariffs price differently per payment method and the account payload does not say which you are on; unset, the server uses the dearer rate and says so |
| `OCTOPUS_GAS_UNITS` | no | `m3` if your gas meter reports cubic metres. Not detectable from the API |
| `GAS_CALORIFIC_VALUE` | no | MJ/m³ for the m³→kWh conversion, from your gas bill. Default 39.5 |
| `OCTOPUS_ELECTRICITY_MPAN` / `_SERIAL`, `OCTOPUS_GAS_MPRN` / `_SERIAL`, `OCTOPUS_EXPORT_MPAN` | no | Pick a default meter when a fuel has more than one |
| `MCP_HOST` / `MCP_PORT` | no | HTTP bind address and port |
| `MCP_ALLOWED_HOSTS` / `MCP_ALLOWED_ORIGINS` | no | Extra names the server answers to — see [Exposure](#exposure) |
| `CACHE_TTL_*`, `MAX_ROWS_RETURNED`, `MAX_RAW_DAYS`, `LOG_LEVEL` | no | Cache lifetimes, response caps, log verbosity |

### Docker deployment

Both containers run on **Chainguard** base images, nonroot, and both track
`:latest` with `pull_policy: always` — the MCP image so `docker compose up`
gets the current CI build, the nginx image so Chainguard's continuous CVE
rebuilds actually reach you.

| Container | Image | Role |
|---|---|---|
| `mcp` | `ghcr.io/ngfw-automation/octopus-energy-mcp-server:latest` | the MCP server, streamable-HTTP on `:8000` |
| `nginx` | `cgr.dev/chainguard/nginx:latest` | front on `:8080`, proxies `/mcp` → `mcp:8000`; SSE buffering off, CORS for MCP clients |

NGINX listens on plain HTTP, which is right for a loopback-only deployment.
`deploy/nginx/conf.d/mcp.conf` carries commented instructions for switching it
to `:443` with real certificates if you ever front it differently — though see
[Exposure](#exposure) first, because the endpoint has no authentication.

```bash
docker compose up -d            # pulls the latest images, then starts
docker compose ps               # mcp container reports (healthy)
docker compose logs -f          # follow logs
```

E2E check against a running stack (needs real credentials in `.env`):

```bash
.venv\Scripts\python scripts\e2e_check.py   # default: http://127.0.0.1:8080/mcp
```

GitHub Actions builds the `mcp` image from `Dockerfile` on every push to
`main` and publishes it to GHCR — `:latest` plus SHA tags.

### Exposure

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

#### Remote access, without publishing anything

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

### Notes & caveats

- **Finest granularity is half-hourly (30 min).** Anything finer (10-second
  live telemetry) requires a Home Mini and the GraphQL API — not in Phase 1.
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
- **Standing charge:** always the charge the tariff publishes, matched to your
  payment method and weighted across rate changes in the period. It is not
  configurable. A tariff that publishes none contributes zero and the response
  says so, rather than the server substituting a figure of its own. Where a
  published charge looks like an unmaintained placeholder — some products still
  carry a row dated years ago — the response flags its age.
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

### Development

```bash
pip install -e ".[dev]"
pytest -q
ruff check .
```

### Licence

MIT — see [LICENSE](LICENSE).
