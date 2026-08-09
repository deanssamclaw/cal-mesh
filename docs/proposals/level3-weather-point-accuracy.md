# Proposal — point-accurate conditions (why Cal's weather reads wrong, and what to do)

**A station five miles away is a real measurement of somewhere else**

*Draft by Cal · v1 2026-08-09 · re: `github.com/deanssamclaw/cal-mesh` · follows
`level3-weather-intent-layer.md`; §4.5 of that doc is the blocker on the obvious fix*

---

## 0. The report

The operator asked Cal for the weather on air and said the answer was wrong. It was — but not
in any way the fail-safes were built to catch. Nothing errored, nothing was stale, no unit was
mis-converted. The trace was clean end to end.

Measured at the moment of the reply:

| source | reading |
|---|---|
| KIXD, 5.4 mi from the reference point — **what Cal said** | 77°F, Mostly Clear, SSE 10 mph |
| KOJC, 4.8 mi away | 79°F, Clear, S 11 mph |
| KMKC, 20.4 mi away | 84°F, Clear |
| **NWS hourly forecast for the reference point itself** | **83°F, Partly Sunny, S 12 mph** |

Six degrees between what Cal transmitted and what any phone at that location would show. The
observation was 24 minutes old, so this is not staleness. **Cal reported something accurate
about an airport and called it "current local weather."**

---

## 1. Two defects, both now fixed

### 1.1 We weren't using the nearest station (FIXED)

`_station_for` took `features[0]` from the `observationStations` list. That list is **not
distance-sorted** — an assumption never verified until now. Measured from the live reference
point:

```
features[0]  KIXD   5.4 mi   <- what we used
features[1]  KOJC   4.8 mi   <- actually nearer
features[2]  KMKC  20.4 mi
```

Now picks by computed great-circle distance. Stations without usable geometry are skipped;
if none has geometry it falls back to the first entry, because a slightly-farther station
beats no weather at all. Live effect: the station moved to KOJC and the fact became
`79F, Clear, wind S 11 mph`.

Worth naming the shape of this bug — a plausible-looking assumption about someone else's API,
never checked, quietly wrong for months. Same family as `features[0]` for alerts, which
`level3-weather-intent-layer.md` §1.3 caught before it shipped.

### 1.2 A stalled station would have read as "current" forever (FIXED)

`format_fact` ignored `properties.timestamp` entirely. A station that stops reporting would
have been served as current indefinitely, with nothing in the reply or the trace to show it.
Every other failure in this capability is fail-safe; time was the gap.

`format_fact(obs, max_age_s=…)` now returns `None` for an observation older than the limit, or
one with no parsable timestamp while a limit is in force — which the responder already handles
as "can't reach weather." Default `WEATHER_MAX_OBS_AGE_S=5400` (90 min): stations report about
hourly, so this tolerates one late cycle and no more. The `max_age_s=None` default keeps the
function's old behaviour for callers that pass raw payloads.

The observation's **station and age** are now recorded and shown in the public decision trace.

---

## 2. The real issue: a point observation is not the weather at a point

Both fixes above are correct and neither closes the six degrees. That gap is **structural**:

- **What we serve:** one station's instrument reading. Ground truth about a spot that may be
  five miles away, and airports are typically flat, open, and cooler than their surroundings.
- **What the question means:** conditions where the asker is.
- **What everything else shows:** NWS's gridded analysis interpolated to the point — which is
  what phone apps, `weather.gov`'s own page, and every consumer source display as "now."

Neither number is a lie. The station is a measurement; the gridpoint is a model estimate. But
only one of them answers *"what's it doing out there?"*, and it isn't the one we're using.

**This is the same class of error as the one that started the intent-layer doc.** There, Cal
answered a forecast question with current conditions — right data, wrong question. Here, Cal
answers a *here* question with *there* data. Both come from the harness composing a fact
without regard to what was actually asked.

---

## 3. The fix, and the tripwire in front of it

The right source is the **hourly forecast** for the grid point (`properties.forecastHourly`,
whose URL already arrives in the `points/` response we fetch). `periods[0]` is the current
hour at the point: `temperature`, `shortForecast`, `windSpeed`/`windDirection`, and
`probabilityOfPrecipitation` — which would also close the rain-question gap from the
intent-layer doc in the same change.

**It cannot be fetched through the current fetcher.** `level3-weather-intent-layer.md` §4.5
measured it: **162–165 KB against `max_bytes = 200_000`** — 83% of the cap, against a horizon
NWS controls and has extended before. And `WEATHER_TIMEOUT_S` is a **socket** timeout, not a
transfer deadline: `r.read()` loops recv, so a slow trickle of 165 KB can blow the wall clock
while never tripping it. When either fires, `fetch_current`'s blanket `except` swallows it and
the operator loses **current conditions too**, logged only as `weather_ok: false`.

So the order is forced:

1. **Harden the fetcher** — a per-call `max_bytes` (hourly needs ~256 KB headroom, observations
   should stay tight), and a **total** wall-clock deadline across all legs, not per request.
   `plan_response` runs inline in the responder's single-threaded loop, so an unbounded fetch
   blocks every other sender's traffic too.
2. **Compose the fact from the hourly period**, harness-extracted field by field with the same
   `unitCode` discipline `format_fact` already applies — never `detailedForecast` prose, which
   measured 6/13 overstatement against a 12% precipitation probability.
3. **Keep the station observation as the fallback**, and as the honest label: if the hourly leg
   fails, say the reading is from a named station rather than silently substituting.

### 3.1 What the reply should say

Terse enough for the 5–7 word budget, honest about which kind of number it is:

- hourly available → *"Eighty three, partly sunny, south twelve"*
- hourly failed, station used → *"Seventy nine at Johnson County field"*
- both failed → the existing fixed *"Can't reach weather right now."*

The middle case matters. Naming the station is how a listener knows they're getting a
measurement from somewhere else, rather than an estimate for where they stand.

---

## 4. Open question — which number is "right" for a mesh?

Genuinely unresolved, and it should be argued rather than assumed.

A mesh is a **field** tool. In the field, a nearby station's real instrument reading may be
*more* useful than a model estimate — especially for wind, where terrain makes the gridded
value soft. The consumer answer (the gridpoint) is the one that matches what a listener sees
on their phone; the station answer is the one that matches a barometer.

Cal's recommendation: **lead with the point estimate** because it matches expectation and
answers the question asked, and **keep the station reading available** rather than discarding
it. Do not average them — a blended number is one nobody can check against any source.

---

## 5. Status

- §1.1 nearest-station — **DONE**, with eval coverage (list deliberately out of distance order,
  missing-geometry skip, no-geometry fallback).
- §1.2 observation-age guard — **DONE**, with eval coverage (fresh passes, stale returns None,
  missing timestamp fails safe, and a stale observation is never served through
  `fetch_current`). Station + age now appear in the public trace.
- §3 point-accurate conditions — **SPEC'D, NOT BUILT.** It changes the number Cal transmits, so
  it goes through the same gate as everything else: refute-it review before code, eval before
  arming, operator's explicit go to arm. The fetcher hardening in §3.1 is a prerequisite and is
  worth doing on its own regardless.
