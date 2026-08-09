#!/usr/bin/env python3
"""cal-mesh weather capability — Level 3, Stage 1 (harness-fetched, injected as context).

The MODEL never fetches. This deterministic module does, from ONE whitelisted public
source (US National Weather Service, api.weather.gov), and the responder injects the
resulting compact fact into the tool-locked generation prompt. The model only narrates.

Design invariants (see docs/proposals/level3-weather.md):
  * read-only, public, structured/typed source (no free text, no auth, size-bounded, timeout)
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
_STRONG = re.compile(r"\b(weather|forecast|temperature|temp)\b", re.I)
_WEAK   = re.compile(r"\b(rain|raining|snow|snowing|sleet|wind|windy|humid|humidity|"
                     r"hot|cold|freezing|storm|storms|sunny|cloudy|degrees|precip)\b", re.I)


def wants_weather(text):
    """True if the (addressed-to-Cal) text is a weather query. Run on the RAW text so a
    trailing '?' survives (sanitize strips it)."""
    t = text or ""
    if _STRONG.search(t):
        return True
    weak = {m.group(0).lower() for m in _WEAK.finditer(t)}
    return len(weak) >= 2 or (len(weak) >= 1 and "?" in t)


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


def _to_F(v, unit):
    """Convert to whole degrees F by declared unit. Unknown unit -> None (fail-safe)."""
    if v is None:
        return None
    u = (unit or "").lower()
    if u.endswith("degc"):
        return round(v * 9 / 5 + 32)
    if u.endswith("degf"):
        return round(v)
    return None


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
    desc = (p.get("textDescription") or "").strip()

    tF, mph = _to_F(tv, tu), _to_mph(wv, wu)
    parts = []
    if tF is not None:
        parts.append(f"{tF}F")
    if desc:
        parts.append(desc)
    if mph is not None:
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
