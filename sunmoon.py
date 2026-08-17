#!/usr/bin/env python3
"""Sun / moon / twilight — a COMPUTE doer. Python owns every digit; no model in the number path.

Why this capability, and why it is second in the build order after the RF pack: it is the only
item on the catalog that is simultaneously high-value and cheap. It is *lookup-y* (nobody carries
tonight's sunset in their head, so Cal beats a laminated card, which is Bob's test for whether a
capability earns its airtime), and it is **offline-resilient** — closed-form astronomy, no network,
no curation, no sourcing, no last-verified date. Under the resilient-first ordering adopted
2026-08-17 that combination is what moves it above propagation.

ALGORITHM. NOAA Solar Calculator equations (themselves a condensation of Meeus, "Astronomical
Algorithms", ch. 25 and 15) for the sun; Meeus ch. 48 low-precision for lunar phase and
illumination. Chosen for correctness-per-line: NOAA claims ~1 minute for latitudes below 72
degrees, which is far inside what anyone needs to know when it gets dark. Full-precision Meeus
would add hundreds of lines to move an answer we round to the minute anyway.

WHAT THIS MODULE DOES NOT DO, deliberately:
  - Moonrise / moonset. The moon moves ~13 deg/day, so rise and set need an iterative altitude
    search rather than the sun's single closed-form hour angle. It is a real field question and it
    is planned, but it carries its own validation burden and is NOT shipped in v1. Asking for it
    gets a refusal, never an estimate.
  - Any position it was not given. The location comes from the caller (the gitignored config),
    never from the message, and no coordinate ever appears in a reply.

KNOWN AND ACCEPTED: THE ANSWER IS ITSELF A WEAK POSITION FIX. Sunrise and sunset times are a
function of latitude and longitude, so publishing them narrows where the observer is. An
adversarial review measured it by brute-force inversion of the published replies: one "when does
it get dark" answer bounds the observer to roughly 293 x 105 miles; two answers from different
seasons bound it to about 21 x 8 miles. That is a real disclosure and it is stated here rather
than left implicit — but it is LOOSER than what the dashboard already publishes, because every
weather reply names the observing station (an airport code, ~1 mile). The capability therefore
adds no practical precision over existing public output. If the station is ever withheld, this
becomes the tightest position signal Cal emits and the decision must be revisited.

REFUSAL EDGE (the discipline that governs every capability here): when the sun does not reach the
requested altitude at all on that date — polar day or polar night, or a twilight depression the
sun never gets to — cos(H) leaves [-1, 1] and there IS no such time. That returns None and the
caller says so. It never clamps, and it never reports the nearest thing it could compute.
"""
import math
import re
from datetime import date as _date, datetime, timedelta, timezone

# --- solar altitude constants ------------------------------------------------------------------
# Geometric sunrise/sunset is taken at -0.833 deg rather than 0: the sun's upper limb touches the
# horizon while its centre is still below, and atmospheric refraction lifts the image further.
# -0.833 = -(34' mean refraction + 16' solar semidiameter). The twilight angles are the standard
# definitions: civil = usable light without artificial illumination, nautical = horizon still
# discernible at sea, astronomical = full darkness.
ALT_SUNRISE = -0.833
ALT_CIVIL = -6.0
ALT_NAUTICAL = -12.0
ALT_ASTRONOMICAL = -18.0

# Events name their altitude by CONSTANT NAME, not by value, and resolve it at call time. Binding
# the value here would freeze a copy at import and quietly fork the source of truth — an eval that
# changed ALT_SUNRISE to prove the refraction correction was load-bearing would then be testing
# nothing at all. (That is exactly what happened when this dict held values.)
_EVENTS = {
    "sunrise": ("ALT_SUNRISE", "rise"),
    "sunset": ("ALT_SUNRISE", "set"),
    "civil_dawn": ("ALT_CIVIL", "rise"),
    "civil_dusk": ("ALT_CIVIL", "set"),
    "nautical_dawn": ("ALT_NAUTICAL", "rise"),
    "nautical_dusk": ("ALT_NAUTICAL", "set"),
    "astronomical_dawn": ("ALT_ASTRONOMICAL", "rise"),
    "astronomical_dusk": ("ALT_ASTRONOMICAL", "set"),
}


def event_altitude(key):
    """The solar altitude for an event, read from the module constant at call time."""
    return globals()[_EVENTS[key][0]]


