#!/usr/bin/env python3
"""cal-mesh weather capability — Level 3, Stage 1 (harness-fetched, injected as context).

The MODEL never fetches. This deterministic module does, from ONE whitelisted public
source (US National Weather Service, api.weather.gov), and the responder injects the
resulting compact fact into the tool-locked generation prompt. The model only narrates.

Design invariants (see docs/proposals/level3-weather.md):
  * read-only, public, TYPED source (no auth, size-bounded, timeout). NOTE, corrected
    2026-08-11 after review: this is NOT entirely free-text-free. `textDescription` is
    prose from NWS and is spliced into the prompt, so it is length-capped here
    (_DESC_MAX) rather than trusted. It is NWS-controlled over HTTPS with no attacker
    path, but the old "no free text" claim was inaccurate and is not a control.
  * fail-safe: any error / missing value -> return None (the responder then says it can't
    reach weather; it NEVER invents a number)
  * location: a named place is honored ONLY if it is on the operator's whitelist; an
    arbitrary user-named place is dropped (never propagated into the prompt or a URL)
  * output is imperial (F, mph) per Dean's standing preference
"""
import os, re, json, time, urllib.request, urllib.parse

BASE  = os.path.expanduser("~/cal-mesh")
CACHE = os.path.join(BASE, "weather-cache.json")   # {latlon: {station, ts}} — station resolution TTL
CACHE_TTL_S = 86400
ALLOWED_HOST = "api.weather.gov"                   # the ONLY host this module will ever fetch

# --- intent ---------------------------------------------------------------
# Strong words fire on their own; weak words need reinforcement (2+ distinct, or a '?').
# This kills single-word false-fires like "I have a cold" / "that's hot" (review #8).
# "heat index" / "wind chill" / "feels like" are STRONG: they are unambiguous weather asks and
# there is no other thing they could mean. Added 2026-08-11 — the capability that reports the
# heat index shipped without them, so asking "what's the heat index?" by name produced NO
# weather lookup at all. ("wind chill" only worked by accident: "wind" is a weak word and the
# question mark carried it.) Build the answer AND check the way people will ask for it.
_STRONG = re.compile(r"\b(weather|forecast|temperature|temp|heat\s*index|wind\s*chill|"
                     r"feels?\s+like|dew\s*point|humidity)\b", re.I)
# Forecast-shaped asks. The capability holds CURRENT OBSERVATIONS ONLY, so these cannot be
# answered — and answering them with current conditions is the exact confident-wrongness the
# roadmap forbids ("clear skies" to "is it going to rain?"). Detected deterministically here
# and answered with a FIXED string; the model is never asked to narrate a caveat, because at
# 5-7 words it drops caveats (measured 4/4). Remove this gate only when a real forecast fact
# is being injected — see docs/proposals/level3-weather-point-accuracy.md.
_FORECAST = re.compile(r"\b(forecast|tomorrow|tonight|later|overnight|this\s+(afternoon|evening|week|weekend)|"
                       r"next\s+(hour|day|week)|going\s+to\s+(rain|snow|storm)|will\s+it|gonna|"
                       # time-of-day qualifiers: "at dusk", "by sunset", "before dark" are all
                       # FUTURE states. Added 2026-08-17 when the sun/moon capability went in and
                       # "will it rain at sunset" was answered with a sunset time — the weather
                       # question was never claimed at all, so the time-of-day word won by default.
                       r"(at|by|before|after|around)\s+(dusk|dawn|sunset|sunrise|sundown|dark|"
                       r"nightfall|first\s+light|last\s+light|noon|midnight|morning|afternoon|"
                       r"evening|night))\b", re.I)
_WEAK   = re.compile(r"\b(rain|raining|snow|snowing|sleet|wind|windy|humid|humidity|"
                     r"hot|heat|muggy|sticky|cold|chilly|freezing|storm|storms|sunny|cloudy|"
                     r"degrees|precip)\b", re.I)
