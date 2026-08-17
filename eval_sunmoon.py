#!/usr/bin/env python3
"""Offline eval for the sun/moon COMPUTE doer (sunmoon.py).

Loaded BY EXPLICIT PATH — eval_dm imported the DEPLOYED dashboard once and three mutations
"passed" against production code. Never trust sys.path here.

COORDINATES: every coordinate in the CHECKS is a placeholder (39.0, -95.0 and friends). The §7
accuracy vectors are the one exception and cannot be — an absolute published time is only
meaningful at the location it was published for; they use the Olathe city centroid, which the
dashboard already discloses by naming KOJC on every weather reply. The DEPLOYED observer point is
a different value and lives only in the gitignored config. Verify that before pushing: a reviewer
flagged that the incentive to take vectors AT the real point is exactly what would leak it.

Two kinds of check:
  - ABSOLUTE, against authoritative published times (§7). These pin accuracy.
  - STRUCTURAL, which need no external source and catch whole classes of error at once —
    symmetry about solar noon, event ordering, hemisphere inversion, synodic period. An
    implementation can match one published sunrise by luck; it cannot satisfy these by luck.

Run:  python3 eval_sunmoon.py
      python3 eval_sunmoon.py --self-test    (negative controls; each mutation MUST be caught)
"""
import importlib.util
import math
import os
import sys
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))


def _load(name, fn):
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, fn))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# Load the module under test and REGISTER IT UNDER ITS REAL NAME before responder.py is imported.
# responder.py does sys.path.insert(0, "~/cal-mesh") and then `import sunmoon`, so loading it by
# path alone is not enough: the responder binds to the DEPLOYED file and every end-to-end check
# silently runs against production code. Proven by an adversarial review, which sabotaged only the
# sandbox copy and watched all 11 end-to-end checks keep passing. This is the identical failure
# eval_dm hit one layer down in session 120 — hence the warning in this file's own docstring,
# which was not sufficient to prevent it recurring. sys.modules is checked before sys.path, so
# seeding it is what actually binds the responder to the file under test.
S = _load("sunmoon", "sunmoon.py")
sys.modules["sunmoon"] = S
R = _load("responder_under_test", "responder.py")
assert R.sunmoon is S, "responder bound a different sunmoon module than the one under test"

# placeholder observer — NOT the deployed point
LAT, LON = 39.0, -95.0
TZ = ZoneInfo("America/Chicago")

passed = 0
failures = []


def check(name, cond, detail=""):
    global passed
    if cond:
        passed += 1
    else:
        failures.append(f"{name}{(' [' + str(detail) + ']') if detail else ''}")


def mins(a, b):
    return abs((a - b).total_seconds()) / 60.0