def _jday(d):
    """Julian day at 00:00 UT on a civil date. Standard Fliegel-Van Flandern via Meeus ch.7."""
    y, m = d.year, d.month
    if m <= 2:
        y -= 1
        m += 12
    a = y // 100
    b = 2 - a + a // 4                      # Gregorian correction
    return (math.floor(365.25 * (y + 4716)) + math.floor(30.6001 * (m + 1))
            + d.day + b - 1524.5)


def _solar(jc):
    """(declination_deg, equation_of_time_minutes) for Julian century jc. NOAA equations."""
    # geometric mean longitude and mean anomaly of the sun
    l0 = (280.46646 + jc * (36000.76983 + jc * 0.0003032)) % 360.0
    m = 357.52911 + jc * (35999.05029 - 0.0001537 * jc)
    e = 0.016708634 - jc * (0.000042037 + 0.0000001267 * jc)   # orbital eccentricity
    mr = math.radians(m)
    # equation of centre: the correction from the mean (uniform) anomaly to the true one
    c = (math.sin(mr) * (1.914602 - jc * (0.004817 + 0.000014 * jc))
         + math.sin(2 * mr) * (0.019993 - 0.000101 * jc)
         + math.sin(3 * mr) * 0.000289)
    true_long = l0 + c
    # apparent longitude: nutation + aberration
    omega = 125.04 - 1934.136 * jc
    lam = true_long - 0.00569 - 0.00478 * math.sin(math.radians(omega))
    # obliquity of the ecliptic, with the same nutation correction
    seconds = 21.448 - jc * (46.8150 + jc * (0.00059 - jc * 0.001813))
    e0 = 23.0 + (26.0 + seconds / 60.0) / 60.0
    ecorr = e0 + 0.00256 * math.cos(math.radians(omega))
    decl = math.degrees(math.asin(math.sin(math.radians(ecorr))
                                  * math.sin(math.radians(lam))))
    # equation of time (minutes): apparent solar time minus mean solar time
    y = math.tan(math.radians(ecorr / 2.0)) ** 2
    l0r = math.radians(l0)
    eot = 4.0 * math.degrees(
        y * math.sin(2 * l0r)
        - 2.0 * e * math.sin(mr)
        + 4.0 * e * y * math.sin(mr) * math.cos(2 * l0r)
        - 0.5 * y * y * math.sin(4 * l0r)
        - 1.25 * e * e * math.sin(2 * mr))
    return decl, eot


def _hour_angle(lat_deg, decl_deg, alt_deg):
    """Hour angle in degrees from local solar noon to the given altitude, or None if unreachable.

    None is the refusal, and it is load-bearing: |cos H| > 1 means the sun does not reach that
    altitude on that date at that latitude. Clamping here would invent a sunrise on a polar night.
    """
    lat, decl, alt = map(math.radians, (lat_deg, decl_deg, alt_deg))
    denom = math.cos(lat) * math.cos(decl)
    if abs(denom) < 1e-12:                  # exactly at a pole
        return None
    cos_h = (math.sin(alt) - math.sin(lat) * math.sin(decl)) / denom
    if cos_h > 1.0 or cos_h < -1.0:
        return None
    return math.degrees(math.acos(cos_h))


def solar_noon_utc(d, lon_deg):
    """datetime (UTC) of solar transit."""
    jc = (_jday(d) - 2451545.0) / 36525.0
    _, eot = _solar(jc)
    minutes = 720.0 - 4.0 * lon_deg - eot
    return datetime.combine(d, datetime.min.time(), timezone.utc) + timedelta(minutes=minutes)


def event_utc(d, lat_deg, lon_deg, alt_deg, direction):
    """datetime (UTC) of a rise/set at a solar altitude, or None if it does not occur.

    Two passes: the first evaluates the sun's position at local solar noon, the second re-evaluates
    at the time the first pass produced. Declination and the equation of time both move measurably
    across a day, and the single-pass form carries that error straight into the answer near the
    solstices, which is exactly when people ask.
    """
    return event_with_reason(d, lat_deg, lon_deg, alt_deg, direction)[0]