# Daily extremes. "Whats high temp today?" is a FORECAST — the day's maximum is not a reading
# any observation carries — and on 2026-08-17 it was answered on the open channel with a
# 13-minute-old 70F, which was that morning's temperature and not the day's high. _FORECAST had
# no notion of high/low, so the refusal built for exactly this was walked around by the most
# natural phrasing of the question.
#
# The word cannot simply be added to _FORECAST: "high" and "low" are ordinary adjectives in a
# PRESENT-tense reading ("high winds", "low humidity", "the high pressure"), and refusing those
# breaks the capability in the other direction. So they are matched only where they NAME a daily
# extreme — beside a temperature word, beside today/tonight, or standing alone as the object of
# the ask. This feeds BOTH the forecast test and the strong-keyword match below, so an ask that
# carries no other weather word still reaches the capability and gets refused BY it, rather than
# falling through to the general model to invent a number.
# Every alternative below requires an explicit TEMPORAL marker, or the bare-object form anchored
# to the end of the ask. Two live regressions taught this:
#   - the old bare "(high|low) + temp" bigram matched regardless of tense, so "whats the high temp
#     RIGHT NOW?" and "cpu high temp?" were refused as forecasts when they had been answered.
#   - the old trailing guard was a negative lookahead (?![\s\w]) which excludes whitespace and
#     word characters but NOT punctuation, so every hyphenated RF term matched: "the low-noise
#     amp", "the high-gain antenna", "the low-pass filter", "the high/low switch" — 16 of 16
#     forms refused, and that is core vocabulary on a ham radio. Anchoring to end-of-ask is the
#     positive form of what the lookahead was trying to say, and it cannot be fooled by a
#     character class nobody thought to exclude.
_EXTREME = re.compile(r"\b(?:todays?|today'?s|tonights?|tonight'?s)\s+(?:high|low)s?\b"
                      r"|\b(?:high|low)s?\s+(?:temp|temps|temperature|temperatures)\b"
                      r"|\b(?:high|low)s?\s+(?:today|tonight|tomorrow|tmrw)\b"
                      r"|\bthe\s+(?:high|low)s?\s*[?.!]*\s*$", re.I)


# An explicitly PRESENT-TENSE ask overrides the daily-extreme reading. "whats the high temp right
# now" is a current observation; "whats the high temp" is the day's maximum. Requiring a temporal
# marker on the extreme instead put the bare, most natural phrasing back on the current-conditions
# path — restoring the original live defect this whole family exists to prevent. Naming the
# present tense is the narrow fix; demanding a future marker was the wide one.
_PRESENT = re.compile(r"\b(right\s*now|just\s*now|currently|at\s+the\s+moment|"
                      r"at\s+present|out\s+there\s+now|this\s+(?:second|minute))\b", re.I)


def wants_forecast(text):
    """True if the ask is about a FUTURE state we cannot source. Run on RAW text, same as
    wants_weather — the words live in parts sanitize would trim."""
    t = text or ""
    if _FORECAST.search(t):
        return True
    return bool(_EXTREME.search(t)) and not _PRESENT.search(t)


