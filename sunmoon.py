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
    ("sunrise", re.compile(r"\b(sunrise|sun\s*(?:is\s*)?ris(?:e|es|ing)|first\s*light|dawn|daybreak|"
                           r"sun\s*(?:come|comes|coming)\s*up|sun\s*up\b)", re.I)),
    ("dark", re.compile(r"\b(get\s*dark|gets\s*dark|getting\s*dark|dark\s*out|"
                        r"night\s*fall|last\s*light|till\s*dark|until\s*dark|"
                        r"dark\s*by|is\s*it\s*dark|when\s*is\s*it\s*dark|"
                        r"how\s*much\s*light|light\s*left|dark\s*yet|dusk)\b", re.I)),
    ("sunset", re.compile(r"\b(sunset|sun\s*(?:is\s*)?set(?:s|ting)?|sundown|"
                          r"sun\s*(?:go|goes|going)\s*down|golden\s*hour)\b", re.I)),
    ("twilight", re.compile(r"\b(twilight|daylight)\b", re.I)),
)

# Unambiguous sun wording: claims on its own.
_SUN_STRONG = re.compile("|".join("(?:%s)" % rx.pattern for name, rx in _INTENTS
                                  if name != "twilight"), re.I)
# Ambiguous wording: a real word in ordinary traffic too, so it needs corroboration. "dawn" is a
# name, "twilight" and "dusk" are nouns people use about other things. Mirrors weather's
# strong/weak split for exactly the same reason.
_WEAK_TOKENS = ("dawn", "dusk", "twilight", "daylight")
_SUN_WEAK = re.compile(r"\b(" + "|".join(_WEAK_TOKENS) + r")\b", re.I)
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
_MOON_RISESET = re.compile(r"\b(moon\s*rise|moonrise|moon\s*set|moonset|moon\s*out)\b|"
                           r"\bmoon\b[^.?!]{0,20}\b(rise|rises|set|sets|up|down|out)\b", re.I)

# STRUCTURAL GUARD, and it exists because the obvious fix created the opposite bug.
#
# Deriving _SUN_STRONG from _INTENTS guaranteed the front door could never be NARROWER than the
# intent table. Then a hand-maintained _SUN_WEAK was layered on top, containing "dawn" and "dusk"
# — words that were, at that moment, in no _INTENTS pattern at all. So the door became WIDER than
# the table, and the widening fell straight into the intent resolver's default. "cal when is
# dawn?" was answered "Sunset 8:12 PM, dark 8:40 PM": a morning question given an evening time,
# as a fixed Python-authored reply, with no model to blame.
#
# A comment would not have caught that. This does: every weak token must resolve to a real intent,
# checked at import, so the module refuses to load rather than answering the wrong half of the day.
#
# And it iterates _WEAK_TOKENS, not a literal copy of it. The first version listed the four words
# inline, so adding a fifth weak token sailed past the very guard written to catch exactly that —
# a guard that does not read the list it guards is decoration.
# Any ask about a day that is not today. This module computes for NOW only, so answering one with
# today's time is the same confident wrongness weather refuses forecasts to avoid.
#
# It is a SECOND temporal table beside weather._FORECAST and they diverged immediately: weather
# already knew "this weekend" and "next week" while this missed them, along with bare weekday names
# ("sunset saturday") and relative offsets ("in 3 days"), each of which was answered with today's
# time. The eval now cross-checks this against every day-shifting phrase weather knows, so the two
# cannot drift apart silently again — the divergence, not the individual misses, is the defect.
_DAYNAME_FULL = r"monday|tuesday|wednesday|thursday|friday|saturday|sunday"
_DAYNAME = _DAYNAME_FULL + r"|mon|tue|tues|wed|thu|thur|thurs|fri"   # 'sat'/'sun' never bare
_MONTH = (r"january|february|march|april|may|june|july|august|september|october|november|december|"
          r"jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec")
# A Maidenhead locator (4/6/8) or a decimal coordinate pair anywhere in the message.
_NAMED_PLACE = re.compile(r"\b[A-R]{2}[0-9]{2}(?:[A-X]{2}(?:[0-9]{2})?)?\b"
                          r"|(?<![\d.])-?\d{1,2}\.\d+\s*[, ]\s*-?\d{1,3}\.\d+", re.I)