def event_with_reason(d, lat_deg, lon_deg, alt_deg, direction):
    """(datetime_or_None, reason_or_None) — ONE computation for both the time and the refusal.

    These used to be two functions evaluating DIFFERENT epochs: the time was refused if either
    refinement pass failed, while the reason looked only at the first. In the band where they
    disagreed the caller got "no event" with no reason at all — an adversarial review found 995
    such (latitude, date, altitude) combinations in 60-90N during 2026 alone, and the caller then
    picked a refusal sentence blind, sometimes the wrong-signed one. Two functions answering the
    same question separately eventually disagree; this codebase has now proved that three times.
    """
    jd0 = _jday(d)
    minutes = None
    decl = None
    for _ in range(2):
        jc = (jd0 - 2451545.0 + (minutes / 1440.0 if minutes is not None else 0.5)) / 36525.0
        decl, eot = _solar(jc)
        ha = _hour_angle(lat_deg, decl, alt_deg)
        if ha is None:
            # The sun's altitude at transit separates midnight sun from polar night: above the
            # target at its highest point means it never descends to it, and vice versa.
            noon_alt = 90.0 - abs(lat_deg - decl)
            return None, ("always_above" if noon_alt > alt_deg else "always_below")
        noon = 720.0 - 4.0 * lon_deg - eot
        minutes = noon + (-4.0 * ha if direction == "rise" else 4.0 * ha)
    t = datetime.combine(d, datetime.min.time(), timezone.utc) + timedelta(minutes=minutes)
    return t, None


def no_event_reason(d, lat_deg, alt_deg):
    """Why an event does not occur: 'always_above' or 'always_below'. None if it does occur.

    Thin wrapper over event_with_reason so it cannot drift from the time computation. Longitude
    does not affect WHETHER an event occurs, only when, so 0.0 is passed deliberately.
    """
    return event_with_reason(d, lat_deg, 0.0, alt_deg, "rise")[1]


def sun_events(d, lat_deg, lon_deg):
    """All eight solar events for a date, UTC. Value is None where the event does not occur."""
    out = {}
    for name, (_, direction) in _EVENTS.items():
        out[name] = event_utc(d, lat_deg, lon_deg, event_altitude(name), direction)
    out["solar_noon"] = solar_noon_utc(d, lon_deg)
    return out


# --- moon --------------------------------------------------------------------------------------
_PHASES = ((0.02, "new moon"), (0.24, "waxing crescent"), (0.26, "first quarter"),
           (0.49, "waxing gibbous"), (0.51, "full moon"), (0.74, "waning gibbous"),
           (0.76, "last quarter"), (0.98, "waning crescent"), (1.01, "new moon"))


def moon_phase(when):
    """(illuminated_fraction, phase_name, waxing) for an aware datetime. Meeus ch.48 low precision.

    Illumination is accurate to well under a percent, which is far finer than the one word a radio
    reply can carry. The phase NAME is taken from the mean elongation rather than from the
    illumination, because illumination alone cannot tell waxing from waning — both limbs read the
    same fraction, and naming a waning moon "waxing" is the kind of confidently-wrong answer this
    tier exists to avoid.

    Evaluated at the instant asked, NOT at local noon. Illumination moves ~1.5 points a day, so
    "today's illumination" has no canonical value and every published table has to pick a
    convention (USNO uses local noon, and is inconsistent about which noon). A live radio reply
    should describe the sky the asker is standing under, so this uses the moment of the question —
    which can differ by a point from a printed almanac. A convention difference, not an error.
    """
    u = when.astimezone(timezone.utc)
    jd = (_jday(u.date()) + (u.hour + u.minute / 60.0 + u.second / 3600.0) / 24.0)
    jc = (jd - 2451545.0) / 36525.0
    d = (297.8501921 + 445267.1114034 * jc - 0.0018819 * jc * jc) % 360.0   # mean elongation
    m = (357.5291092 + 35999.0502909 * jc) % 360.0                          # sun mean anomaly
    mp = (134.9633964 + 477198.8675055 * jc) % 360.0                        # moon mean anomaly
    dr, mr, mpr = map(math.radians, (d, m, mp))
    # phase angle of the moon (Meeus 48.4): 0 = full, 180 = new
    i = (180.0 - d
         - 6.289 * math.sin(mpr)
         + 2.100 * math.sin(mr)
         - 1.274 * math.sin(2 * dr - mpr)
         - 0.658 * math.sin(2 * dr)
         - 0.214 * math.sin(2 * mpr)
         - 0.110 * math.sin(dr))
    k = (1.0 + math.cos(math.radians(i))) / 2.0
    frac = d / 360.0                        # 0 = new, 0.5 = full, measured on the mean elongation
    name = next(n for lim, n in _PHASES if frac < lim)
    return max(0.0, min(1.0, k)), name, frac < 0.5