def explain_weather_match(text):
    """WHY the text did or did not read as a weather query, in a form safe to publish.

    This is the step between "a message arrived" and "a fact was fetched": the wording alone
    decides which capability fires, with no model involved. The trace could not show it because
    nothing recorded it — and it is exactly where the 2026-08-11 defect lived, when
    "whats the heat index?" matched nothing and the capability never ran.

    wants_weather() is implemented on top of this so the displayed reason and the actual decision
    are the same computation. Two functions that answer the same question separately eventually
    disagree — that bug already happened once here, with the SAME parser (session 116).
    """
    t = text or ""
    # _EXTREME counts as a STRONG match: "whats the high today" carries no other weather word,
    # and without this it never reaches the capability that knows to refuse it.
    strong = sorted({m.group(0).lower() for m in _STRONG.finditer(t)}
                    | {m.group(0).lower().strip() for m in _EXTREME.finditer(t)})
    weak = sorted({m.group(0).lower() for m in _WEAK.finditer(t)})
    q = "?" in t
    if strong:
        via = "strong"
    elif len(weak) >= 2:
        via = "two_weak"
    elif len(weak) >= 1 and q:
        via = "weak_plus_question"
    else:
        via = None
    # REVERTED 2026-08-17: a "weak word + forecast phrase" branch briefly lived here, added so
    # "will it rain at sunset" would be claimed by weather rather than answered with a sunset
    # time. It was far too wide — measured, it claimed 210 of 210 synthetic weak x forecast pairs
    # and 13 of 14 realistic non-weather sentences ("i have a cold, will it get better", "cold
    # boot the node later"), each getting a nonsense forecast refusal, and it dragged the embedded
    # calc rescue with it so "cold box 5 * 3 later" started computing. The right place to solve
    # that collision is the capability doing the stealing: sun/moon now declines any message
    # carrying a weather word at all. A capability should give way to its neighbour, not grab
    # wider to beat it.
    return {"strong": strong, "weak": weak, "question": q, "via": via}


def wants_weather(text):
    """True if the (addressed-to-Cal) text is a weather query. Run on the RAW text so a
    trailing '?' survives (sanitize strips it)."""
    return explain_weather_match(text)["via"] is not None


# --- location whitelist ---------------------------------------------------
def _parse_places(cfg):
    """WEATHER_PLACES = 'name:lat,lon;name2:lat,lon' -> {name_lower: 'lat,lon'}."""
    out = {}
    for part in (cfg.get("WEATHER_PLACES", "") or "").split(";"):
        part = part.strip()
        if not part or ":" not in part:
            continue
        name, latlon = part.split(":", 1)
        if name.strip() and "," in latlon:
            out[name.strip().lower()] = latlon.strip()
    return out


def resolve_location(cfg, text):
    """Return ('label', 'lat,lon'). A named place is used ONLY if whitelisted; otherwise
    fall back to WEATHER_POINT (the default public reference). Arbitrary named places are
    NEVER propagated — this closes the location-as-exfil path."""
    default = (cfg.get("WEATHER_POINT", "") or "").strip()
    t = (text or "").lower()
    for name, latlon in _parse_places(cfg).items():
        if re.search(r"\b" + re.escape(name) + r"\b", t):
            return name, latlon
    return "default", default