_OTHER_DAY = re.compile(
    r"\b(?:tomorrow|tmrw|tmw|yesterday|"
    r"(?:next|last|this)\s+(?:day|week|month|year|weekend|" + _DAYNAME_FULL + r")|"
    r"last\s+night|"
    # _DAYNAME here, which is full names PLUS the abbreviations that do not collide. The
    # dangerous two are already absent from it: "this sun is brutal" and "on sat we ride" were
    # both refused as other-day asks, and "this sun" sits inside the most common phrasing this
    # capability has. "this wed" is a genuine day shift and stays covered.
    r"(?:on|this|next|last)\s+(?:" + _DAYNAME + r")|"
    # A bare weekday name anywhere is a day shift ("sunset saturday?"). FULL names only: the
    # abbreviations collide badly on this of all paths — "sun" is inside no word boundary of
    # "sunset" but IS a bare word in "when does the sun go down", and "mar"/"may"/"sat" collide
    # with months and ordinary verbs.
    r"monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
    r"in\s+\d+\s+(?:day|days|week|weeks|month|months)|"
    r"in\s+a\s+(?:day|week|month)|"
    r"\d+\s+days?\s+(?:from\s+now|out|ahead)|"
    r"on\s+(?:christmas|new\s*years?|thanksgiving|halloween|easter|the\s+\d{1,2}(?:st|nd|rd|th))|"
    r"in\s+(?:" + _MONTH + r")|"
    r"(?:" + _MONTH + r")\s+\d{1,2}"
    # A bare N/N date form is deliberately ABSENT. "50/50 chance of rain", "3/4 throttle" and
    # "1/2 mile" are ordinary mesh traffic and were all refused as other-day asks; a numeric date
    # with no other cue is rare by comparison, and calc owns fractions. Month names, weekday
    # names, "on the 4th" and relative offsets carry the real cases.
    r")\b",
    re.I)


def _resolve_intent(text):
    return next((n for n, rx in _INTENTS if rx.search(text)), None)


for _tok in _WEAK_TOKENS:                       # NOT a hardcoded copy — see below
    if _resolve_intent(_tok) is None:
        raise AssertionError(
            "sunmoon: weak trigger %r matches no _INTENTS pattern, so it would fall through to "
            "the resolver's default and be answered as the wrong time of day" % _tok)
del _tok


def explain_match(text):
    """Why the text did or did not read as a sun/moon query, in a form safe to publish."""
    t = text or ""
    q = "?" in t
    # Derived from _INTENTS, then MINUS the weak tokens. "dawn" must live in _INTENTS so it
    # resolves to the sunrise branch rather than the resolver's default — but it is also a common
    # given name, so it must not claim on its own. Both properties are needed and they pull in
    # opposite directions; subtracting here keeps the derivation while preserving the weak rule.
    sun_s = sorted({m.group(0).lower().strip() for m in _SUN_STRONG.finditer(t)}
                   - set(_WEAK_TOKENS))
    sun_w = sorted({m.group(0).lower() for m in _SUN_WEAK.finditer(t)})
    moon_s = sorted({m.group(0).lower() for m in _MOON_STRONG.finditer(t)})
    moon_w = sorted({m.group(0).lower() for m in _MOON_WEAK.finditer(t)})
    riseset = bool(_MOON_RISESET.search(t))
    sun = sorted(set(sun_s) | set(sun_w))
    moon = sorted(set(moon_s) | set(moon_w))
    # The exclusion list vetoes only the WEAK layer, and only when nothing unambiguous is present.
    # It used to run first and veto the whole message, so "does it get dark earlier after daylight
    # savings?" and "moon landing anniversary and when is sunset?" fell through to the model even
    # though each carries a strong token — defeating the very purpose of recognising an ask so it
    # can be answered or refused. An exclusion should suppress ambiguity, never override certainty.
    excluded = bool(_NOT_SKY.search(t))
    if excluded and riseset and not (sun_s or moon_s):
        # _MOON_RISESET's proximity alternative fires on any rise/set/up/down/out word within 20
        # chars of "moon", which every _NOT_SKY moon collocation can satisfy — "grab a moon pie on
        # the way down" was refused as a moon rise/set ask. An exclusion that the pattern it
        # excludes can bypass is not an exclusion.
        riseset = False
    if excluded and not (sun_s or moon_s or riseset):
        return {"sun": [], "moon": [], "via": None, "moon_riseset": False, "excluded": True}
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
            "excluded": excluded}


def mention_positions(text):
    """Character offsets of every sun/moon word in the text, ascending.

    Counterpart to weather.mention_positions. Deduped by span because the trigger regexes overlap
    by design ("dusk" is both a derived strong token and a weak one) — and it enumerates EVERY
    regex explain_match consults, which the previous mechanism did not: it listed four of five and
    silently dropped `moon`, so 216 of 294 genuine moon asks were judged to have no mentions at
    all and were handed away.
    """
    t = text or ""
    pos = set()
    for rx in (_SUN_STRONG, _SUN_WEAK, _MOON_STRONG, _MOON_WEAK, _MOON_RISESET):
        for m in rx.finditer(t):
            pos.add(m.start())
    return sorted(pos)