# --- intent ------------------------------------------------------------------------------------
# Same construction as weather.explain_weather_match: the reason shown in the public trace and the
# decision actually taken are ONE computation. Two functions answering the same question
# separately eventually disagree, and that bug has already happened once in this codebase.
# --- intent ------------------------------------------------------------------------------------
# Same construction as weather.explain_weather_match: the reason shown in the public trace and the
# decision actually taken are ONE computation.
#
# THE FRONT DOOR IS DERIVED FROM THE INTENT TABLE, NOT MAINTAINED BESIDE IT. A separate trigger
# regex is how this codebase has now shipped the same bug three times: a capability that knows a
# phrasing internally but does not RECOGNISE it, so the ask falls through to the language model,
# which invents an answer. ("cal whats the heat index?" matched nothing, session 118.) An
# adversarial review found 17 of 20 natural phrasings of these questions missed here the same way
# — "what time does the sun go down", "when does the sun come up", "how long till dark" — every
# one of them a phrasing _INTENTS already knew. Deriving _SUN from _INTENTS makes that class of
# divergence impossible rather than merely fixed.
_INTENTS = (
    ("noon", re.compile(r"\b(solar\s*noon|sun\s*(?:is\s*)?highest|zenith|"
                        r"highest\s*point)\b", re.I)),
    ("sunrise", re.compile(r"\b(sunrise|sun\s*rise|first\s*light|"
                           r"sun\s*(?:come|comes|coming)\s*up|sun\s*up\b)", re.I)),
    ("dark", re.compile(r"\b(get\s*dark|gets\s*dark|getting\s*dark|dark\s*out|"
                        r"night\s*fall|last\s*light|till\s*dark|until\s*dark|"
                        r"dark\s*by|is\s*it\s*dark|when\s*is\s*it\s*dark|"
                        r"how\s*much\s*light|light\s*left|dark\s*yet)\b", re.I)),
    ("sunset", re.compile(r"\b(sunset|sun\s*set|sundown|"
                          r"sun\s*(?:go|goes|going)\s*down|golden\s*hour)\b", re.I)),
    ("twilight", re.compile(r"\b(twilight|daylight)\b", re.I)),
)

# Unambiguous sun wording: claims on its own.
_SUN_STRONG = re.compile("|".join("(?:%s)" % rx.pattern for name, rx in _INTENTS
                                  if name != "twilight"), re.I)
# Ambiguous wording: a real word in ordinary traffic too, so it needs corroboration. "dawn" is a
# name, "twilight" and "dusk" are nouns people use about other things. Mirrors weather's
# strong/weak split for exactly the same reason.
_SUN_WEAK = re.compile(r"\b(dawn|dusk|twilight|daylight)\b", re.I)
_MOON_STRONG = re.compile(r"\b(moonrise|moonset|moon\s*phase|full\s*moon|new\s*moon|"
                          r"waxing|waning|gibbous|crescent)\b", re.I)
_MOON_WEAK = re.compile(r"\bmoon\b", re.I)
# Collocations where these words are demonstrably NOT about the sky. Found by an adversarial
# review firing on real-looking traffic: "Dawn Smith node status", "twilight zone episode",
# "moon landing year", "daylight savings time when".
_NOT_SKY = re.compile(r"twilight\s*zone|moon\s*landing|moon\s*shot|daylight\s*savings?|"
                      r"moonshine|dark\s*(?:web|mode|horse)|moon\s*pie|"
                      r"how\s*far.{0,12}moon|distance.{0,12}moon", re.I)
# Rise/set for the MOON is not built. It must be recognised so it can be refused explicitly —
# an unrecognised ask would fall through to the general model, which would invent a time.
_MOON_RISESET = re.compile(r"\b(moon\s*rise|moonrise|moon\s*set|moonset)\b|"
                           r"\bmoon\b[^.?!]{0,20}\b(rise|rises|set|sets|up|down)\b", re.I)