# --- fetch (NWS) ----------------------------------------------------------
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse redirects — a 3xx to another host would be SSRF (review #3)."""
    def redirect_request(self, *a, **k):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _get_json(url, cfg, timeout, max_bytes=200_000):
    # Enforce the allow-list on EVERY fetch, including URLs that came back inside an NWS
    # response (observationStations, station id). https + api.weather.gov only, no redirects.
    u = urllib.parse.urlparse(url)
    if u.scheme != "https" or u.hostname != ALLOWED_HOST:
        raise ValueError(f"blocked non-allowlisted URL: {u.scheme}://{u.hostname}")
    req = urllib.request.Request(url, headers={
        "User-Agent": cfg.get("WEATHER_UA", "cal-mesh/1.0"),
        "Accept": "application/geo+json"})
    with _OPENER.open(req, timeout=timeout) as r:
        data = r.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValueError("weather response too large")
    return json.loads(data.decode("utf-8", "replace"))


def _load_cache():
    try:
        return json.load(open(CACHE))
    except Exception:
        return {}


def _save_cache(c):
    try:
        tmp = CACHE + ".tmp"
        json.dump(c, open(tmp, "w"))
        os.replace(tmp, CACHE)
    except Exception:
        pass


def _miles(lat1, lon1, lat2, lon2):
    """Great-circle distance in miles (imperial per the operator's standing preference)."""
    import math
    r = 3958.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def _nearest(feats, latlon):
    """Pick the station actually closest to the point.

    The list from `observationStations` is NOT distance-sorted — measured against a real
    point, features[0] was 5.4 mi away while features[1] was 4.8 mi. Taking [0] was an
    unverified assumption. Stations without usable geometry are skipped; if none has any,
    fall back to the first entry rather than failing (a slightly-farther station beats no
    weather at all)."""
    try:
        lat, lon = [float(x) for x in latlon.split(",")]
    except Exception:
        return feats[0] if feats else None
    best, best_d = None, None
    for f in feats:
        try:
            c = (f.get("geometry") or {}).get("coordinates") or []
            d = _miles(lat, lon, float(c[1]), float(c[0]))
        except Exception:
            continue
        if best_d is None or d < best_d:
            best, best_d = f, d
    return best if best is not None else (feats[0] if feats else None)


def _station_for(cfg, latlon, timeout, get):
    """Resolve (and cache) the nearest observation-station URL for a 'lat,lon' point."""
    cache = _load_cache()
    ent = cache.get(latlon)
    if ent and time.time() - ent.get("ts", 0) < CACHE_TTL_S and ent.get("station"):
        return ent["station"]
    pts = get(f"https://api.weather.gov/points/{latlon}", cfg, timeout)
    stations_url = (pts.get("properties") or {}).get("observationStations")
    if not stations_url:
        return None
    st = get(stations_url, cfg, timeout)
    feats = st.get("features") or []
    if not feats:
        return None
    pick = _nearest(feats, latlon)
    station = (pick or {}).get("id")        # full URL, e.g. https://api.weather.gov/stations/KXYZ
    if not station:
        return None
    cache[latlon] = {"station": station, "ts": time.time()}
    _save_cache(cache)
    return station


# --- fact formatting (pure) ----------------------------------------------
_DIRS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
         "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]


def _compass(d):   return _DIRS[int((d / 22.5) + 0.5) % 16]


# A unit token, not merely a suffix. `endswith("degc")` also accepted "wmoUnit:mydegc" and
# converted it (review finding 6) — the stated rule is that an unrecognised unit is DROPPED,
# so the match has to end at a token boundary rather than anywhere in the string.
_UNIT_F_RE = re.compile(r"(?:[a-z]+:)?deg([cf])$")

# Values outside this are not weather, they are a broken feed. Bounds are deliberately wider
# than any Earth record (-129F Vostok, 134F Death Valley) so a real extreme is never dropped,
# while 392F and -460F — both produced from malformed input during review — are.
_PLAUSIBLE_F = (-150.0, 200.0)


def _to_F(v, unit):
    """Convert to whole degrees F by declared unit. Unknown unit or unusable value -> None.

    Rejects bools explicitly: JSON `true` is numerically 1, so a `{"value": true}` heat index
    converted cleanly to 34F and shipped (review finding 2) — a wrong-but-plausible number, and
    one pointing the *safe* direction on a dangerous day, which is the worst possible failure
    for this field. Also rejects NaN/Infinity, which `json.loads` accepts by default."""
    if v is None or isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    try:
        if v != v or v in (float("inf"), float("-inf")):   # NaN / +-Inf
            return None
    except TypeError:
        return None
    m = _UNIT_F_RE.match((unit or "").lower())
    if not m:
        return None
    f = v * 9 / 5 + 32 if m.group(1) == "c" else v
    if not (_PLAUSIBLE_F[0] <= f <= _PLAUSIBLE_F[1]):
        return None
    return round(f)


def _to_mph(v, unit):
    """Convert to whole mph by declared unit. Unknown unit -> None (fail-safe)."""
    if v is None:
        return None
    u = (unit or "").lower()
    if u.endswith("km_h-1"):
        return round(v * 0.621371)
    if u.endswith("m_s-1"):
        return round(v * 2.2369363)
    if u.endswith("mi_h-1"):
        return round(v)
    return None


# Apparent temperature — how hot or cold it FEELS, which is the number a person acts on and
# can differ from air temperature by a lot. NWS publishes `heatIndex` when it is hot and
# `windChill` when it is cold, nulling whichever does not apply. Both arrive in degC and must
# go through _to_F, whose conversion is driven by the declared unitCode, so a unit change
# cannot silently produce a wrong-but-plausible number (the windGust km_h-1 near-miss of
# 2026-08-10, where a 25 mph gust nearly went out as 91 mph).
#
# Only reported when it differs from air temperature by at least this much. Below that it
# spends words on nothing a person would do anything differently about, and on a 5-7 word
# radio message every word displaces another. Measured 2026-08-11 at the station Cal uses:
# 95F air, 107F heat index — a 12F gap, and NWS "Danger" territory.
_APPARENT_MIN_DELTA_F = 3

# A delta threshold alone is the wrong rule, and review proved it: 102F air with a 104F heat
# index sits in the NWS DANGER band and was DROPPED for a 2F delta, while 79F/82F — entirely
# harmless — was reported. That is the same silent omission this whole feature exists to fix,
# just moved. So a value inside a genuinely hazardous band is always reported, however small
# the gap. NWS heat-index bands: Caution 80, Extreme Caution 90, Danger 103, Extreme Danger 125.
_HEAT_ALERT_F  = 103
_CHILL_ALERT_F = 0

# Each index is only DEFINED over part of the range — NWS's heat-index table starts at 80F and
# the wind-chill formula is specified for 50F and below. Outside its own domain a value is not
# a cold reading of a hot day, it is garbage. Caught by the eval: a direction check alone let
# "23F, heat index 107F" through, because 107 really is above 23.
_HEAT_VALID_MIN_F  = 80
_CHILL_VALID_MAX_F = 50

# Cap on the source's one free-text field. Real values at Cal's station are '' or 'Clear'.
_DESC_MAX = 40


def apparent_temp(p, tF):
    """(label, degreesF) for heat index or wind chill worth reporting, else None.
    `p` is the observation's properties dict, `tF` air temp in F.

    Reported when it differs from air temperature by >= _APPARENT_MIN_DELTA_F, OR whenever it
    falls in a hazardous band regardless of the gap (see above).

    Heat index is checked first. The two are mutually exclusive in practice — verified during
    review across 500 real observations at 5 stations, they were never both non-null — but the
    ordering is no longer load-bearing, because a value pointing the physically impossible
    direction is now rejected outright rather than merely deprioritised.

    DIRECTION IS ENFORCED: heat index is by definition >= air temperature and wind chill <= it.
    Review produced '23F, wind chill 43F' and '95F, heat index 75F' from malformed input — both
    physically impossible, and the second understates danger, which is the direction that hurts."""
    if tF is None:
        return None
    for name, key, hotter in (("heat index", "heatIndex", True),
                              ("wind chill", "windChill", False)):
        try:
            x = p.get(key) or {}
            v = _to_F(x.get("value"), x.get("unitCode"))
        except Exception:
            continue          # a malformed field costs that field, never the whole observation
        if v is None:
            continue
        if hotter and (v < tF or tF < _HEAT_VALID_MIN_F):
            continue      # heat index below air temp, or claimed on a cold day: not defined
        if not hotter and (v > tF or tF > _CHILL_VALID_MAX_F):
            continue      # wind chill above air temp, or claimed on a warm day: not defined
        hazardous = (v >= _HEAT_ALERT_F) if hotter else (v <= _CHILL_ALERT_F)
        if abs(v - tF) >= _APPARENT_MIN_DELTA_F or hazardous:
            return name, v
    return None


def obs_age_s(obs, now=None):
    """Seconds since the observation was taken, or None if it has no parsable timestamp."""
    ts = ((obs or {}).get("properties") or {}).get("timestamp")
    if not ts:
        return None
    try:
        from datetime import datetime, timezone
        t = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return ((now or datetime.now(timezone.utc)) - t).total_seconds()
    except Exception:
        return None


def format_fact(obs, label="default", max_age_s=None, now=None):
    """Turn an NWS latest-observation payload into a compact imperial fact string, or None
    if there's nothing usable (which the responder treats as 'can't reach weather').
    Conversions are driven by each field's declared unitCode — a value in an UNEXPECTED unit
    is dropped rather than mis-converted, so a wrong-but-plausible number never goes on air.

    `max_age_s`, when given, applies the same discipline to TIME: a station that has stopped
    reporting would otherwise be served as "current" indefinitely with nothing to show for it.
    An observation older than the limit — or one with no usable timestamp while a limit is in
    force — returns None, which the responder already treats as "can't reach weather." Default
    None keeps the function's old behaviour for callers (and tests) that pass raw payloads."""
    if max_age_s is not None:
        age = obs_age_s(obs, now=now)
        if age is None or age > max_age_s:
            return None
    p = (obs or {}).get("properties") or {}

    def fld(k):
        x = p.get(k) or {}
        return x.get("value"), x.get("unitCode")

    tv, tu = fld("temperature")
    wv, wu = fld("windSpeed")
    dv, _  = fld("windDirection")
    # The one free-text field from the source, and it goes into the prompt. Real values are a
    # handful of characters ("Clear"); review demonstrated a 5000-char value producing a
    # 5323-char prompt with no cap anywhere. Capped, not trusted — see the module docstring.
    desc = (p.get("textDescription") or "")
    desc = desc.strip()[:_DESC_MAX] if isinstance(desc, str) else ""

    tF, mph = _to_F(tv, tu), _to_mph(wv, wu)
    app = apparent_temp(p, tF)
    parts = []
    if tF is not None:
        parts.append(f"{tF}F")
    # Ranked, because the reply is 5-7 words and the model drops what does not fit (measured
    # 4/4 at that length). When it feels 3F+ different from the air temperature, that IS the
    # weather as far as a person outdoors is concerned, so it outranks wind and wind is left
    # out of the fact entirely. This is the harness CHOOSING WHICH FACT to supply — never
    # telling the model what to conclude — which is the rule both reviewers arrived at
    # independently (docs/proposals/level3-weather-intent-layer.md §2).
    if app:
        parts.append(f"{app[0]} {app[1]}F")
    if desc:
        parts.append(desc)
    if mph is not None and not app:
        parts.append(f"wind {_compass(dv)} {mph} mph" if dv is not None else f"wind {mph} mph")
    if not parts:
        return None
    prefix = "" if label == "default" else f"{label}: "
    return prefix + ", ".join(parts)


def fetch_current(cfg, latlon, label="default", get=_get_json, meta=None):
    """Compact current-conditions fact for 'lat,lon', or None on ANY failure (fail-safe).
    `get` is injectable so the eval can exercise this without touching the network.
    `meta`, if a dict is passed, is filled with provenance (station URL, observation age) for
    the decision trace — optional so no existing caller has to change.

    NOTE ON WHAT THIS IS: a single station's point observation, which can be several miles
    from `latlon`. It is a real measurement of somewhere nearby, NOT an estimate for the
    point itself — those differ by several degrees routinely. See docs/proposals/."""
    if not latlon or "," not in latlon:
        return None
    try:
        timeout = float(cfg.get("WEATHER_TIMEOUT_S", "6"))
        max_age = float(cfg.get("WEATHER_MAX_OBS_AGE_S", "5400"))
        station = _station_for(cfg, latlon, timeout, get)
        if not station:
            return None
        obs = get(station.rstrip("/") + "/observations/latest", cfg, timeout)
        if meta is not None:
            meta["station"] = station.rstrip("/").rsplit("/", 1)[-1]
            age = obs_age_s(obs)
            meta["obs_age_s"] = round(age) if age is not None else None
        return format_fact(obs, label, max_age_s=max_age)
    except Exception:
        return None