# A time interrogative directly governing a sun/moon word settles it outright, regardless of what
# else the sentence mentions first: "rain later, when is sunset" is a sunset question with a
# weather clause in front of it.
_TIME_ASK = re.compile(r"\b(?:when|what\s+time|how\s+long|what's\s+the\s+time|whens?)\b", re.I)


def governed_by_time_ask(text, window=28):
    """True if a sun/moon word follows a time interrogative closely enough to be its object."""
    t = text or ""
    asks = [m.end() for m in _TIME_ASK.finditer(t)]
    if not asks:
        return False
    return any(0 <= p - a <= window for a in asks for p in mention_positions(t))


def wants_sunmoon(text):
    return explain_match(text)["via"] is not None


def _bounded(reply, meta, max_chars):
    """Enforce the length bound on EVERY branch, not just the longest one.

    The bound was originally checked on one branch of seven. Nothing overflowed at the default,
    so the constraint held by accident rather than by construction — which is the state a
    capability is in right before someone adds a longer string.
    """
    if reply is not None and len(reply) > max_chars:
        # REFUSE, do not abstain. Returning None made the responder read this as "the capability
        # declined", which hands the question to the language model — so a length bound on a
        # compute doer became a route to an INVENTED time. Measured: SUNMOON_MAX_CHARS=25, a
        # plausible operator setting for airtime, turned every sun answer into a model answer.
        meta["refused"] = "too long"
        # The refusal has to fit the bound it is enforcing. The old string was 29 chars and was
        # emitted at max_chars=5, 10 and 25 — including 25, the exact setting this function's own
        # docstring names as the motivating case. A bound that its own violation message breaks is
        # not a bound. Below any plausible floor it degrades rather than lying.
        for cand in ("Answer too long for this link", "Answer too long", "Too long", "long", "-"):
            if len(cand) <= max_chars:
                return cand, meta
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

    Scans a WINDOW of solar days around today and returns the EARLIEST event still ahead, rather
    than walking forward from today and stopping at the first hit. Two bugs came from the walk:
    `today` is a LOCAL date while event_with_reason computes around the solar noon of a UTC date,
    so where the zone offset is far from the longitude the two disagree and a nearer event sits on
    the day BEFORE the one the loop starts at (measured: whole-day-late answers on the far side of
    the dateline, and an Anchorage case where the next civil dusk was 17 minutes away and the
    answer given was 24 hours later). Starting at -1 and taking the minimum removes the ordering
    assumption entirely.
    """
    today = now.astimezone(tz).date()
    alt, direction = event_altitude(key), _EVENTS[key][1]
    best, reason = None, None
    for offset in (-1, 0, 1, 2):
        t, r = event_with_reason(today + timedelta(days=offset), lat, lon, alt, direction)
        if t is None:
            reason = reason or r
            continue
        if t > now and (best is None or t < best):
            best = t
    if best is None:
        return None, (reason or "not_found")
    return best, (best.astimezone(tz).date() != today)


def _following(after, tz, lat, lon, key, max_hours=12):
    """The first occurrence of `key` strictly after a given instant, or (None, reason).

    This is how the sunset/dusk pair is kept coherent, and it is deliberately NOT expressed as
    "the same day". Civil dusk falls after local midnight at high latitude, so a same-day test is
    a date-bookkeeping question with a different answer depending on which calendar you mean —
    which is exactly how the pairing broke: one fix redefined the is_tomorrow flag from loop index
    to local-date comparison, and the guard in the OTHER fix was keyed on those flags being
    unequal, so it silently stopped firing. 461 mispaired replies followed, worst case 24.2 hours
    off, all above about 59N. "The dusk that follows THIS sunset" needs no calendar at all.
    """
    base = after.astimezone(tz).date()
    alt, direction = event_altitude(key), _EVENTS[key][1]
    reason = None
    for offset in (-1, 0, 1, 2):
        t, r = event_with_reason(base + timedelta(days=offset), lat, lon, alt, direction)
        if t is None:
            # KEEP SCANNING. Bailing on the first eventless day was wrong: near the edge of the
            # midnight-sun band a location has civil dusk on some days and not others, so the
            # window opens on a day without one and the day that HAS one is never reached.
            # Measured at 731 cases across 8 zones — every one reported "no full dark" for a night
            # that genuinely gets dark. A refusal is only honest once the whole window is empty.
            reason = reason or r
            continue
        if t > after:
            # It must belong to the SAME night. A dusk two days later is a real event and a
            # dishonest answer: at the edge of the midnight-sun band the night after a given
            # sunset can have no civil dusk while a later one does, and pairing across that gap
            # would tell someone it gets dark tonight when it does not.
            if (t - after).total_seconds() <= max_hours * 3600:
                return t, None
            return None, (reason or "always_above")
    return None, (reason or "not_found")


def answer(text, lat, lon, tz, now, max_chars=160):
    """(reply, meta). reply is None when this capability declines or must refuse.

    Python formats the whole string. Nothing here is passed to a model, and no coordinate ever
    appears in the output — the location is an input, never a fact we report.
    """
    meta = {"intent": None, "refused": None, "event": None, "tomorrow": False}
    m = explain_match(text)
    if not m["via"]:
        return None, meta

    # A NAMED LOCATION IS REFUSED, not silently ignored. This capability computes for the
    # operator's own configured point and deliberately never takes a position from the message
    # (see the module header). So "sunrise at grid EM28" answered with the local sunrise would be
    # a confidently wrong answer to a question about somewhere else — the asker supplied a
    # location precisely because they meant it.
    if _NAMED_PLACE.search(text):
        meta["intent"], meta["refused"] = "elsewhere", "other location"
        return _bounded("I only compute for my own location", meta, max_chars)

    # Epoch bound. The solar model is accurate to about a minute for two centuries either side of
    # J2000, and the lunar series is stated valid 1900-2100. Outside that it does not fail — it
    # DRIFTS, silently, still returning a plausible time. A capability whose whole claim is that
    # Python owns the digits cannot serve a number it has no accuracy claim for.
    # A date-qualified ask cannot be served: this capability computes for NOW only, and answering
    # "sunset tomorrow" with today's time is the same confident wrongness the weather capability
    # refuses forecasts to avoid. Weather's own temporal machinery sits in the same process and
    # was never consulted here.
    if _OTHER_DAY.search(text):
        meta["intent"], meta["refused"] = "other_day", "other day"
        return _bounded("I only have today's times", meta, max_chars)

    if not (1901 <= now.year <= 2099):
        meta["refused"] = "out of epoch"
        return _bounded("Date outside my accurate range", meta, max_chars)

    if m["via"] == "moon_riseset":
        # Recognised so it can be refused HONESTLY. Left unrecognised it would fall through to the
        # general model, which would produce a confident time for something we do not compute.
        meta["intent"], meta["refused"] = "moon_riseset", "not built"
        return _bounded("Moon rise/set not built yet, phase only", meta, max_chars)

    if m["via"] == "moon":
        k, name, waxing = moon_phase(now)
        meta["intent"] = "moon"
        return _bounded("Moon %d%%, %s" % (round(k * 100), name), meta, max_chars)

    # FAIL CLOSED on an unresolved intent rather than defaulting to an arbitrary branch. The
    # import-time guard makes this unreachable for the shipped table, and that is the point: if a
    # trigger word is ever added without an intent, this refuses instead of answering the wrong
    # half of the day, which is exactly what a silent "dark" default did to "cal when is dawn?".
    intent = _resolve_intent(text)
    if intent is None:
        meta["intent"], meta["refused"] = None, "unresolved intent"
        return _bounded("Not sure which time you mean", meta, max_chars)
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
    if ss is None:
        meta["refused"] = f1
        return _bounded(_no_event_text("sunset", f1), meta, max_chars)
    # The dusk that FOLLOWS this sunset, not "today's" dusk. See _following.
    cd, r2 = _following(ss, tz, lat, lon, "civil_dusk")
    if cd is None:
        # The sun sets normally but never reaches -6: real above ~61N in midsummer. Reporting this
        # as a missing SUNSET is a published falsehood, and the reply must still carry the
        # Tomorrow qualifier when the sunset it quotes is tomorrow's — the branch added for this
        # case originally dropped it.
        meta["refused"], meta["event"], meta["tomorrow"] = r2, "sunset", f1
        return _bounded("%sSunset %s, no full dark" % ("Tomorrow " if f1 else "", _clock(ss, tz)),
                        meta, max_chars)
    meta["event"], meta["tomorrow"] = "sunset+civil_dusk", bool(f1)
    return _bounded("%sSunset %s, dark %s" % ("Tomorrow " if f1 else "",
                                              _clock(ss, tz), _clock(cd, tz)), meta, max_chars)
