# Octopus Energy API Capabilities & MCP Server Technical Specification

## TL;DR
- Octopus Energy exposes two consumer APIs: a stable, largely-public **REST API** at `https://api.octopus.energy/v1/` (products, tariff rates, meter consumption, GSP lookup — HTTP Basic auth with an account API key) and a far richer but partly reverse-engineered **Kraken GraphQL API** at `https://api.octopus.energy/v1/graphql/` (account, billing, ledgers, live telemetry, saving sessions, Octoplus points, intelligent/EV dispatches, heat-pump data — JWT auth via `obtainKrakenToken`).
- A robust MCP server should route static/price and half-hourly consumption tools to REST (simpler, cacheable, no token) and route account/billing/telemetry/dispatch/loyalty tools to GraphQL, sharing one credential (the API key), caching the JWT for its 60-minute lifetime, and enforcing **read-only** behaviour (no tariff-change or payment mutations).
- The spec below documents every endpoint/query, flags officially-documented vs community-reverse-engineered surfaces, and defines ~23 MCP tools, resources, prompts, data models, LLM-response-shaping rules (downsampling half-hourly data), error/backoff handling, and a phased roadmap.

---

## Key Findings

1. **Two APIs, one credential.** A single account API key (Basic-auth username for REST; `obtainKrakenToken(input:{APIKey:...})` for GraphQL) unlocks both surfaces. The key grants **full account access**, so read-only enforcement is a design requirement, not an option.
2. **REST is stable and largely public.** Product catalogue, tariff unit rates/standing charges, and even Agile/Go/Tracker price history require **no authentication**. Only `/accounts/` and `/consumption/` need the key.
3. **GraphQL is where the modern data lives.** Billing statements, transactions, ledgers/balance, live smart-meter telemetry (Octopus Home Mini), Saving Sessions/Octoplus, Intelligent Octopus EV dispatches, and Cosy heat-pump telemetry are only available via GraphQL, much of it undocumented and reverse-engineered by the community.
4. **Half-hourly data is delayed and gappy.** Settlement consumption typically appears the morning after (often mid-day or later), with frequent missing periods; live telemetry via Home Mini is ~10–30s but requires the hardware.
5. **Timezone handling is the #1 gotcha.** Always send `period_from`/`period_to` in UTC with a trailing `Z`; consumption responses can flip to local (BST) offsets, price responses stay UTC, and Octopus publishes bulk price data in CET.

---

## Part 1 — The Octopus Energy API Surface

### 1.1 Authentication & identifiers

**API key.** Customers generate a key from the online dashboard developer page (`https://octopus.energy/dashboard/developer/`, or the newer `.../accounts/personal-details/api-access` path). It is used as the HTTP Basic auth **username with a blank password** for REST: `curl -u "$API_KEY:" https://api.octopus.energy/v1/accounts/`. The trailing colon prevents cURL prompting for a password. There is no developer registration or approval process — any customer can use it.

**Account number** format is `A-AAAA1111` (letter `A`, hyphen, then an alphanumeric block) — visible in the dashboard URL and on bills.

**Identifier hierarchy** (from the `/accounts/` response):
- **Account** → one or more **properties** (each with `id`, address, `moved_in_at`/`moved_out_at`).
- Each property has **electricity_meter_points** (keyed by **MPAN**, 13 digits) and **gas_meter_points** (keyed by **MPRN**).
- Each meter point has one or more **meters** (each with a `serial_number` and `registers`), and a list of **agreements** (`tariff_code`, `valid_from`, `valid_to`).
- Solar/battery export uses a **second electricity MPAN** with `direction: EXPORT`.
- **Region/GSP:** each MPAN maps to a region letter A–P (14 regions, skipping I and O), derivable from the first two MPAN digits or via the GSP lookup endpoint.

**Tariff code anatomy:** e.g. `E-1R-AGILE-18-02-21-C` = `E` (electricity) / `1R` (single register; `2R`=dual/Economy 7; `G-1R`=gas) / product code `AGILE-18-02-21` / region `C`.

**GraphQL auth flow.** `POST` to `https://api.octopus.energy/v1/graphql/` with the `obtainKrakenToken` mutation. Three input styles:
- API key: `mutation { obtainKrakenToken(input: {APIKey: "<KEY>"}) { token } }`
- Email/password: `mutation Login($input: ObtainJSONWebTokenInput!) { obtainKrakenToken(input: $input) { token refreshToken refreshExpiresIn } }` with `{ "input": { "email": "...", "password": "..." } }`
- Pre-signed key.