def explain_match(text):
    """Why the text did or did not read as a sun/moon query, in a form safe to publish."""
    t = text or ""
    if _NOT_SKY.search(t):
        return {"sun": [], "moon": [], "via": None, "moon_riseset": False,
                "excluded": True}
    q = "?" in t
    sun_s = sorted({m.group(0).lower().strip() for m in _SUN_STRONG.finditer(t)})
    sun_w = sorted({m.group(0).lower() for m in _SUN_WEAK.finditer(t)})
    moon_s = sorted({m.group(0).lower() for m in _MOON_STRONG.finditer(t)})
    moon_w = sorted({m.group(0).lower() for m in _MOON_WEAK.finditer(t)})
    riseset = bool(_MOON_RISESET.search(t))
    sun = sorted(set(sun_s) | set(sun_w))
    moon = sorted(set(moon_s) | set(moon_w))
    if riseset:
        # A moon rise/set ask is claimed UNCONDITIONALLY, weak wording or not, because claiming it
        # is the only way to refuse it. "when does the moon rise" carries no strong token and no
        # question mark; under the weak rule it fell through to the model, which would answer with
        # an invented time. Recognition is the refusal.
        via = "moon_riseset"
    elif sun_s:
        via = "sun"
    elif moon_s:
        via = "moon"
    elif sun_w and q:                       # weak wording needs a question to claim
        via = "sun"
    elif moon_w and q:
        via = "moon"
    else:
        via = None
    return {"sun": sun, "moon": moon, "via": via, "moon_riseset": riseset,
            "excluded": False}


def wants_sunmoon(text):
    return explain_match(text)["via"] is not None


def _bounded(reply, meta, max_chars):
    """Enforce the length bound on EVERY branch, not just the longest one.

    The bound was originally checked on one branch of seven. Nothing overflowed at the default,
    so the constraint held by accident rather than by construction — which is the state a
    capability is in right before someone adds a longer string.
    """
    if reply is not None and len(reply) > max_chars:
        meta["refused"] = "too long"
        return None, meta
    return reply, meta


def _no_event_text(key, reason):
    """The honest sentence for an event that does not occur, keyed to WHICH event is missing.

    'always_above' means the sun never descends to that altitude; 'always_below' means it never
    climbs to it. A single pair of sentences about sunrise/sunset cannot describe a missing
    TWILIGHT boundary, and saying it does is a published falsehood.
    """
    if key in ("sunrise",):
        return "Sun is up all day here" if reason == "always_above" else \
               "Sun does not rise here today"
    if key in ("sunset",):
        return "Sun stays up here today" if reason == "always_above" else \
               "Sun does not rise here today"
    # twilight boundaries
    return "No full dark here tonight" if reason == "always_above" else \
           "Twilight never ends here today"


def _clock(dt, tz):
    """'8:10 PM' in the caller's timezone. No date, no zone name — the reply is for someone
    standing under the same sky.

    Rounds to the nearest minute rather than truncating, which is what USNO and NOAA publish and
    therefore what anyone comparing against a printed table expects. Truncation is up to 59
    seconds early and would disagree with every published source about half the time.
    """
    lt = (dt + timedelta(seconds=30)).astimezone(tz)
    return "%d:%02d %s" % (((lt.hour % 12) or 12), lt.minute, "AM" if lt.hour < 12 else "PM")


def _next_event(now, tz, lat, lon, key):
    """(datetime, is_tomorrow) for the next occurrence of an event, or (None, reason).

    Looks at today first and rolls to tomorrow once today's has passed, because 'when does it get
    dark' asked after dark means tomorrow. The roll is explicit in the reply — one word to remove
    a real ambiguity is worth the airtime.
    """
    today = now.astimezone(tz).date()
    alt, direction = event_altitude(key), _EVENTS[key][1]
    for offset in (0, 1, 2):
        d = today + timedelta(days=offset)
        t, reason = event_with_reason(d, lat, lon, alt, direction)
        if t is None:
            return None, reason
        # The UTC instant can land on the previous or next LOCAL date when the zone offset is far
        # from the longitude, so compare local dates rather than trusting the loop index. Without
        # this, a Chatham Islands observer got a sunset a full day late (measured, 62 min wrong
        # after the roll). The scan runs one day past the roll to cover the shifted case.
        if t > now:
            return t, (t.astimezone(tz).date() != today)
    return None, "not_found"