def run():
    global passed, failures
    passed, failures = 0, []

    # ---- 1. event ORDERING. One assertion that catches a whole class of sign/branch errors ----
    for d in (date(2026, 3, 20), date(2026, 6, 21), date(2026, 9, 22), date(2026, 12, 21)):
        e = S.sun_events(d, LAT, LON)
        seq = ["astronomical_dawn", "nautical_dawn", "civil_dawn", "sunrise",
               "solar_noon", "sunset", "civil_dusk", "nautical_dusk", "astronomical_dusk"]
        ts = [e[k] for k in seq]
        check(f"events strictly ordered {d}", all(a is not None for a in ts)
              and all(ts[i] < ts[i + 1] for i in range(len(ts) - 1)),
              [k for k, v in zip(seq, ts) if v is None])

    # ---- 2. SYMMETRY about solar noon. Rise and set are equidistant from transit. -------------
    #      A one-sided sign error in the hour angle passes every "does it look like 8pm" check
    #      and dies here.
    for d in (date(2026, 1, 15), date(2026, 6, 21), date(2026, 11, 3)):
        e = S.sun_events(d, LAT, LON)
        check(f"rise/set symmetric about noon {d}",
              abs(mins(e["solar_noon"], e["sunrise"]) - mins(e["sunset"], e["solar_noon"])) < 1.0,
              mins(e["solar_noon"], e["sunrise"]) - mins(e["sunset"], e["solar_noon"]))
        for dawn, dusk in (("civil_dawn", "civil_dusk"), ("nautical_dawn", "nautical_dusk"),
                           ("astronomical_dawn", "astronomical_dusk")):
            check(f"{dawn}/{dusk} symmetric {d}",
                  abs(mins(e["solar_noon"], e[dawn]) - mins(e[dusk], e["solar_noon"])) < 1.0)

    # ---- 3. EQUINOX ~12h everywhere; refraction makes it slightly MORE, never less ------------
    #      The lower bound is 12.05, not 12.0, ON PURPOSE. Day and night are equal only for a
    #      point sun with no atmosphere; the -0.833 correction buys ~7 extra minutes at the
    #      equator and more with latitude. A check that merely allowed ">= 12.0" would pass with
    #      the refraction correction deleted, which is the single most likely constant to get
    #      wrong here — a mutation proved exactly that.
    for lat in (0.0, 23.5, 39.0, 51.5, -33.9):
        e = S.sun_events(date(2026, 3, 20), lat, 0.0)
        day_h = (e["sunset"] - e["sunrise"]).total_seconds() / 3600.0
        check(f"equinox slightly OVER 12h at lat {lat}", 12.05 <= day_h <= 12.35, round(day_h, 3))

    # ---- 3b. TWILIGHT DURATIONS pin the depression angles themselves --------------------------
    #      Ordering (check 1) survives any angle that is merely monotonic, so -5 for civil passed
    #      until this existed. Durations at a known latitude are the thing that actually pins them.
    e = S.sun_events(date(2026, 3, 20), 39.0, 0.0)
    civil = mins(e["civil_dusk"], e["sunset"])
    naut = mins(e["nautical_dusk"], e["civil_dusk"])
    astro = mins(e["astronomical_dusk"], e["nautical_dusk"])
    check("civil twilight ~30 min at 39N equinox", 26.0 <= civil <= 34.0, round(civil, 1))
    check("nautical band ~30 min at 39N equinox", 27.0 <= naut <= 36.0, round(naut, 1))
    check("astronomical band ~30 min at 39N equinox", 27.0 <= astro <= 38.0, round(astro, 1))
    check("twilight bands widen toward the poles",
          mins(S.sun_events(date(2026, 3, 20), 60.0, 0.0)["civil_dusk"],
               S.sun_events(date(2026, 3, 20), 60.0, 0.0)["sunset"]) > civil * 1.3)

    # ---- 4. SOLSTICE extremes and HEMISPHERE INVERSION ----------------------------------------
    def daylen(d, lat):
        e = S.sun_events(d, lat, 0.0)
        return (e["sunset"] - e["sunrise"]).total_seconds() / 3600.0

    jun_n, dec_n = daylen(date(2026, 6, 21), 39.0), daylen(date(2026, 12, 21), 39.0)
    jun_s, dec_s = daylen(date(2026, 6, 21), -39.0), daylen(date(2026, 12, 21), -39.0)
    check("N hemisphere: June longest", jun_n > 14.0 and dec_n < 10.0, (jun_n, dec_n))
    check("S hemisphere inverts", dec_s > 14.0 and jun_s < 10.0, (jun_s, dec_s))
    check("hemispheres mirror each other", abs(jun_n - dec_s) < 0.2 and abs(dec_n - jun_s) < 0.2)
    check("equator ~12h year round",
          all(11.8 < daylen(date(2026, m, 15), 0.0) < 12.4 for m in range(1, 13)))

    # ---- 5. PHYSICAL BOUNDS on the intermediate quantities ------------------------------------
    decls, eots = [], []
    for n in range(0, 366, 7):
        d = date(2026, 1, 1) + timedelta(days=n)
        jc = (S._jday(d) - 2451545.0 + 0.5) / 36525.0
        dec, eot = S._solar(jc)
        decls.append(dec)
        eots.append(eot)
    check("declination within obliquity", max(abs(x) for x in decls) <= 23.45, max(decls))
    check("declination reaches both tropics", max(decls) > 23.0 and min(decls) < -23.0)
    check("equation of time within +-17 min", max(abs(x) for x in eots) < 17.0, max(eots))
    check("equation of time changes sign", max(eots) > 10 and min(eots) < -10)

    # ---- 6. REFUSALS — the discipline. Never clamp, never invent. -----------------------------
    check("polar night: no sunrise", S.event_utc(date(2026, 12, 21), 78.2, 15.6,
                                                 S.ALT_SUNRISE, "rise") is None)
    check("midnight sun: no sunset", S.event_utc(date(2026, 6, 21), 78.2, 15.6,
                                                 S.ALT_SUNRISE, "set") is None)
    check("polar night reason is always_below",
          S.no_event_reason(date(2026, 12, 21), 78.2, S.ALT_SUNRISE) == "always_below")
    check("midnight sun reason is always_above",
          S.no_event_reason(date(2026, 6, 21), 78.2, S.ALT_SUNRISE) == "always_above")
    # the subtle one: at 61N in midsummer the sun never reaches -18, so astronomical twilight
    # never begins even though the sun does rise and set normally.
    check("no astronomical twilight at 61N midsummer",
          S.no_event_reason(date(2026, 6, 21), 61.2, S.ALT_ASTRONOMICAL) == "always_above")
    check("ordinary latitude has a reason of None",
          S.no_event_reason(date(2026, 8, 17), LAT, S.ALT_SUNRISE) is None)
    check("exact pole does not raise", S.event_utc(date(2026, 3, 20), 90.0, 0.0,
                                                   S.ALT_SUNRISE, "rise") is None)

    # ---- 6b. THE TWILIGHT LIMIT IS INDEPENDENT OF SUNRISE, IN BOTH DIRECTIONS ----------------
    #      The tempting shortcut — "no sunrise, so skip twilight" — is wrong at BOTH ends, and
    #      each direction has a real published counterexample. Verified against USNO.
    #      Fairbanks 2026-06-21: the sun rises (02:58) AND sets (00:48), yet civil twilight
    #      never begins or ends, because the sun never gets 6 deg down. USNO: "Object
    #      continuously above the Twilight Limit".
    fb = S.sun_events(date(2026, 6, 21), 64.84, -147.72)
    check("Fairbanks midsummer: sun still rises", fb["sunrise"] is not None)
    check("Fairbanks midsummer: sun still sets", fb["sunset"] is not None)
    check("Fairbanks midsummer: NO civil twilight",
          fb["civil_dawn"] is None and fb["civil_dusk"] is None)
    check("Fairbanks midsummer: reason is always_above",
          S.no_event_reason(date(2026, 6, 21), 64.84, S.ALT_CIVIL) == "always_above")
    #      Utqiagvik 2026-12-21 is the mirror: NO sunrise at all, but civil twilight does occur
    #      (USNO publishes 11:56-14:55). A "no sunrise implies dark all day" shortcut says the
    #      opposite of the truth here.
    uq = S.sun_events(date(2026, 12, 21), 71.29, -156.79)
    check("Utqiagvik midwinter: no sunrise", uq["sunrise"] is None)
    check("Utqiagvik midwinter: civil twilight DOES occur",
          uq["civil_dawn"] is not None and uq["civil_dusk"] is not None)
    AK = ZoneInfo("America/Anchorage")
    check("Utqiagvik midwinter: civil dawn matches USNO 11:56",
          mins(uq["civil_dawn"], datetime(2026, 12, 21, 11, 56, tzinfo=AK)) <= 1.5)
    check("Utqiagvik midwinter: civil dusk matches USNO 14:55",
          mins(uq["civil_dusk"], datetime(2026, 12, 21, 14, 55, tzinfo=AK)) <= 1.5)

    # ---- 6c. EPOCH BOUNDS: the model drifts outside its validity window, it does not fail -----
    now_utc = datetime(2026, 8, 17, 14, 30, tzinfo=timezone.utc)
    for yr in (1899, 1900, 2101, 2200):
        r, m = S.answer("cal sunset", LAT, LON, TZ, now_utc.replace(year=yr))
        check(f"out-of-epoch {yr} refused", m["refused"] == "out of epoch" and r
              and not any(ch.isdigit() for ch in r), (r, m))
    for yr in (1901, 2026, 2099):
        _, m = S.answer("cal sunset", LAT, LON, TZ, now_utc.replace(year=yr))
        check(f"in-epoch {yr} answered", m["refused"] is None, m)

    # ---- 7. ABSOLUTE ACCURACY against authoritative published times ---------------------------
    # Filled from a sourced table; see VECTORS below. Each entry pins one computed event against
    # a published value to the minute.
    for (d, lat, lon, tzname, key, hh, mm, src) in VECTORS:
        got = S.sun_events(d, lat, lon)[key]
        want = datetime(d.year, d.month, d.day, hh, mm, tzinfo=ZoneInfo(tzname))
        check(f"vector {src} {d} {key}", got is not None and mins(got, want) <= 1.5,
              None if got is None else round(mins(got, want), 2))

    # ---- 8. DST transition days: the conversion, not the astronomy, is what breaks ------------
    for d in (date(2026, 3, 8), date(2026, 11, 1)):          # US DST start / end
        e = S.sun_events(d, LAT, LON)
        prev = S.sun_events(d - timedelta(days=1), LAT, LON)
        # Consecutive days are ~1440 min apart in UTC; the drift is a couple of minutes. A DST
        # bug leaking into the astronomy shows up as ~60 min of extra gap.
        check(f"no 1h UTC jump across DST {d}",
              abs(mins(e["sunrise"], prev["sunrise"]) - 1440.0) < 10.0,
              mins(e["sunrise"], prev["sunrise"]) - 1440.0)
        # and the LOCAL wall clock must jump, because that is what DST is
    loc_before = S.sun_events(date(2026, 3, 7), LAT, LON)["sunrise"].astimezone(TZ).hour
    loc_after = S.sun_events(date(2026, 3, 8), LAT, LON)["sunrise"].astimezone(TZ).hour
    check("local sunrise hour shifts across DST start", loc_after == loc_before + 1,
          (loc_before, loc_after))

    # ---- 9. CONTINUITY: adjacent days never jump. Catches epoch/rollover errors ---------------
    prev = None
    worst = 0.0
    for n in range(0, 365):
        d = date(2026, 1, 1) + timedelta(days=n)
        t = S.sun_events(d, LAT, LON)["sunset"]
        if prev is not None:
            delta = abs((t - prev).total_seconds() / 60.0 - 1440.0)
            worst = max(worst, delta)
        prev = t
    check("sunset moves <3 min/day all year", worst < 3.0, round(worst, 2))

    # ---- 10. MOON --------------------------------------------------------------------------
    for n in range(0, 60, 3):
        when = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(days=n)
        k, name, waxing = S.moon_phase(when)
        check(f"illumination in range {when:%Y-%m-%d}", 0.0 <= k <= 1.0, k)
        check(f"phase name non-empty {when:%Y-%m-%d}", bool(name))
    # synodic period: the phase must repeat after 29.53 days
    a = S.moon_phase(datetime(2026, 5, 1, tzinfo=timezone.utc))[0]
    b = S.moon_phase(datetime(2026, 5, 1, tzinfo=timezone.utc) + timedelta(days=29.53))[0]
    check("illumination repeats over a synodic month", abs(a - b) < 0.05, (a, b))
    # a full cycle must actually visit both extremes
    ks = [S.moon_phase(datetime(2026, 5, 1, tzinfo=timezone.utc)
                       + timedelta(days=n))[0] for n in range(30)]
    check("cycle reaches near-new", min(ks) < 0.05, min(ks))
    check("cycle reaches near-full", max(ks) > 0.95, max(ks))
    # ILLUMINATION MUST AGREE WITH THE PHASE NAME. They come from two different expressions —
    # k from the phase angle, the name from the mean elongation — so they can genuinely disagree,
    # and nothing above would notice: an inverted k stays in [0,1], still repeats over a synodic
    # month, and still visits both extremes. A mutation proved that gap. This closes it.
    for n in range(0, 120):
        when = datetime(2026, 3, 1, tzinfo=timezone.utc) + timedelta(days=n)
        k, name, _ = S.moon_phase(when)
        if name == "full moon":
            check(f"full moon is lit d{n}", k > 0.94, k)
        if name == "new moon":
            check(f"new moon is dark d{n}", k < 0.06, k)
        if name in ("first quarter", "last quarter"):
            check(f"quarter is half lit d{n}", 0.35 < k < 0.65, k)
        if "crescent" in name:
            check(f"crescent is under half d{n}", k < 0.55, k)
        if "gibbous" in name:
            check(f"gibbous is over half d{n}", k > 0.45, k)
    # waxing/waning must flip exactly once per half cycle, and never contradict the name
    for n in range(60):
        when = datetime(2026, 5, 1, tzinfo=timezone.utc) + timedelta(days=n)
        _, name, waxing = S.moon_phase(when)
        if "waxing" in name:
            check(f"waxing name agrees with flag d{n}", waxing is True)
        if "waning" in name:
            check(f"waning name agrees with flag d{n}", waxing is False)

    # ---- 11. INTENT: selection only, and no collision with the other capabilities -------------
    for t in ("cal when does it get dark", "cal whats sunset", "cal sunrise?", "cal moon phase",
              "when is first light", "cal solar noon", "cal is it a full moon", "cal dusk?",
              "cal what time does the sun go down", "cal when does the sun come up",
              "cal when is the sun highest", "cal how long till dark", "cal is it dark yet",
              "cal when does night fall", "cal how much light is left", "cal will it be dark by 8",
              "cal solar zenith", "cal moonrise", "cal when does the moon rise",
              "cal moonset tonight"):
        check(f"claims: {t[:30]}", S.wants_sunmoon(t))
    for t in ("cal whats the temperature", "cal 12*12", "cal hows the weather", "cal 2+2",
              "cal whats the wind", "cal .5 mi in km", "cal hows the radio", "good morning",
              "cal Dawn Smith node status", "cal call dawn on the radio",
              "cal twilight zone episode", "cal daylight savings time when",
              "cal moon landing year", "cal how far to the moon in miles"):
        check(f"declines: {t[:30]}", not S.wants_sunmoon(t))

    # ---- 12. NO COORDINATE MAY EVER APPEAR IN A REPLY ----------------------------------------
    now = datetime(2026, 8, 17, 14, 30, tzinfo=timezone.utc)
    lat2, lon2 = 39.123456, -95.654321
    for t in ("cal when does it get dark", "cal sunset", "cal sunrise", "cal moon?",
              "cal solar noon", "cal moonrise", "cal twilight?"):
        r, _ = S.answer(t, lat2, lon2, TZ, now)
        body = (r or "")
        leaked = [tok for tok in ("39.1", "95.6", "39.123", "-95", "39,", "latitude", "longitude")
                  if tok in body]
        check(f"no coordinate leak: {t[:26]}", not leaked, (r, leaked))

    # ---- 13. REPLY SHAPE: short, single line, bounded ----------------------------------------
    for t in ("cal when does it get dark", "cal sunset", "cal sunrise", "cal moon phase",
              "cal solar noon", "cal twilight?", "cal moonrise"):
        r, _ = S.answer(t, LAT, LON, TZ, now, max_chars=120)
        check(f"reply present and bounded: {t[:26]}",
              r and len(r) <= 120 and "\n" not in r, (r, len(r or "")))

    # ---- 13b. THE REPLY MUST SAY THE RIGHT TIME ----------------------------------------------
    #      The gap an adversarial review measured: 23 of 46 realistic mutations survived, and the
    #      pattern was exact — EVERY mutation confined to answer()/_clock()/_next_event() lived,
    #      because nothing here asserted what time a reply CONTAINS. §7's vectors exercise
    #      sun_events(), which answer() never calls. Swapping sunrise for sunset, printing dusk
    #      before sunset, rendering UTC instead of local, dropping the noon/midnight `or 12`,
    #      shifting rounding by 30 minutes and never rolling to tomorrow all passed 366 checks.
    def reply_at(text, when_local, lat=LAT, lon=LON, tz=TZ):
        return S.answer(text, lat, lon, tz, when_local.astimezone(timezone.utc))[0] or ""

    noonish = datetime(2026, 8, 17, 9, 30, tzinfo=TZ)     # before every event of the day
    ev = S.sun_events(date(2026, 8, 17), LAT, LON)

    def hhmm(dt):
        lt = (dt + timedelta(seconds=30)).astimezone(TZ)
        return "%d:%02d %s" % (((lt.hour % 12) or 12), lt.minute, "AM" if lt.hour < 12 else "PM")

    check("sunset reply states the sunset time",
          hhmm(ev["sunset"]) in reply_at("cal sunset", noonish), reply_at("cal sunset", noonish))
    predawn = datetime(2026, 8, 17, 4, 0, tzinfo=TZ)   # before sunrise, so no roll to tomorrow
    check("sunrise reply states the SUNRISE time, not sunset",
          hhmm(ev["sunrise"]) in reply_at("cal sunrise?", predawn)
          and hhmm(ev["sunset"]) not in reply_at("cal sunrise?", predawn),
          reply_at("cal sunrise?", predawn))
    check("twilight reply states CIVIL dusk, not nautical",
          hhmm(ev["civil_dusk"]) in reply_at("cal twilight?", noonish)
          and hhmm(ev["nautical_dusk"]) not in reply_at("cal twilight?", noonish),
          reply_at("cal twilight?", noonish))
    check("solar noon reply states the transit time",
          hhmm(ev["solar_noon"]) in reply_at("cal solar noon", noonish))
    dark = reply_at("cal when does it get dark", noonish)
    check("dark reply states BOTH sunset and civil dusk",
          hhmm(ev["sunset"]) in dark and hhmm(ev["civil_dusk"]) in dark, dark)
    check("dark reply orders sunset BEFORE dusk",
          dark.index(hhmm(ev["sunset"])) < dark.index(hhmm(ev["civil_dusk"])), dark)
    # local rendering, not UTC
    check("reply is LOCAL time, not UTC",
          hhmm(ev["sunset"]) in dark
          and ev["sunset"].strftime("%-I:%M %p").upper().lstrip("0") not in dark.upper()
          or ev["sunset"].astimezone(TZ).hour != ev["sunset"].hour, dark)
    # the 12-hour clock boundaries: noon and midnight are where `or 12` and `< 12` break
    for h, expect in ((0, "12:"), (12, "12:")):
        t = datetime(2026, 8, 17, h, 5, tzinfo=TZ)
        check(f"clock renders hour {h} as 12", S._clock(t.astimezone(timezone.utc), TZ)
              .startswith(expect), S._clock(t.astimezone(timezone.utc), TZ))
    check("midnight is AM", S._clock(datetime(2026, 8, 17, 0, 5, tzinfo=TZ), TZ).endswith("AM"))
    check("noon is PM", S._clock(datetime(2026, 8, 17, 12, 5, tzinfo=TZ), TZ).endswith("PM"))
    check("11:59 AM is AM", S._clock(datetime(2026, 8, 17, 11, 59, tzinfo=TZ), TZ).endswith("AM"))
    # rounding, not truncation, and not shifted
    check("rounds up past the half minute",
          S._clock(datetime(2026, 8, 17, 20, 11, 40, tzinfo=TZ), TZ) == "8:12 PM")
    check("rounds down below the half minute",
          S._clock(datetime(2026, 8, 17, 20, 11, 20, tzinfo=TZ), TZ) == "8:11 PM")
    # the tomorrow roll actually rolls, and is labelled
    after_dark = datetime(2026, 8, 17, 23, 30, tzinfo=TZ)
    r_t = reply_at("cal when does it get dark", after_dark)
    check("after dark, rolls to tomorrow", r_t.startswith("Tomorrow"), r_t)
    check("tomorrow's reply states TOMORROW's sunset",
          hhmm(S.sun_events(date(2026, 8, 18), LAT, LON)["sunset"]) in r_t, r_t)
    check("before sunset, no tomorrow label",
          not reply_at("cal sunset", noonish).startswith("Tomorrow"))
    # the pair must come from ONE day: in the window between sunset and civil dusk the naive
    # implementation returned tomorrow's sunset beside today's dusk (measured 6 min wrong at 74N).
    between = ev["sunset"].astimezone(TZ) + timedelta(minutes=5)
    rb = reply_at("cal when does it get dark", between)
    tom = S.sun_events(date(2026, 8, 18), LAT, LON)
    check("sunset/dusk pair is same-day",
          (hhmm(ev["sunset"]) in rb and hhmm(ev["civil_dusk"]) in rb)
          or (hhmm(tom["sunset"]) in rb and hhmm(tom["civil_dusk"]) in rb), rb)

    # ---- 13c. A MISSING TWILIGHT MUST NOT BE DESCRIBED AS A MISSING SUNSET --------------------
    #      Above ~61N in midsummer the sun sets normally and never reaches -6. The module used to
    #      answer "Sun stays up here today" to "when does it get dark" while its own sunset branch
    #      answered "Sunset 9:54 PM" — two contradictory published claims one line apart.
    for lat in (61.5, 62.0, 64.0):
        mid = datetime(2026, 6, 21, 10, 0, tzinfo=TZ)
        e2 = S.sun_events(date(2026, 6, 21), lat, LON)
        if e2["sunset"] is not None and e2["civil_dusk"] is None:
            rd = reply_at("cal when does it get dark", mid, lat=lat)
            check(f"lat {lat}: sun-sets-but-no-dark is honest",
                  "stays up" not in rd and "does not set" not in rd and "dark" in rd.lower(), rd)
            check(f"lat {lat}: and it still gives the sunset", hhmm(e2["sunset"]) in rd, rd)
    # every refusal reason must be a real reason, never None-into-a-guessed-sentence
    for lat in (60.0, 63.0, 66.0, 69.0, 72.0, 78.0):
        for d in (date(2026, 6, 21), date(2026, 12, 21), date(2026, 6, 13)):
            for key in ("sunrise", "sunset", "civil_dusk"):
                t, reason = S.event_with_reason(d, lat, LON, S.event_altitude(key),
                                                S._EVENTS[key][1])
                check(f"reason present when no event {lat} {d} {key}",
                      (t is not None) or (reason in ("always_above", "always_below")),
                      (t, reason))

    # ---- 13d. THE REFUSAL SENTENCE ITSELF, not just the reason code --------------------------
    #      Swapping the two polar sentences survived every check above, because the checks tested
    #      no_event_reason() and never the text a human reads. Midnight sun reported as polar
    #      night is the constraint-2 failure stated as plainly as it can be stated.
    LY = ZoneInfo("Arctic/Longyearbyen")
    mid_summer = datetime(2026, 6, 21, 12, 0, tzinfo=LY)
    mid_winter = datetime(2026, 12, 21, 12, 0, tzinfo=LY)
    r_sum = S.answer("cal sunrise?", 78.22, 15.65, LY, mid_summer.astimezone(timezone.utc))[0]
    r_win = S.answer("cal sunrise?", 78.22, 15.65, LY, mid_winter.astimezone(timezone.utc))[0]
    check("midnight sun says the sun is UP", "up all day" in r_sum.lower(), r_sum)
    check("midnight sun does NOT say it fails to rise", "not rise" not in r_sum.lower(), r_sum)
    check("polar night says it does NOT rise", "not rise" in r_win.lower(), r_win)
    check("polar night does NOT say the sun is up", "up all day" not in r_win.lower(), r_win)
    r_dk = S.answer("cal when does it get dark", 78.22, 15.65, LY,
                    mid_summer.astimezone(timezone.utc))[0]
    check("midnight sun dark-ask says the sun stays up", "stays up" in r_dk.lower(), r_dk)

    # ---- 13e. the length bound must actually REFUSE ------------------------------------------
    for t in ("cal when does it get dark", "cal sunset", "cal sunrise", "cal moon phase",
              "cal solar noon", "cal twilight?", "cal moonrise"):
        r, m = S.answer(t, LAT, LON, TZ, now, max_chars=5)
        # REFUSES, does not abstain. Returning None made the responder read it as "capability
        # declined" and hand the question to the model, so a length bound became a route to an
        # invented time. The refusal must be a string, must say nothing numeric, and must be
        # marked in meta.
        check(f"over-length refused on every branch: {t[:26]}",
              isinstance(r, str) and m["refused"] == "too long"
              and not any(ch.isdigit() for ch in r), (r, m))

    # ---- 13f. solar noon is keyed to the LOCAL date ------------------------------------------
    #      At 01:00 local the UTC date is already the next day, so using now.date() silently
    #      answers for tomorrow. Only detectable near local midnight, which is why it survived.
    #      Asserted on the RECORDED date rather than the rendered time: solar noon moves only
    #      ~13 seconds a day, so a whole-day error never changes the printed minute and is
    #      undetectable from the reply text. 23:00 local is 04:00 UTC the FOLLOWING day.
    late = datetime(2026, 8, 17, 23, 0, tzinfo=TZ)
    r_noon, m_noon = S.answer("cal solar noon", LAT, LON, TZ, late.astimezone(timezone.utc))
    check("solar noon local vs UTC date actually differ in this fixture",
          late.astimezone(timezone.utc).date() != late.date())
    check("solar noon uses the LOCAL date, not the UTC date",
          m_noon.get("for_date") == "2026-08-17", m_noon)
    check("solar noon reply still states a time", ":" in (r_noon or ""), r_noon)

    # ---- 13g. ABSOLUTE moon illumination, against USNO published values ----------------------
    #      A sign error in the Meeus 48.4 series moves illumination 27% -> 15% while still
    #      staying in range, still repeating over a synodic month, and still visiting both
    #      extremes. Only an absolute vector catches it. USNO publishes at local noon.
    for d, want in ((date(2026, 1, 15), 9), (date(2026, 3, 20), 4), (date(2026, 6, 21), 48),
                    (date(2026, 8, 17), 27), (date(2026, 9, 22), 84), (date(2026, 11, 1), 51),
                    (date(2026, 12, 21), 92)):
        noon_local = datetime(d.year, d.month, d.day, 12, 0, tzinfo=ZoneInfo("America/Chicago"))
        k, _, _ = S.moon_phase(noon_local)
        check(f"moon illumination {d} ~= USNO {want}%", abs(k * 100 - want) <= 2.0,
              round(k * 100, 2))

    # ---- 13h. phase-name windows must be narrow ----------------------------------------------
    #      Widening the full/new windows says "full moon" for days on either side. Sample a whole
    #      synodic month and require each named quarter-point to occupy a short span.
    span = {}
    for n in range(0, 30 * 24):
        w = datetime(2026, 5, 1, tzinfo=timezone.utc) + timedelta(hours=n)
        _, nm, _ = S.moon_phase(w)
        span[nm] = span.get(nm, 0) + 1
    for nm in ("full moon", "new moon", "first quarter", "last quarter"):
        if nm in span:
            check(f"'{nm}' window is under 2 days", span[nm] <= 48, span[nm])

    # ---- 13i. GAPS FOUND BY ROUND 2 — 9 mutations survived 510 checks, all in code added
    #      that same day. The pattern was that the fixes themselves had no assertions.
    # (a) the TWILIGHT refusal pair (the sunrise and sunset pairs were covered; this one was not)
    for reason, want, avoid in (("always_above", "no full dark", "twilight never"),
                                ("always_below", "twilight never", "no full dark")):
        txt = S._no_event_text("civil_dusk", reason).lower()
        check(f"twilight refusal sentence for {reason}", want in txt and avoid not in txt, txt)
    check("twilight refusal differs from the sunset refusal",
          S._no_event_text("civil_dusk", "always_above")
          != S._no_event_text("sunset", "always_above"))
    # (b) the tomorrow roll must SCAN A WINDOW, not walk forward from today, and must label from
    #     the local date. Reverting either was invisible.
    kir = ZoneInfo("Pacific/Kiritimati")
    for tzname, la, lo in (("Pacific/Kiritimati", 1.87, -157.4), ("America/Anchorage", 61.22, -149.9),
                           ("Pacific/Chatham", -43.95, -176.55)):
        z = ZoneInfo(tzname)
        for hh in (0, 6, 12, 18):
            when = datetime(2026, 4, 4, hh, 3, tzinfo=timezone.utc)
            t, flag = S._next_event(when, z, la, lo, "sunset")
            if t is None:
                continue
            check(f"next sunset is in the future {tzname} {hh}h", t > when)
            check(f"next sunset is the EARLIEST ahead {tzname} {hh}h",
                  (t - when).total_seconds() <= 26 * 3600, (t - when))
            check(f"tomorrow flag matches the local date {tzname} {hh}h",
                  flag == (t.astimezone(z).date() != when.astimezone(z).date()))
    # (c) the sunset/dusk pair must be COHERENT everywhere, not just at one latitude. The
    #     same-day guard broke silently when the flags it keyed on changed meaning; keying on
    #     "the dusk that follows THIS sunset" removes the calendar from the question entirely.
    # The dateline zones are load-bearing here, not decoration: where the zone offset is far from
    # the longitude, the dusk following a sunset sits on a DIFFERENT local date, so a search window
    # anchored only on the sunset's own date finds nothing and reports "no full dark" for a night
    # that gets dark. Measured across 9 zones x 365 days, narrowing the window changes 2190 of
    # 9855 answers — and none of the mid-latitude zones show it.
    for tzname, la, lo in (("Europe/Oslo", 59.91, 10.75), ("Europe/Helsinki", 60.17, 24.94),
                           ("America/Anchorage", 61.22, -149.9), ("America/Chicago", 39.0, -95.0),
                           ("Pacific/Chatham", -43.95, -176.55),
                           ("Pacific/Kiritimati", 1.87, -157.4),
                           ("Asia/Kathmandu", 27.7, 85.3),
                           ("Atlantic/Reykjavik", 64.15, -21.9)):
        z = ZoneInfo(tzname)
        # June at high latitude is where civil dusk crosses local midnight, which is the ONLY
        # regime where a same-day pairing and a follows-this-sunset pairing disagree. Sampling
        # only round-numbered offsets missed it and let a truncated search window survive.
        for dd in (0, 40, 80, 120, 152, 155, 158, 160, 165, 170, 175, 180, 190, 200):
            when = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(days=dd, hours=19, minutes=33)
            ss, f1 = S._next_event(when, z, la, lo, "sunset")
            if ss is None:
                continue
            cd, _r = S._following(ss, z, la, lo, "civil_dusk")
            # Below ~50 degrees latitude civil dusk occurs EVERY night of the year, so "not
            # found" there is always a bug and the assertion must be unconditional. Guarding it
            # behind `if cd is None: continue` made it vacuous — it could then only ever run in
            # the case where it already passed, which is how a narrowed search window survived.
            if abs(la) < 50.0:
                check(f"dusk exists and is FOUND {tzname} +{dd}d", cd is not None, _r)
            if cd is None:
                continue
            gap = (cd - ss).total_seconds() / 60.0
            check(f"dusk follows sunset {tzname} +{dd}d", 0 < gap <= 360, round(gap, 1))
    # (d) the exclusion list must be EXERCISED — deleting it entirely passed, because the two
    #     cases present were already declined by the weak rule for want of a question mark.
    for t in ("cal twilight zone episode?", "cal moon landing year?", "cal daylight savings when?",
              "cal how far to the moon in miles?"):
        check(f"exclusion holds even with a question mark: {t[:34]}", not S.wants_sunmoon(t))
    check("exclusion does NOT veto an unambiguous ask in the same message",
          S.wants_sunmoon("cal moon landing anniversary and when is sunset?"))
    check("exclusion does NOT veto a strong ask", S.wants_sunmoon("cal does it get dark earlier "
                                                                  "after daylight savings?"))
    # (d2) _following must SCAN THE WHOLE WINDOW, not bail on the first eventless day. Near the
    #      edge of the midnight-sun band a place has civil dusk on some nights and not others; a
    #      bail reported "no full dark" for 731 nights that genuinely get dark.
    _ank = ZoneInfo("America/Anchorage")
    for dd in (180, 183, 186, 189, 192, 195, 200, 205):
        w = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(days=dd, hours=19, minutes=33)
        _ss, _f = S._next_event(w, _ank, 61.22, -149.9, "sunset")
        if _ss is None:
            continue
        _cd, _r = S._following(_ss, _ank, 61.22, -149.9, "civil_dusk")
        # either a dusk that FOLLOWS the sunset, or an honest reason — never a bail with a
        # reason borrowed from a different day
        check(f"Anchorage +{dd}d dusk follows or refuses honestly",
              (_cd is not None and 0 < (_cd - _ss).total_seconds() / 60.0 <= 360)
              or _r in ("always_above", "always_below", "not_found"), (_cd, _r))
    _w = datetime(2026, 7, 5, 19, 33, tzinfo=timezone.utc)
    _rep = S.answer("when does it get dark", 61.22, -149.9, _ank, _w)[0]
    check("Anchorage midsummer night that DOES get dark says so",
          "dark" in _rep and "no full dark" not in _rep, _rep)

    # (e0) an unresolved intent FAILS CLOSED rather than defaulting to a branch. Forced by
    #      emptying the intent table, since the import-time guard makes it otherwise unreachable.
    _saved = S._INTENTS
    try:
        S._INTENTS = ()
        _r2, _m2 = S.answer("cal sunset", LAT, LON, TZ, now)
        check("unresolved intent refuses, does not guess",
              _m2["refused"] == "unresolved intent"
              and not any(c.isdigit() for c in (_r2 or "")), (_r2, _m2))
    finally:
        S._INTENTS = _saved
    # (e) the intent fall-through must be UNREACHABLE, not merely harmless. A mutation changing
    #     its default from "dark" to "sunset" survives, and that is only acceptable if no claiming
    #     message can ever reach the default. Assert exactly that, so the day someone adds a
    #     trigger word without an intent, this fails instead of the radio answering the wrong
    #     half of the day. (That is precisely how "cal when is dawn?" returned a sunset time.)
    _claiming = ["cal sunset", "cal sunrise?", "cal when is dawn?", "cal dusk?", "cal twilight?",
                 "cal daylight?", "cal when does it get dark", "cal solar noon", "cal sundown",
                 "cal first light", "cal how long till dark", "cal is it dark yet",
                 "cal when does night fall", "cal what time does the sun go down",
                 "cal when does the sun come up", "cal when is the sun highest",
                 "cal solar zenith", "cal how much light is left", "cal will it be dark by 8",
                 "cal golden hour", "cal daybreak?", "cal last light"]
    for t in _claiming:
        if S.explain_match(t)["via"] == "sun":
            check(f"intent resolves without the default: {t[:34]}",
                  S._resolve_intent(t) is not None, t)
    #     every weak token must resolve to a REAL intent — the hole behind the dawn bug

    for tok, want in (("dawn", "sunrise"), ("dusk", "dark"), ("twilight", "twilight"),
                      ("daylight", "twilight")):
        check(f"weak token {tok!r} resolves to {want}", S._resolve_intent(tok) == want)
    check("dawn answers a MORNING event",
          "Sunrise" in (S.answer("cal when is dawn?", LAT, LON, TZ, now)[0] or ""))
    check("dawn never answers with a sunset",
          "Sunset" not in (S.answer("cal when is dawn?", LAT, LON, TZ, now)[0] or ""))
    # (f) the length bound is exact at the boundary
    r_ok, m_ok = S.answer("cal sunset", LAT, LON, TZ, now, max_chars=14)
    check("bound allows exactly max_chars", m_ok["refused"] is None and len(r_ok) == 14, r_ok)
    r_no, m_no = S.answer("cal sunset", LAT, LON, TZ, now, max_chars=13)
    check("bound refuses at max_chars+1", m_no["refused"] == "too long", (r_no, m_no))
    # (g) the exact-pole guard
    # The exact-pole guard is the cos(lat)->0 division, not the |cos H|>1 refusal. Pick a
    # declination near 0, where a near-polar latitude genuinely DOES have a rise, or the check
    # passes for the wrong reason: at 89N with declination +10 the sun never sets at all, so
    # None there proves nothing about the guard.
    check("exact pole refuses (division guard)", S._hour_angle(90.0, 0.0, -0.833) is None)
    check("near-pole with a real rise still computes",
          S._hour_angle(89.0, 0.0, -0.833) is not None)
    check("near-pole midnight sun still refuses", S._hour_angle(89.0, 10.0, -0.833) is None)
    # NOTE, recorded rather than chased: loosening the division guard from 1e-12 to 1e-300 is an
    # EQUIVALENT mutation, not an eval gap — at the exact pole cos(lat) is ~6e-17, so the division
    # still yields |cos H| far outside [-1, 1] and the refusal fires one line later. Two guards
    # cover the same input; only the second is load-bearing.
    # (h) moon must not outrank an unambiguous sun ask in the same message
    check("sun wins over a bare moon mention",
          S.explain_match("cal sunset, and is the moon nice?")["via"] == "sun")

    # ---- 13j. DATE-QUALIFIED asks are refused, not answered with today's time ----------------
    for t in ("cal sunset tomorrow?", "cal what time is sunset on christmas?",
              "cal when was sunset yesterday?", "cal sunrise next monday", "cal sunset 12/25"):
        r, m = S.answer(t, LAT, LON, TZ, now)
        check(f"other-day ask refused: {t[:34]}",
              m["refused"] == "other day" and not any(c.isdigit() for c in (r or "")), (r, m))
    for t in ("cal sunset", "cal when does it get dark", "cal sunrise?"):
        check(f"today's ask still answered: {t[:26]}",
              S.answer(t, LAT, LON, TZ, now)[1]["refused"] is None)

    # ---- 14. moon rise/set is REFUSED, never estimated ----------------------------------------
    for t in ("cal when does the moon rise", "cal moonset tonight", "cal what time is moonrise",
              "cal when does the moon set"):
        r, m = S.answer(t, LAT, LON, TZ, now)
        check(f"moon rise/set refused: {t[:30]}", m["refused"] == "not built"
              and not any(ch.isdigit() for ch in (r or "")), r)

    # ---- 15. END TO END through the responder (test what ships) ------------------------------
    cfg = dict(R.DEFAULTS)
    cfg.update({"SUNMOON_ENABLED": "true", "WEATHER_POINT": f"{LAT},{LON}",
                "CALC_ENABLED": "true", "WEATHER_ENABLED": "true"})
    p = R.plan_response(cfg, "!aaaaaaaa", "cal when does it get dark")
    check("responder: sunmoon capability", p["capability"] == "sunmoon")
    check("responder: FIXED reply, no model", p["mode"] == "fixed" and p["prompt"] is None)
    check("responder: records the sub-intent for the trace",
          (p.get("sunmoon_meta") or {}).get("intent") == "dark")
    check("responder: no weather fetch", p["weather_fact"] is None)
    # default OFF is the standing gate
    off = dict(R.DEFAULTS); off["WEATHER_POINT"] = f"{LAT},{LON}"
    check("responder: default OFF",
          R.plan_response(off, "!aaaaaaaa", "cal when does it get dark")["capability"] is None)
    # fail-closed with no point, exactly like GREET_TEXT
    noptn = dict(cfg); noptn["WEATHER_POINT"] = ""
    pn = R.plan_response(noptn, "!aaaaaaaa", "cal when does it get dark")
    check("responder: fail-closed without a point",
          pn["mode"] == "fixed" and "not configured" in (pn["fixed_reply"] or ""))
    for bad in ("garbage", "999,999", "39.0", ",", "39.0,abc"):
        b = dict(cfg); b["WEATHER_POINT"] = bad
        pb = R.plan_response(b, "!aaaaaaaa", "cal sunset")
        check(f"responder: malformed point fails closed ({bad})",
              "not configured" in (pb["fixed_reply"] or ""), pb["fixed_reply"])
    # a named whitelisted place must NOT move the observer (location-as-exfil closed)
    wl = dict(cfg); wl["WEATHER_PLACES"] = "townx:40.0,-96.0"
    a1 = R.plan_response(wl, "!aaaaaaaa", "cal sunset")["fixed_reply"]
    a2 = R.plan_response(wl, "!aaaaaaaa", "cal sunset in townx")["fixed_reply"]
    check("named place does not move the sun observer", a1 == a2, (a1, a2))
    # the other capabilities still win their own questions
    check("calc still wins arithmetic",
          R.plan_response(cfg, "!aaaaaaaa", "cal 12*12")["capability"] == "calc")
    check("weather still wins a forecast ask",
          R.plan_response(cfg, "!aaaaaaaa", "cal whats the high today")["capability"] == "weather")

    # ---- 15b. CAPABILITY COLLISIONS. sunmoon sits above weather and calc, so it inherits the
    #      obligation not to steal from either. Both thefts were found live: "cal sunset 12*12"
    #      answered with a sunset time instead of 144, and "cal will it rain at sunset" answered
    #      with a sunset time instead of the forecast refusal. The calc rescue was written once
    #      for weather and then re-forgotten one layer up, which is why it now lives beside the
    #      capabilities rather than inside one of them.
    for q, want in (("cal sunset 12*12", "calc"), ("cal temp 12*12", "calc")):
        p2 = R.plan_response(cfg, "!aaaaaaaa", q)
        check(f"calc not stolen: {q}", p2["capability"] == want and "144" in (p2["fixed_reply"] or ""),
              (p2["capability"], p2["fixed_reply"]))
    # A message carrying a STRONG weather word reaches weather and is refused as a forecast.
    for q in ("cal whats the temp at dusk", "cal high today at sunset"):
        p2 = R.plan_response(cfg, "!aaaaaaaa", q)
        check(f"weather not stolen: {q[:32]}",
              p2["capability"] == "weather" and p2.get("forecast_asked") is True,
              (p2["capability"], p2["fixed_reply"]))
    # A message carrying only a WEAK weather word reaches neither: weather's claim rule is
    # deliberately narrow (widening it claimed 210 of 210 synthetic non-weather pairs and was
    # reverted), and sun/moon declines anything with a weather word rather than grabbing it.
    # What matters here is only that sun/moon does NOT answer it with a sun time.
    for q in ("cal whats the wind at dawn", "cal will it rain at sunset", "cal rain before dark"):
        p2 = R.plan_response(cfg, "!aaaaaaaa", q)
        check(f"sunmoon yields on a weather word: {q[:32]}",
              p2["capability"] != "sunmoon" and p2["fixed_reply"] is None,
              (p2["capability"], p2["fixed_reply"]))
    for q in ("cal when does it get dark", "cal sunset", "cal moon phase",
              "cal what time does the sun go down"):
        check(f"sunmoon still wins its own: {q[:32]}",
              R.plan_response(cfg, "!aaaaaaaa", q)["capability"] == "sunmoon")

    # ---- 15c. CONFIG FAILURES MUST FAIL CLOSED, NEVER OPEN ------------------------------------
    #      An unparseable timezone used to fall back to UTC and put a five-hours-wrong local time
    #      on air with no warning and no trace flag — the cleanest "wrong answer that looks right"
    #      in the module. A malformed length bound used to raise out of plan_response, which the
    #      daemon swallowed BEFORE the decision record was written: the capability went silent
    #      with nothing on the public dashboard.
    good = R.plan_response(cfg, "!aaaaaaaa", "cal sunset")["fixed_reply"]
    for bad_tz in ("Mars/Olympus", "", "../../../etc/passwd", "UTC+5", "Not/A/Zone"):
        c2 = dict(cfg); c2["SUNMOON_TZ"] = bad_tz
        rp = R.plan_response(c2, "!aaaaaaaa", "cal sunset")["fixed_reply"]
        check(f"bad tz fails closed: {bad_tz[:20]!r}",
              "not configured" in (rp or "") and rp != good, rp)
    for bad_len in ("abc", "", None, "12.5", "-1"):
        c2 = dict(cfg)
        if bad_len is None:
            c2.pop("SUNMOON_MAX_CHARS", None)
        else:
            c2["SUNMOON_MAX_CHARS"] = bad_len
        try:
            rp = R.plan_response(c2, "!aaaaaaaa", "cal sunset")
            check(f"malformed max_chars does not raise: {bad_len!r}", True)
        except Exception as exc:
            check(f"malformed max_chars does not raise: {bad_len!r}", False, repr(exc))

    # ---- 15d. THE PUBLIC TRACE MUST NAME THE RIGHT CAUSE --------------------------------------
    #      Every sun/moon reply was logged as 'fixed_weather_unavailable' because the fixed_kind
    #      ladder had no rung for it and fell to the else — the trace asserted a cause that did
    #      not happen, which is the same defect class as the caption fixed in session 118.
    check("sunmoon has its own fixed_kind",
          R.plan_response(cfg, "!aaaaaaaa", "cal sunset")["fixed_kind"] == "sunmoon")
    check("sunmoon match is exposed for the record",
          (R.plan_response(cfg, "!aaaaaaaa", "cal sunset").get("sunmoon_match") or {}).get("via")
          == "sun")

    return passed, failures