Per Octopus's official GraphQL docs, **"The token is only valid for 60 minutes from time of issuance. However, the refreshToken is valid for 7 days (you can use the `refreshExpiresIn` value to know when the refreshToken will expire)."** Recommended usage: cache both, use the token until expiry, regenerate via the refresh token. The token is passed in the `Authorization` header (value is the raw token; community mobile-app captures use `Authorization: JWT <token>`).

### 1.2 REST API endpoints

Base URL: `https://api.octopus.energy/v1/`. All responses are JSON with a `{count, next, previous, results}` pagination envelope where applicable.

| Endpoint | Auth | Purpose | Key params |
|---|---|---|---|
| `GET /products/` | none | List energy products | `brand`, `is_variable`, `is_green`, `is_tracker`, `is_prepay`, `is_business`, `available_at`, `page` |
| `GET /products/{product_code}/` | none | Product detail incl. tariffs by region + links to rate history | `tariffs_active_at` |
| `GET /products/{code}/electricity-tariffs/{tariff_code}/standard-unit-rates/` | none | Unit rate history / Agile half-hourly rates | `period_from`, `period_to`, `page_size` (up to 1500) |
| `.../day-unit-rates/` and `.../night-unit-rates/` | none | Economy-7 (dual register) day/night rates | as above |
| `.../standing-charges/` | none | Standing charge history | `period_from`, `period_to` |
| `GET /products/{code}/gas-tariffs/{tariff_code}/standard-unit-rates/` | none | Gas unit rates | as above |
| `GET /accounts/{account}/` | **key** | Properties, MPANs/MPRNs, meters, agreements | — |
| `GET /electricity-meter-points/{mpan}/meters/{serial}/consumption/` | **key** | Half-hourly electricity consumption (kWh) | `period_from`, `period_to`, `page_size` (max 25000), `order_by=period`, `group_by=hour/day/week/month/quarter` |
| `GET /gas-meter-points/{mprn}/meters/{serial}/consumption/` | **key** | Gas consumption (kWh for SMETS1, m³ for SMETS2) | as above |
| `GET /electricity-meter-points/{mpan}/` | none | GSP/region for an MPAN | — |
| `GET /industry/grid-supply-points/?postcode=` | none | GSP lookup by postcode | `postcode` |

**Pagination:** default `page_size` is 100; consumption supports up to 25000, price feeds up to 1500. Follow the `next` URL for subsequent pages. Consumption supports `order_by=period` (ascending); by default results are newest-first.

**Consumption vs price boundary semantics (a documented gotcha):**
- Consumption: returns records with `interval_start >= period_from` and `<= period_to`.
- Price: returns records with `valid_to >= period_from` and `valid_from < period_to`. These differ, so identical parameters return different row counts.

