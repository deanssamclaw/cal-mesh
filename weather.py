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
import os, re, json, time, urllib.request

BASE  = os.path.expanduser("~/cal-mesh")
CACHE = os.path.join(BASE, "weather-cache.json")   # {latlon: {station, ts}} — station resolution TTL
CACHE_TTL_S = 86400

# --- intent ---------------------------------------------------------------
_WEATHER_KW = re.compile(
    r"\b(weather|temp|temperature|forecast|rain|raining|snow|snowing|sleet|"
    r"wind|windy|humid|humidity|hot|cold|freezing|storm|storms|sunny|cloudy|"
    r"degrees|precip)\b", re.I)


def wants_weather(text, min_kw=1):
    """True if the (already-sanitized, already-addressed-to-Cal) text is a weather query.
    Counts DISTINCT keywords; caller can raise the threshold via min_kw if false triggers appear."""
    hits = {m.group(0).lower() for m in _WEATHER_KW.finditer(text or "")}
    return len(hits) >= max(1, int(min_kw))


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
def _get_json(url, cfg, timeout, max_bytes=200_000):
    req = urllib.request.Request(url, headers={
        "User-Agent": cfg.get("WEATHER_UA", "cal-mesh/1.0"),
        "Accept": "application/geo+json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
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
    station = feats[0].get("id")            # full URL, e.g. https://api.weather.gov/stations/KXYZ
    if not station:
        return None
    cache[latlon] = {"station": station, "ts": time.time()}
    _save_cache(cache)
    return station


# --- fact formatting (pure) ----------------------------------------------
_DIRS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
         "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]


def _c_to_f(c):    return round(c * 9 / 5 + 32)
def _kmh_to_mph(k): return round(k * 0.621371)
def _compass(d):   return _DIRS[int((d / 22.5) + 0.5) % 16]


def format_fact(obs, label="default"):
    """Turn an NWS latest-observation payload into a compact imperial fact string, or None
    if there's nothing usable (which the responder treats as 'can't reach weather')."""
    p = (obs or {}).get("properties") or {}

    def val(k):
        return (p.get(k) or {}).get("value")

    tC, desc, wS, wD = val("temperature"), (p.get("textDescription") or "").strip(), \
        val("windSpeed"), val("windDirection")
    parts = []
    if tC is not None:
        parts.append(f"{_c_to_f(tC)}F")
    if desc:
        parts.append(desc)
    if wS is not None:
        parts.append(f"wind {_compass(wD)} {_kmh_to_mph(wS)} mph" if wD is not None
                     else f"wind {_kmh_to_mph(wS)} mph")
    if not parts:
        return None
    prefix = "" if label == "default" else f"{label}: "
    return prefix + ", ".join(parts)


def fetch_current(cfg, latlon, label="default", get=_get_json):
    """Compact current-conditions fact for 'lat,lon', or None on ANY failure (fail-safe).
    `get` is injectable so the eval can exercise this without touching the network."""
    if not latlon or "," not in latlon:
        return None
    try:
        timeout = float(cfg.get("WEATHER_TIMEOUT_S", "6"))
        station = _station_for(cfg, latlon, timeout, get)
        if not station:
            return None
        obs = get(station.rstrip("/") + "/observations/latest", cfg, timeout)
        return format_fact(obs, label)
    except Exception:
        return None