# --- §7 authoritative vectors -------------------------------------------------------------------
# (date, lat, lon, tz, event, hour, minute, source-label).
#
# COORDINATES HERE ARE NOT PLACEHOLDERS AND CANNOT BE — an absolute vector is only meaningful at
# the location it was published for. 38.88,-94.82 is the Olathe city centroid, which is already
# public: the dashboard names KOJC (the city's airport) as the observing station on every weather
# reply. The DEPLOYED observer point is a different value and lives only in the gitignored config.
# The high-latitude rows are Alaskan cities and localize nobody.
#
# Sun values are U.S. Naval Observatory published times, nearest minute:
#   https://aa.usno.navy.mil/api/rstt/oneday?date=YYYY-MM-DD&coords=38.88,-94.82&tz=-6&dst=true|false
# USNO's oneday endpoint publishes civil twilight only; the nautical and astronomical rows are
# from an independent ephemeris (PyEphem/libastro 4.2.1, pressure=0) which reproduced every one of
# USNO's published sun values exactly on rounding, so it is trustworthy where USNO is silent.
_KS = (38.88, -94.82, "America/Chicago")
VECTORS = [
    # equinoxes, solstices, today, and both US DST transition days
    (date(2026, 3, 20), *_KS, "civil_dawn", 6, 56, "USNO"),
    (date(2026, 3, 20), *_KS, "sunrise", 7, 23, "USNO"),
    (date(2026, 3, 20), *_KS, "solar_noon", 13, 27, "USNO"),
    (date(2026, 3, 20), *_KS, "sunset", 19, 31, "USNO"),
    (date(2026, 3, 20), *_KS, "civil_dusk", 19, 58, "USNO"),
    (date(2026, 6, 21), *_KS, "civil_dawn", 5, 22, "USNO"),
    (date(2026, 6, 21), *_KS, "sunrise", 5, 54, "USNO"),
    (date(2026, 6, 21), *_KS, "solar_noon", 13, 21, "USNO"),
    (date(2026, 6, 21), *_KS, "sunset", 20, 48, "USNO"),
    (date(2026, 6, 21), *_KS, "civil_dusk", 21, 20, "USNO"),
    (date(2026, 8, 17), *_KS, "civil_dawn", 6, 7, "USNO"),
    (date(2026, 8, 17), *_KS, "sunrise", 6, 35, "USNO"),
    (date(2026, 8, 17), *_KS, "solar_noon", 13, 23, "USNO"),
    (date(2026, 8, 17), *_KS, "sunset", 20, 11, "USNO"),
    (date(2026, 8, 17), *_KS, "civil_dusk", 20, 39, "USNO"),
    (date(2026, 9, 22), *_KS, "civil_dawn", 6, 41, "USNO"),
    (date(2026, 9, 22), *_KS, "sunrise", 7, 7, "USNO"),
    (date(2026, 9, 22), *_KS, "sunset", 19, 16, "USNO"),
    (date(2026, 9, 22), *_KS, "civil_dusk", 19, 43, "USNO"),
    (date(2026, 12, 21), *_KS, "civil_dawn", 7, 4, "USNO"),
    (date(2026, 12, 21), *_KS, "sunrise", 7, 34, "USNO"),
    (date(2026, 12, 21), *_KS, "solar_noon", 12, 17, "USNO"),
    (date(2026, 12, 21), *_KS, "sunset", 17, 1, "USNO"),
    (date(2026, 12, 21), *_KS, "civil_dusk", 17, 31, "USNO"),
    (date(2026, 1, 15), *_KS, "sunrise", 7, 36, "USNO"),
    (date(2026, 1, 15), *_KS, "sunset", 17, 22, "USNO"),
    (date(2026, 1, 15), *_KS, "civil_dusk", 17, 51, "USNO"),
    # DST spring forward: every event lands after the 02:00->03:00 jump, so all are CDT
    (date(2026, 3, 8), *_KS, "civil_dawn", 7, 15, "USNO/DST-start"),
    (date(2026, 3, 8), *_KS, "sunrise", 7, 41, "USNO/DST-start"),
    (date(2026, 3, 8), *_KS, "sunset", 19, 19, "USNO/DST-start"),
    (date(2026, 3, 8), *_KS, "civil_dusk", 19, 46, "USNO/DST-start"),
    # DST fall back: the 01:00-02:00 local hour occurs twice
    (date(2026, 11, 1), *_KS, "civil_dawn", 6, 19, "USNO/DST-end"),
    (date(2026, 11, 1), *_KS, "sunrise", 6, 47, "USNO/DST-end"),
    (date(2026, 11, 1), *_KS, "sunset", 17, 19, "USNO/DST-end"),
    (date(2026, 11, 1), *_KS, "civil_dusk", 17, 46, "USNO/DST-end"),
    # the twilight bands USNO does not publish, from the independent ephemeris
    (date(2026, 8, 17), *_KS, "astronomical_dawn", 4, 56, "ephem"),
    (date(2026, 8, 17), *_KS, "nautical_dawn", 5, 32, "ephem"),
    (date(2026, 8, 17), *_KS, "nautical_dusk", 21, 13, "ephem"),
    (date(2026, 8, 17), *_KS, "astronomical_dusk", 21, 49, "ephem"),
    (date(2026, 6, 21), *_KS, "astronomical_dawn", 3, 55, "ephem"),
    (date(2026, 6, 21), *_KS, "nautical_dawn", 4, 41, "ephem"),
    (date(2026, 6, 21), *_KS, "nautical_dusk", 22, 0, "ephem"),
    (date(2026, 6, 21), *_KS, "astronomical_dusk", 22, 47, "ephem"),
]