**Units:** Electricity always kWh (to 0.001 kWh; billing rounds to 0.01 kWh using banker's/unbiased rounding, where numbers ending in 5 round to the nearest even). Gas: **SMETS1 returns kWh; SMETS2 returns m³** and must be converted to kWh via `kWh = m³ × calorific_value × 1.02264 / 3.6` (calorific value ≈ 39–40 MJ/m³, i.e. roughly ~11 kWh per m³ with the standard volume-correction factor; a coarse rule of thumb is ~10–11 kWh per m³).

**Rate feed formats:** Each rate row has `value_exc_vat`, `value_inc_vat`, `valid_from`, `valid_to` (null `valid_to` = still active). Octopus recommends billing using `value_exc_vat` then applying VAT (5% domestic) at the end to match their rounding: multiply rounded consumption (0.01 kWh) by the exc-VAT price, round the half-hourly cost, sum, add standing charge, then apply 5% VAT.

### 1.3 Tariff-specific behaviour

- **Agile Octopus** (`AGILE-*`): one price row per 30-min slot. Next-day prices publish daily **between 4pm and 8pm local (usually ~4pm)**, from day-ahead EPEX wholesale auctions, covering 11pm–11pm. **"Price Cap Protect"** caps the unit rate at 100p/kWh inc VAT (Octopus sets the exc-VAT cap at ~95p so that the price is capped at 100p/kWh once 5% VAT is added; older Agile versions used a 35p cap). Plunge/negative pricing occurs. Region prices can be derived from region C via published regional multipliers + peak adders. (Note: Octopus states it passed a wholesale cost reduction through to Agile as a flat reduction of 3.5p/kWh on every half-hourly rate from 1 April 2026 — evidence of how frequently the formula changes.)
- **Octopus Go** (`GO-*`): fixed cheap overnight window (original: 5p for 00:30–04:30 local); rate rows are sparse (only price changes), not per-half-hour, so consumers must expand them to match half-hourly consumption.
- **Tracker** (Tracker products): one price per day per fuel, published daily. Caps 100p/kWh electricity, 30p/kWh gas. Available via the standard products API.
- **Outgoing Octopus** (`OUTGOING-*`): flat export tariff, **currently 12p/kWh — cut from 15p on 1 March 2026** (its first export-rate change since 2022, per Octopus's help article). Queried like any import tariff but under an export MPAN with `direction: EXPORT`.
- **Agile Outgoing** (`OUTGOING-AGILE-*`): half-hourly export prices published ~4pm.
- **Flux / Intelligent Flux** (`FLUX-*`): solar+battery import/export tariff with a peak export band.
- **Export consumption** uses the same `/consumption/` endpoint on the export MPAN; the field is still called `consumption` but represents export.

### 1.4 GraphQL API — queries & mutations

Base URL `https://api.octopus.energy/v1/graphql/`; GraphiQL IDE available at the same URL (also `https://auth.octopus.energy/graphql/`). **Always returns HTTP 200**; errors appear in the `errors` array with codes like `KT-CT-1112` (no auth header), `KT-CT-1111` (unauthorized), `KT-CT-1113` (disabled field), `KT-CT-1188` (complexity exceeded), `KT-CT-1199` (rate limited), `KT-CT-1189` (node count exceeded).

**Documented / stable consumer surface:**

- `account(accountNumber: String!)` → `status`, `balance`, `overdueBalance`, billing name/address, `ledgers { ... }`, `properties { ... }`, `electricityAgreements(active: Boolean)`, `gasAgreements`, `payments`, `paymentSchedules`, `paymentForecast`, `transactions`.
- `account.ledgers[]` → `id`, `name`, `ledgerType`, `balance`, `amountOwedByCustomer`, `statements { ... }`, `invoices { ... }`, `transactions { ... }` (all Relay-paginated connections with `pageInfo`/`edges`/`node`).
- Meter/device discovery: `account.electricityAgreements(active:true) { meterPoint { meters(includeInactive:false) { smartDevices { deviceId } } } }` — the `deviceId` is required for telemetry.
- `accountDebtPosition(accountNumber, asOn)`, `balanceForecast(accountNumber)`, `accountPaymentById`, `accountStatusSearchByNumber`.
- `addressUprns` / `addressMeterpoints(uprn, postcode)` — meter-point discovery for a postcode (replaced the deprecated `addresses` query; backed by ECOES/Xoserve, cached 1 month).

**Live smart-meter telemetry (Home Mini required):**
```graphql
{ smartMeterTelemetry(deviceId: "<GUID>", grouping: TEN_SECONDS,
    start: "2023-07-01T15:38:00+01:00", end: "2023-07-02T00:00:00+01:00") {
    readAt consumption consumptionDelta demand costDelta } }
```
`consumption` is a cumulative register read; `demand` is instantaneous watts (negative when exporting on a SMETS2 import/export meter). Refresh ~10–30s. `grouping` supports `TEN_SECONDS` and coarser. The export and import meter share the same `deviceId`.

**Measurements API (newer, unified consumption/generation):** `measurements(...)` with `utilityFilters: [{ electricityFilters: { readingFrequencyType: THIRTY_MIN_INTERVAL|DAY_INTERVAL|...|MONTHLY_INTERVAL, marketSupplyPointId, readingDirection: CONSUMPTION|GENERATION, deviceId, registerId } }]`, plus `startAt`, `endAt`, `timezone: "Europe/London"`, `first`. Returns readings converted to industry (local) time. `RAW_INTERVAL` = 30 min in GB.

**Saving Sessions / Octoplus (community-reverse-engineered; internal but widely used):**
```graphql
query savingSessionInfo($accountNumber: String!) {
  savingSessions(accountNumber: $accountNumber) {
    account(accountNumber: $accountNumber) {
      hasJoinedCampaign
      joinedEvents { status startAt endAt eventId rewardGivenInOctoPoints
        energySavedInKwh baselineConsumptionDeltaKwh consumptionDeltaKwh
        netReductionPctRank percentageSaved }
    }
    events { code startAt endAt id rewardPerKwhInOctoPoints totalParticipants }
  }
}
```
Join mutation (excluded from a read-only server; documented here for completeness):
```graphql
mutation Join($accountNumber: String!, $eventCode: String!) {
  joinSavingSessionsEvent(input: {accountNumber: $accountNumber, eventCode: $eventCode}) {
    joinedEventCodes } }
```
(`eventCode` comes from `events[].code`; ineligibility error `KT-GB-5117`.)

Octoplus points balance:
```graphql
{ octoPoints { account { currentPointsInWallet } } }
```
Per Octopus's official "What are Octopoints?" guidance, **800 Octopoints = £1** (each point = 0.125p), redeemed via a "Convert Octopoints to credit" action. Redeem-for-credit mutation: `redeemLoyaltyPointsForAccountCredit(input:{accountNumber, points})` — **input type/return fields unconfirmed** (validate against the live schema). Related documented mutation `awardLoyaltyPoints(input: AwardLoyaltyPointsInput!)`. `claimOctoplusReward(accountNumber, offerSlug)` (used for Greggs/Caffè Nero perks) is **deprecated (marked deprecated ~Feb/Mar 2026, scheduled for removal in August 2026)**.

**Free Electricity / "Power Up" sessions (added to schema 2025-05-29):**
```graphql
query($accountNumber: String!, $mpan: String!) {
  customerFlexibilityCampaignEvents(accountNumber: $accountNumber,
    supplyPointIdentifier: $mpan, campaignSlug: "free_electricity", first: 10) {
    edges { node { code startAt endAt } } } }
```

**Intelligent Octopus (EV) dispatches (community-reverse-engineered):**
```graphql
query PlannedDispatches($input: String!) {
  plannedDispatches(accountNumber: $input) {
    startDt endDt delta meta { location source } } }
query { completedDispatches(accountNumber: "...") { startDt endDt deltaKwh delta source } }
```
- Planned dispatches represent expected smart-charging slots (car/heat pump/battery); they can be added/removed at the last minute and are typically cleared from the API at 17:00 if the vehicle is not plugged in; a planned dispatch is **not** a guarantee it will be actioned (community reports show planned slots sometimes still billed at peak, and with Ohme integrations `plannedDispatches` are only a superset used to influence Ohme's own schedule).
- Completed dispatches are historical; Octopus does **not** provide the cause of a completed dispatch, and does **not** retain historic dispatch data server-side (community integrations store it locally).
- Related: device registration, vehicle charging preferences (target time/SoC), `accountIoEligibility(accountNumber, propertyId){ isEligibleForIo }`, and `devices(...)`/`SmartFlexInverter`/`SmartFlexDevice`. The old `batteryDevice(accountNumber, propertyId)` query is **deprecated (marked 2025-09-08, removal on/after 2026-03-01)** in favour of `SmartFlexInverter` on the `devices` query. Bump charging and preferences are exposed via mutations (must NOT be exposed by a read-only MCP server).

**Heat pump / Cosy (community-reverse-engineered):** heat-pump telemetry exposes lifetime SCOP, lifetime energy input (kWh), lifetime heat output (kWh), live COP, live input power, plus Cosy Pod sensors (temperature, humidity, battery) and per-zone climate/target-temperature control. Data is cloud-polled with delay. Field names are not officially documented and some sensors can report spurious/negative values on certain installs.

### 1.5 Rate limits, freshness, retention, gotchas

- **GraphQL usage constraints (documented):** per-request **complexity limit 200**; **hourly points allowance** default **50,000 points for account users** (100,000 organisations, 300,000 OAuth applications), dynamically scaled up for users managing many supply points; **max 10,000 nodes per request**; forced Relay pagination with `first` < 100; request-specific static/dynamic per-field rate limits (e.g. login). The `rateLimitInfo` query returns the remaining balance. Field complexity is visible via the `X-Kraken-Query-Complexity: true` header or the IDE.
- **REST rate limits:** Octopus does not publish a precise documented numeric REST limit; community practice is to stay well under a modest request rate (a few requests/second at most) and cache aggressively. Community guidance explicitly warns against, e.g., polling every second.
- **Data freshness:** settlement half-hourly consumption is usually available the morning after (SMETS1 often by ~9am but highly variable; SMETS2 typically later in the day), with a meaningful fraction delayed to the next day or beyond. Generally the previous day's full data is present after midnight but gaps happen. Live Home Mini telemetry is near-real-time (~10–30s). Agile/Outgoing next-day prices ~4pm; Tracker daily.
- **Historical retention:** consumption is available back to smart-meter installation; tariff rate history is available for the life of the tariff. Intelligent dispatch history is NOT retained server-side.
- **Timezone gotchas:** send params in UTC (`Z`). Consumption `interval_end` can switch to `+01:00` across the BST boundary within a single response; price responses stay UTC. Bulk/CSV price exports are published in CET. Bills bucket half-hours in the timezone of the bill's start date, producing 46-/50-slot edge days at DST boundaries.
- **Missing readings** are common; code must tolerate gaps rather than assume 48 slots/day.

**Documented vs reverse-engineered (flag):** REST endpoints, `obtainKrakenToken`, `account`, ledgers/transactions/statements, `measurements`, `addressMeterpoints`, and the usage-constraint rules are **officially documented**. `smartMeterTelemetry`, `savingSessions`/`joinSavingSessionsEvent`, `octoPoints`, `plannedDispatches`/`completedDispatches`, heat-pump fields, and `redeemLoyaltyPointsForAccountCredit` are **community-discovered / internal** and subject to change without notice.

---

## Part 2 — MCP Server Specification

### 2.1 Architecture & design rationale

**Routing principle:** Prefer REST for anything it covers well (products, tariff rates, half-hourly consumption, GSP), because REST is stable, cacheable, and needs no token for price/product data. Use GraphQL only where REST cannot reach (account financials, live telemetry, saving sessions, points, dispatches, heat pump). This minimises token churn and maximises cache hits.

**Credential handling:** one env-provided API key. REST uses it as Basic-auth username (blank password). GraphQL exchanges it once via `obtainKrakenToken` for a JWT, cached in memory with expiry `now + 55min` (refresh 5 min early); on `KT-CT-1111`/`KT-CT-1112`, transparently re-mint. Never log the key or token.

**Caching layers:**
- Static product catalogue & product detail: TTL 24h.
- Tariff unit rates / standing charges (historical, immutable): TTL 24h–7d; Agile/Outgoing next-day rates: cache until the next 4pm publish.
- Account structure (`/accounts/`): TTL 1h.
- Consumption: TTL 30 min for the trailing day, longer (24h) for older immutable windows.
- JWT: in-memory, 55 min.

**Pagination handling:** the server auto-follows REST `next` links and GraphQL Relay cursors up to a configurable cap, and downsamples/aggregates before returning to the LLM (see §2.6).

### 2.2 Configuration / environment variables

```
OCTOPUS_API_KEY            (required) account API key
OCTOPUS_ACCOUNT_NUMBER     (required) A-XXXXXXXX
OCTOPUS_ELECTRICITY_MPAN   (optional) pin default electricity MPAN
OCTOPUS_ELECTRICITY_SERIAL (optional) pin default elec meter serial
OCTOPUS_GAS_MPRN           (optional) pin default gas MPRN
OCTOPUS_GAS_SERIAL         (optional) pin default gas meter serial
OCTOPUS_EXPORT_MPAN        (optional) pin export MPAN
OCTOPUS_HOME_MINI_DEVICE_ID(optional) telemetry deviceId
OCTOPUS_GAS_UNITS          (optional) kwh|m3 — the account payload cannot be
                           used to tell SMETS1 (kWh) from SMETS2 (m3)
GAS_CALORIFIC_VALUE        (default 39.5) MJ/m3 for the m3→kWh conversion
CACHE_TTL_PRODUCTS         (default 86400)
CACHE_TTL_RATES            (default 86400)
CACHE_TTL_CONSUMPTION      (default 1800)
CACHE_TTL_ACCOUNT          (default 3600)
OCTOPUS_READ_ONLY          (default true) hard-disables all mutations
MAX_ROWS_RETURNED          (default 500) downsample threshold
```

### 2.3 MCP tools

Each tool is read-only. `upstream` marks REST or GraphQL.

| Tool | Description | Key inputs | Output shape | Upstream |
|---|---|---|---|---|
| `get_account_summary` | Account status, balance, properties, meters, current tariffs | none | account object | GraphQL `account` (+REST `/accounts/` fallback) |
| `list_meter_points` | All MPANs/MPRNs, meters, register info, region/GSP | `property_id?` | array of meter points | REST `/accounts/` |
| `get_agreements` | Current + historical tariff agreements per meter point | `mpan?`,`mprn?`,`include_historical?` | agreements[] | REST `/accounts/` |
| `get_electricity_consumption` | HH/daily/weekly/monthly electricity usage with stats | `mpan?`,`serial?`,`period_from`,`period_to`,`group_by?` | aggregates+stats | REST consumption |
| `get_gas_consumption` | Gas usage, auto m³→kWh for SMETS2 | `mprn?`,`serial?`,`period_from`,`period_to`,`group_by?`,`calorific_value?` | aggregates+stats (kWh & m³) | REST consumption |
| `get_export_consumption` | Solar/battery export usage | `mpan?`(export),`serial?`,`period_from`,`period_to`,`group_by?` | aggregates+stats | REST consumption |
| `get_live_telemetry` | Near-real-time demand/consumption (Home Mini) | `device_id?`,`start`,`end`,`grouping?` | telemetry[] (downsampled) | GraphQL `smartMeterTelemetry` |
| `list_products` | Browse tariff catalogue with filters | `brand?`,`is_green?`,`is_variable?`,`is_tracker?`,`is_prepay?`,`available_at?` | products[] | REST `/products/` |
| `get_product` | Product detail incl. per-region tariffs | `product_code`,`tariffs_active_at?` | product | REST `/products/{code}/` |
| `get_unit_rates` | Unit-rate history / Agile HH rates for a tariff | `product_code`,`tariff_code`,`period_from?`,`period_to?`,`rate_type?` | rates[]+stats | REST rates |
| `get_standing_charges` | Standing-charge history, per payment method (rows carry `value_exc_vat`/`value_inc_vat`, **not** a bare `value`; the published charge is the only source -- nothing about it is configurable) | `product_code`,`tariff_code`,`period_from?`,`period_to?`,`payment_method?` | charges[] | REST standing-charges |
| `get_current_datetime` | Current date/time (UTC + Europe/London) with weekday, timezone name, UTC offset — for resolving relative dates | none | `{utc{}, europe_london{}}` | local clock |
| `get_agile_rates` | Current + next-day Agile rates for the region, with cheapest/peak slots | `region?`,`date?` | rates[]+cheapest/most-expensive windows | REST rates |
| `calculate_cost` | Join consumption to rates → cost for a period (import/export) | `mpan/mprn?`,`serial?`,`tariff_code?`,`period_from`,`period_to` | cost breakdown | REST (both) + local calc |
| `compare_tariffs` | Estimate period/annual cost of candidate tariffs vs current usage | `candidate_product_codes[]`,`period_from`,`period_to` | ranked comparison | REST |
| `get_bills` | List statements/invoices with totals | `first?`,`after?` | statements[] | GraphQL ledgers.statements |
| `get_transactions` | Ledger transactions (charges, payments, credits) | `first?`,`after?`,`ledger?` | transactions[] | GraphQL ledgers.transactions |
| `get_balance` | Current balance + ledger breakdown + forecast | none | balance object | GraphQL `account`/`balanceForecast` |
| `get_saving_sessions` | Saving Session events (available + joined) with rewards/results | none | sessions object | GraphQL `savingSessions` |
| `get_octoplus_points` | Octoplus points balance + £ value | none | `{points, gbp_value}` | GraphQL `octoPoints` |
| `get_free_electricity_sessions` | Upcoming Power-Up/free-electricity events | none | events[] | GraphQL `customerFlexibilityCampaignEvents` |
| `get_intelligent_dispatches` | Planned + completed EV/smart-charging dispatches | none | `{planned[],completed[]}` | GraphQL dispatches |
| `get_heat_pump_status` | Cosy heat-pump COP/SCOP/energy + Cosy Pod sensors | `heat_pump_id?` | heat-pump object | GraphQL heat-pump |
| `lookup_gsp` | Region/GSP for a postcode or MPAN | `postcode?`,`mpan?` | `{gsp, region}` | REST industry |

**Example tool JSON Schema (`get_electricity_consumption`):**
```json
{
  "name": "get_electricity_consumption",
  "description": "Electricity consumption for a meter over a period, aggregated and summarised for analysis. Returns aggregates + statistics, not raw half-hourly dumps unless explicitly requested.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "mpan": {"type": "string", "description": "13-digit MPAN; defaults to configured meter"},
      "serial": {"type": "string", "description": "meter serial; defaults to configured"},
      "period_from": {"type": "string", "format": "date-time", "description": "ISO 8601 UTC, e.g. 2026-01-01T00:00:00Z"},
      "period_to": {"type": "string", "format": "date-time"},
      "group_by": {"type": "string", "enum": ["half_hour","hour","day","week","month","quarter"], "default": "day"},
      "include_raw": {"type": "boolean", "default": false}
    },
    "required": ["period_from","period_to"]
  }
}
```
Output:
```json
{
  "unit": "kWh",
  "group_by": "day",
  "total_kwh": 210.4,
  "series": [{"start":"2026-01-01T00:00:00Z","end":"2026-01-02T00:00:00Z","kwh":7.3}],
  "stats": {"mean":6.8,"min":4.1,"max":11.2,"peak_period":"2026-01-05","n":31,"missing_intervals":4},
  "notes": "4 half-hour intervals missing in range."
}
```

### 2.4 MCP resources

- `octopus://account/summary` — human-readable account + property + meter + current-tariff snapshot.
- `octopus://tariffs/current` — current unit rates & standing charges for the account's active tariffs.
- `octopus://consumption/last-30-days` — pre-aggregated daily consumption (elec+gas+export).
- `octopus://agile/today` — today's + tomorrow's Agile rates with cheapest windows (if on Agile).

### 2.5 MCP prompts

- `analyse_high_bill` — "Why was my bill high last month?": pulls the last two statements, daily consumption deltas, tariff changes, standing-charge changes, and flags anomalies.
- `optimise_tariff` — compares current usage against candidate tariffs and recommends.
- `agile_shifting_advice` — identifies cheapest upcoming Agile windows for load shifting.
- `export_earnings_summary` — summarises export volume × export rate → earnings for a period.
- `saving_session_briefing` — upcoming saving sessions and estimated reward.

### 2.6 Response shaping for LLM consumption

- **Never dump raw half-hourly arrays by default.** A month of HH data ≈ 1,440 rows/meter; return aggregates + statistics instead. Only return raw when `include_raw:true` and the range is small (< 2 days).
- **Downsample** to the requested `group_by`; for HH-on-large-ranges, return daily aggregates plus a `stats` block (mean/min/max/percentiles, peak slot, missing count).
- **Prefer CSV for large tabular series** (token-dense) and JSON for small structured objects. Provide a `format` hint.
- **Round** kWh to 3 dp, prices to appropriate p/kWh, costs to pence.
- **Always annotate** units (kWh vs m³), VAT inclusion, timezone, and missing-data counts.
- **Cap** results at `MAX_ROWS_RETURNED`; when truncating, say so and return summary stats over the full range.

### 2.7 Data models (Pydantic/TypeScript-style)

```typescript
interface MeterPoint { mpan?: string; mprn?: string; direction: "IMPORT"|"EXPORT";
  fuel: "electricity"|"gas"; gsp?: string; region?: string;
  meters: Meter[]; agreements: Agreement[]; }
interface Meter { serialNumber: string; smets?: "SMETS1"|"SMETS2"; deviceId?: string;
  registers: {identifier:string; rate:string; isSettlementRegister:boolean}[]; }
interface Agreement { tariffCode: string; productCode: string; validFrom: string; validTo: string|null; }
interface ConsumptionSeries { unit:"kWh"|"m3"; groupBy:string; total:number;
  series:{start:string;end:string;value:number}[];
  stats:{mean:number;min:number;max:number;n:number;missingIntervals:number}; }
interface UnitRate { valueExcVat:number; valueIncVat:number; validFrom:string; validTo:string|null; }
interface Dispatch { startDt:string; endDt:string; deltaKwh?:number; delta?:string;
  source?:string; location?:string; type:"planned"|"completed"; }
interface SavingSessionEvent { id:string; code:string; startAt:string; endAt:string;
  rewardPerKwhInOctoPoints:number; joined:boolean; result?:{octoPoints:number;kwhSaved:number}; }
interface LedgerBalance { balance:number; ledgerType:string; amountOwedByCustomer:number; }
```

### 2.8 Error handling, backoff, multi-meter

- **REST:** map 401→auth error (bad key), 404→unknown meter/account, 429/5xx→exponential backoff (base 1s, jitter, max ~5 retries). Respect `next` links; abort if page count exceeds a safety cap.
- **GraphQL:** since HTTP is always 200, parse the `errors[]` array. On `KT-CT-1111`/`KT-CT-1112` re-mint token and retry once; on `KT-CT-1188` (complexity) split the query; on `KT-CT-1189` (nodes) shrink `first`/date range; on `KT-CT-1199` back off. Send `X-Kraken-Possible-Errors` in dev to enumerate possible errors up front.
- **Multi-property/meter:** if the account has >1 property or meter and no MPAN/MPRN is pinned or supplied, the tool returns a disambiguation list rather than guessing; pinning via env or explicit parameter resolves it. Aggregate tools iterate meters and label each series by MPAN/MPRN + address. (Because a single meter serial can back multiple MPANs — e.g. import and export — always pair MPAN+serial.)

### 2.9 Security & privacy

- **The API key grants full account access** (including mutations that change tariffs, join campaigns, redeem points, register devices, and can influence billing). Treat it as a high-value secret.
- **Enforce read-only:** `OCTOPUS_READ_ONLY=true` (default) hard-blocks every mutation at the client layer. The server should expose **no** tool that changes tariffs, makes/schedules payments, redeems points, joins/leaves campaigns, registers/deregisters devices, or changes charging preferences. (`joinSavingSessionsEvent`, `redeemLoyaltyPointsForAccountCredit`, `claimOctoplusReward`, bump-charge, and tariff-change mutations are explicitly excluded.)
- **Never expose** the raw API key/JWT, full bank/payment-instrument details, or the full billing address beyond what the analysis needs; redact PII in tool outputs where not essential.
- Prefer a **pre-signed/scoped key** if Octopus offers one; store secrets in env/secret manager, never in code or logs.
- Rate-limit-friendly: cache aggressively to avoid hammering shared limits; the key is shared across all of a user's tooling.

### 2.10 Phased implementation roadmap

- **Phase 0 (foundation):** credential handling, JWT mint/refresh/cache, REST+GraphQL clients, error/backoff, response-shaping utilities, config.
- **Phase 1 (MVP, REST-only):** `get_account_summary`, `list_meter_points`, `get_agreements`, `get_electricity_consumption`, `get_gas_consumption`, `list_products`, `get_product`, `get_unit_rates`, `get_standing_charges`, `lookup_gsp`. Resources: account summary, current tariffs.
- **Phase 2 (analytics):** `calculate_cost`, `compare_tariffs`, `get_agile_rates`, `get_export_consumption`; prompts (`analyse_high_bill`, `optimise_tariff`, `agile_shifting_advice`). Downsampling + CSV output.
- **Phase 3 (GraphQL financials):** `get_bills`, `get_transactions`, `get_balance`.
- **Phase 4 (advanced/telemetry):** `get_live_telemetry`, `get_saving_sessions`, `get_octoplus_points`, `get_free_electricity_sessions`, `get_intelligent_dispatches`, `get_heat_pump_status`. Flag these as "may break" given reverse-engineered status.

---

## Recommendations

1. **Build REST-first (Phase 1).** It delivers the majority of analytical value (consumption, tariffs, cost reconciliation) with the least fragility and no token management. Ship this MVP before touching GraphQL.
2. **Treat GraphQL consumer features as unstable.** Wrap `smartMeterTelemetry`, dispatches, saving sessions, points, and heat-pump queries behind capability checks and graceful degradation; log schema errors and expect breakage. Validate `redeemLoyaltyPointsForAccountCredit` and dispatch field names against the live schema before relying on them (and never expose the mutation).
3. **Enforce read-only hard.** Default `OCTOPUS_READ_ONLY=true`, and simply do not implement mutation tools. This is the single most important safety control given the key's full-access scope.
4. **Invest in response shaping early.** The biggest LLM failure mode here is dumping thousands of half-hourly rows; aggregates + stats + optional CSV should be core infrastructure, not an afterthought.
5. **Get timezone handling right once, centrally.** Normalise all inputs to UTC-`Z`, and clearly label BST/UTC/CET in outputs. Most community bugs trace to this.

**Benchmarks that change the plan:** if Octopus ships an official documented consumer GraphQL surface for telemetry/dispatches (watch the `docs.octopus.energy` announcements/changelog), promote those tools from Phase 4 to Phase 2 and drop the "unstable" wrappers. If rate-limit errors (`KT-CT-1199`) appear in normal use, add a shared token-bucket limiter and lengthen cache TTLs. If a customer has a Home Mini, prioritise `get_live_telemetry`; if they have solar/battery + export MPAN, prioritise `get_export_consumption` and export-earnings analysis.

## Caveats

- **Reverse-engineered surfaces change without notice.** `smartMeterTelemetry`, `savingSessions`/`joinSavingSessionsEvent`, `octoPoints`, `plannedDispatches`/`completedDispatches`, and heat-pump fields are community-discovered; field names and availability may shift. `batteryDevice` (removal on/after 2026-03-01) and `claimOctoplusReward` (removal ~August 2026) already carry deprecation/removal dates.
- **Exact REST numeric rate limits are not officially published**; the server's backoff/caching is precautionary. GraphQL limits (complexity 200, 50,000 points/hour for account users, 10,000 nodes/request) are documented.
- **`redeemLoyaltyPointsForAccountCredit` input/return shape is unconfirmed** and is intentionally not exposed by the read-only server. The `devicesRewardPerKwhInOctoPoints` field and an `upcomingEvents` query name mentioned in some community discussion could not be verified — the public schema uses `events`.
- **Gas kWh conversion uses a calorific value that varies** by region/time; without the exact per-period calorific value from the bill, computed gas kWh for SMETS2 meters is approximate (~11 kWh/m³ with the standard 1.02264 volume-correction factor).
- **Data delays and gaps are inherent**; any cost reconciliation may be incomplete until settlement data lands, and dispatch/telemetry availability depends on hardware (Home Mini) and provider integration (e.g., Ohme vs native Intelligent Octopus).
- **Point-in-time figures** (Outgoing 12p/kWh from 1 March 2026; 800 Octopoints = £1; the 3.5p/kWh Agile reduction from 1 April 2026; Agile's 100p/kWh cap) reflect values reported around 2026 and are subject to change by Octopus. Verify current values against the live product/tariff endpoints at runtime rather than hard-coding them.