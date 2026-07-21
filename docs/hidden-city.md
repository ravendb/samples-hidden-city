# Hidden City Ticketing — Spec

## What It Is

Hidden city ticketing (throwaway ticketing) exploits airline hub pricing:
a ticket to a further destination **through** your real target is cheaper than
flying directly to that target.

**Example:**
- WAW → LHR direct: 2800 PLN
- WAW → JFK via LHR: 1650 PLN
- User buys the JFK ticket and exits in London

This happens because airlines price routes based on competition at the endpoint,
not at the hub. Routes to major hubs (LHR, JFK, FRA) are highly competitive,
but those hubs as a layover on onward journeys can be cheaper entry points.

## Legal and Ethical Constraints

- Display hidden city candidates as **information only**
- **Never automate booking** — airlines prohibit this in their ToS; repeated use
  can result in account bans and ticket invalidation
- Show a disclaimer on every hidden city result
- Do not store personally identifiable booking data

## Detection Algorithm

### Input

```
origin:           IATA code of departure airport (e.g. WAW)
real_destination: IATA code the user actually wants to reach (e.g. LHR)
date:             departure date
constraints:      carry_on_only, max_stops, max_duration_min
```

### Step 1 — Find the direct price

Query RavenDB for `routes/{origin}-{real_destination}`. If stale (> 2h),
call `get_live_prices` (Travelpayouts) to refresh. This is `price_direct`.

### Step 2 — Find routes through real_destination to anywhere

Query RavenDB: `WHERE hubs CONTAINS real_destination AND origin = origin`.
This returns all routes where `real_destination` is a layover hub.
These are the candidate decoy routes.

### Step 3 — Compare prices

For each candidate decoy route with price `price_hidden`:

```
if price_hidden < price_direct:
    savings     = price_direct - price_hidden
    savings_pct = savings / price_direct
    → candidate for scoring
```

### Step 4 — Score

```python
hidden_city_score = savings_pct * risk_multiplier
```

`risk_multiplier` starts at 1.0 and is reduced by risk factors:

| Risk factor                   | Multiplier | Reason                                              |
|-------------------------------|------------|-----------------------------------------------------|
| Checked baggage               | × 0.2      | Baggage is checked to final destination, not hub    |
| Return ticket on same booking | × 0.3      | Airline may cancel return leg when you skip segment |
| Low-cost carrier              | × 0.7      | Higher enforcement, stricter ToS                    |
| Connection time < 90 min      | × 0.8      | Risk of missing connection used as cover            |

Final score is clamped to [0.0, 1.0].

**Only surface candidates with `hidden_city_score > 0.5`** to the user.
Below 0.5 the savings don't justify the risk profile.

### Step 5 — Store result

Write or update the route document with:
```json
{
  "hidden_city_score": 0.74,
  "hidden_city_via": "LHR",
  "hidden_city_decoy": "JFK",
  "hidden_city_risks": ["checked_baggage"]
}
```

## Risk Enum Reference

```
CHECKED_BAGGAGE        — user has checked baggage
RETURN_SAME_BOOKING    — return ticket on same PNR
LOW_COST_CARRIER       — LCC operating the flight
SHORT_CONNECTION       — connection < 90 minutes
```

## What to Show the User

```
✈ Hidden city opportunity detected

  WAW → LHR  (your destination)
  Book as:   WAW → JFK via LHR
  Price:     1,650 PLN  (vs 2,800 PLN direct — save 41%)

  ⚠ Risks:
  - Only works without checked baggage
  - Do not book a return on the same ticket

  This is for information only. Booking decisions are yours.
```

## Edge Cases

- **One-way only** — hidden city only works on one-way legs; flag if user asks about return
- **Airline enforces** — some airlines (Ryanair, Wizz) have near-zero tolerance; lower score accordingly
- **Price parity** — if `price_hidden >= price_direct`, score is 0.0, do not surface
- **Same airline required** — interline hidden city (two carriers) almost never works; exclude or score very low
- **Minimum savings threshold** — do not surface if savings < 100 PLN regardless of score; not worth the friction