def self_test():
    """Negative controls. Each mutation is a plausible real bug; every one MUST be caught."""
    import re as _re
    originals = {n: getattr(S, n) for n in
                 ("ALT_SUNRISE", "ALT_CIVIL", "_solar", "_hour_angle", "moon_phase",
                  "_MOON_RISESET", "no_event_reason")}
    orig_hour = S._hour_angle
    orig_solar = S._solar

    MUTATIONS = [
        ("sunrise refraction constant dropped (alt 0 instead of -0.833)",
         lambda: setattr(S, "ALT_SUNRISE", 0.0)),
        ("civil twilight angle wrong (-5 instead of -6)",
         lambda: setattr(S, "ALT_CIVIL", -5.0)),
        ("equation of time dropped",
         lambda: setattr(S, "_solar", lambda jc: (orig_solar(jc)[0], 0.0))),
        ("declination sign flipped",
         lambda: setattr(S, "_solar", lambda jc: (-orig_solar(jc)[0], orig_solar(jc)[1]))),
        ("hour angle clamps instead of refusing",
         lambda: setattr(S, "_hour_angle",
                         lambda lat, dec, alt: (orig_hour(lat, dec, alt) or 90.0))),
        ("polar reason always reports always_below",
         lambda: setattr(S, "no_event_reason", lambda d, lat, alt: "always_below")),
        ("moon illumination inverted",
         lambda: setattr(S, "moon_phase",
                         lambda w: (1.0 - originals["moon_phase"](w)[0],)
                         + originals["moon_phase"](w)[1:])),
        ("moon rise/set no longer recognised (would fall through to the model)",
         lambda: setattr(S, "_MOON_RISESET", _re.compile(r"(?!x)x"))),
    ]
    print("negative controls — each mutation MUST be caught:")
    all_caught = True
    for name, mutate in MUTATIONS:
        mutate()
        try:
            _, fails = run()
            caught = len(fails) > 0
        except Exception:
            caught = True                    # a crash is a detection, if an ugly one
        all_caught &= caught
        print(f"  [{'CAUGHT' if caught else 'SURVIVED'}] {name}")
        for n, v in originals.items():
            setattr(S, n, v)
    return all_caught


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        ok = self_test()
        print("\nall mutations caught" if ok else "\nA MUTATION SURVIVED — eval is vacuous")
        sys.exit(0 if ok else 1)
    p, f = run()
    if not VECTORS:
        f.append("NO AUTHORITATIVE VECTORS LOADED — structure is checked, accuracy is not")
    print(f"eval_sunmoon: {p} passed, {len(f)} failed")
    for name in f:
        print("  FAIL:", name)
    sys.exit(1 if f else 0)