def answer(text, lat, lon, tz, now, max_chars=160):
    """(reply, meta). reply is None when this capability declines or must refuse.

    Python formats the whole string. Nothing here is passed to a model, and no coordinate ever
    appears in the output — the location is an input, never a fact we report.
    """
    meta = {"intent": None, "refused": None, "event": None, "tomorrow": False}
    m = explain_match(text)
    if not m["via"]:
        return None, meta

    # Epoch bound. The solar model is accurate to about a minute for two centuries either side of
    # J2000, and the lunar series is stated valid 1900-2100. Outside that it does not fail — it
    # DRIFTS, silently, still returning a plausible time. A capability whose whole claim is that
    # Python owns the digits cannot serve a number it has no accuracy claim for.
    if not (1901 <= now.year <= 2099):
        meta["refused"] = "out of epoch"
        return "Date outside my accurate range", meta

    if m["via"] == "moon_riseset":
        # Recognised so it can be refused HONESTLY. Left unrecognised it would fall through to the
        # general model, which would produce a confident time for something we do not compute.
        meta["intent"], meta["refused"] = "moon_riseset", "not built"
        return _bounded("Moon rise/set not built yet, phase only", meta, max_chars)

    if m["via"] == "moon":
        k, name, waxing = moon_phase(now)
        meta["intent"] = "moon"
        return _bounded("Moon %d%%, %s" % (round(k * 100), name), meta, max_chars)

    intent = next((n for n, rx in _INTENTS if rx.search(text)), "dark")
    meta["intent"] = intent

    if intent == "noon":
        # The LOCAL date, not the UTC one. Near local midnight they differ, and using the UTC date
        # answers for the wrong day. Recorded in meta because the rendered reply cannot show it:
        # solar noon shifts only ~13 seconds a day, so a whole-day error is invisible in a time
        # rounded to the minute. A property that cannot be observed cannot be tested, and this one
        # survived every reply-level mutation check until the date was published in the trace.
        local_date = now.astimezone(tz).date()
        t = solar_noon_utc(local_date, lon)
        meta["event"], meta["for_date"] = "solar_noon", local_date.isoformat()
        return _bounded("Solar noon %s" % _clock(t, tz), meta, max_chars)

    if intent == "sunrise":
        t, flag = _next_event(now, tz, lat, lon, "sunrise")
        if t is None:
            meta["refused"] = flag
            return _bounded(_no_event_text("sunrise", flag), meta, max_chars)
        meta["event"], meta["tomorrow"] = "sunrise", flag
        return _bounded("%sSunrise %s" % ("Tomorrow " if flag else "", _clock(t, tz)),
                        meta, max_chars)

    if intent in ("sunset", "twilight"):
        key = "sunset" if intent == "sunset" else "civil_dusk"
        t, flag = _next_event(now, tz, lat, lon, key)
        if t is None:
            meta["refused"] = flag
            return _bounded(_no_event_text(key, flag), meta, max_chars)
        meta["event"], meta["tomorrow"] = key, flag
        label = "Sunset" if intent == "sunset" else "Civil twilight ends"
        return _bounded("%s%s %s" % ("Tomorrow " if flag else "", label, _clock(t, tz)),
                        meta, max_chars)

    # intent == "dark": the field question. Sunset alone under-answers it — there is usable light
    # for roughly half an hour after — so the pair is the honest answer and it still fits.
    ss, f1 = _next_event(now, tz, lat, lon, "sunset")
    cd, f2 = _next_event(now, tz, lat, lon, "civil_dusk")
    # The two events are refused INDEPENDENTLY, and a missing civil dusk must not be described as
    # a missing sunset. Above ~61N in midsummer the sun sets perfectly normally and simply never
    # reaches -6 degrees; the old code answered "Sun stays up here today" while the sunset branch
    # of the same module answered "Sunset 9:54 PM" — two flatly contradictory published claims,
    # one line apart. Report the event that is actually missing.
    if cd is None and ss is not None:
        meta["refused"], meta["event"] = f2, "sunset"
        return _bounded("Sunset %s, no full dark tonight" % _clock(ss, tz), meta, max_chars)
    if ss is None:
        meta["refused"] = f1
        return _bounded(_no_event_text("sunset", f1), meta, max_chars)
    # Pair the SAME day. _next_event rolls each event independently, so in the ~30 min window
    # between sunset and civil dusk it would return tomorrow's sunset beside today's dusk and
    # label the pair "Tomorrow" — measured up to 6 minutes wrong at high latitude.
    if f1 != f2:
        day = (now.astimezone(tz).date() + timedelta(days=1)) if f1 else now.astimezone(tz).date()
        cd2 = event_utc(day, lat, lon, event_altitude("civil_dusk"), "set")
        if cd2 is None:
            meta["refused"], meta["event"] = "always_above", "sunset"
            return _bounded("%sSunset %s, no full dark" % ("Tomorrow " if f1 else "",
                                                           _clock(ss, tz)), meta, max_chars)
        cd = cd2
    meta["event"], meta["tomorrow"] = "sunset+civil_dusk", bool(f1)
    return _bounded("%sSunset %s, dark %s" % ("Tomorrow " if f1 else "",
                                              _clock(ss, tz), _clock(cd, tz)), meta, max_chars)
