#!/usr/bin/env python3
"""HOLIDAYS — Cal knows the day, says so once, and answers holiday wishes with a fact.

WHY (Dean, 2026-11-26, Thanksgiving). Cal should know US holidays, announce one on the day,
and answer people who post about it with a short known fact — a different one each time.

THREE PARTS, all deterministic:
  * the CALENDAR is computed here, offline: fixed dates, "nth weekday of the month" rules and
    Easter by the Gregorian computus. Checked against the federal holiday schedule and a
    published Easter table in eval_holidays, not against this file's own idea of the dates.
  * the FACTS come from data/holiday_facts.json, every entry with its source URL and a verbatim
    excerpt. Nothing on air about a holiday is written by a model.
  * NO REPEATS: each holiday's facts are dealt like a shuffled deck, one per use, reshuffled
    only when the deck is empty. The deck lives in holiday-state.json under a file lock, because
    two processes draw from it (the presence timer's announcement, the responder's replies).

Which holidays (Dean's call): the 11 federal holidays and 8 widely celebrated ones. The holiday
is the actual date, not the federal "observed" Friday or Monday.
"""
import fcntl
import json
import os
import random
import re
from datetime import date, timedelta

BASE = os.path.dirname(os.path.abspath(__file__))
FACTS = os.path.join(BASE, "data", "holiday_facts.json")
STATE = os.path.join(BASE, "holiday-state.json")

# key -> (display name, wish phrases). A wish phrase SELECTS the holiday; it never shapes the reply.
HOLIDAYS = {
    "new_years_day": ("New Year's Day", ["happy new year", "happy new years", "happy new year's"]),
    "mlk_day": ("Martin Luther King Jr. Day", ["happy mlk day", "happy martin luther king day",
                                               "happy martin luther king jr day"]),
    "washingtons_birthday": ("Washington's Birthday", ["happy presidents day",
                                                       "happy president's day",
                                                       "happy presidents' day",
                                                       "happy washington's birthday"]),
    "valentines_day": ("Valentine's Day", ["happy valentine's day", "happy valentines day",
                                           "happy valentines", "happy valentine's"]),
    "st_patricks_day": ("St. Patrick's Day", ["happy st patrick's day", "happy st patricks day",
                                              "happy st. patrick's day", "happy saint patrick's day",
                                              "happy st paddy's day", "happy st paddys day",
                                              "happy st patty's day", "happy st pattys day"]),
    "easter": ("Easter", ["happy easter"]),
    "mothers_day": ("Mother's Day", ["happy mother's day", "happy mothers day"]),
    "memorial_day": ("Memorial Day", ["happy memorial day"]),
    "fathers_day": ("Father's Day", ["happy father's day", "happy fathers day"]),
    "juneteenth": ("Juneteenth", ["happy juneteenth"]),
    "independence_day": ("Independence Day", ["happy 4th", "happy fourth", "happy 4th of july",
                                              "happy fourth of july", "happy independence day",
                                              "happy july 4th"]),
    "labor_day": ("Labor Day", ["happy labor day"]),
    "columbus_day": ("Columbus Day", ["happy columbus day", "happy indigenous peoples day",
                                      "happy indigenous peoples' day"]),
    "halloween": ("Halloween", ["happy halloween", "trick or treat"]),
    "veterans_day": ("Veterans Day", ["happy veterans day", "happy veteran's day",
                                      "happy veterans' day"]),
    "thanksgiving": ("Thanksgiving", ["happy thanksgiving", "happy turkey day"]),
    "christmas_eve": ("Christmas Eve", ["merry christmas eve", "happy christmas eve"]),
    "christmas": ("Christmas", ["merry christmas", "happy christmas", "merry xmas"]),
    "new_years_eve": ("New Year's Eve", ["happy new year's eve", "happy new years eve"]),
}
FIXED = {"new_years_day": (1, 1), "valentines_day": (2, 14), "st_patricks_day": (3, 17),
         "juneteenth": (6, 19), "independence_day": (7, 4), "halloween": (10, 31),
         "veterans_day": (11, 11), "christmas_eve": (12, 24), "christmas": (12, 25),
         "new_years_eve": (12, 31)}
# (month, weekday Mon=0, n) -- n=-1 means the last one in the month
NTH = {"mlk_day": (1, 0, 3), "washingtons_birthday": (2, 0, 3), "mothers_day": (5, 6, 2),
       "memorial_day": (5, 0, -1), "fathers_day": (6, 6, 3), "labor_day": (9, 0, 1),
       "columbus_day": (10, 0, 2), "thanksgiving": (11, 3, 4)}


def easter(year):
    """Gregorian Easter Sunday (the anonymous Gregorian algorithm, Meeus/Jones/Butcher)."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return date(year, month, day + 1)


def _nth(year, month, weekday, n):
    if n > 0:
        d = date(year, month, 1)
        d += timedelta(days=(weekday - d.weekday()) % 7)
        return d + timedelta(weeks=n - 1)
    nxt = date(year + (month == 12), month % 12 + 1, 1)
    d = nxt - timedelta(days=1)
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def dates(year):
    """{key: date} for every holiday in `year`."""
    out = {k: date(year, m, d) for k, (m, d) in FIXED.items()}
    out.update({k: _nth(year, *spec) for k, spec in NTH.items()})
    out["easter"] = easter(year)
    return out


def on(day):
    """Holiday keys falling on `day`, in calendar order of the table above."""
    ds = dates(day.year)
    return [k for k in HOLIDAYS if ds.get(k) == day]


def name(key):
    return HOLIDAYS[key][0]


# --- wishes ---------------------------------------------------------------------------------
# CLOSED WORLD (adversarial review 2026-11-26). The first version matched the phrase anywhere in
# a short message, and "not a happy thanksgiving here, power is out and my dad is in the
# hospital" got a cheerful fact, while "Happy Thanksgiving cal whats the weather tomorrow" got a
# fact instead of the weather. A message is a wish only if EVERY word besides the wish itself is
# greeting vocabulary from this list, or up to four words of a place after "from" or "in". Any
# other word -- "not", "but", "hate", "weather", a question -- means it is not a wish, and the
# message goes on to the ladders as if this did not exist.
_OK = frozenset("""
    everyone everybody all yall y'all mesh meshers folks friends fam family loved ones
    and to you your yours too cal us our ya hi hey hello howdy hiya
    hope have has a an the great good wonderful safe blessed happy merry wishing wish enjoy
    day holiday holidays morning evening afternoon out there here of
""".split())
_PLACE_AFTER = ("from", "in")
_PLACE_WORDS = 4
_MAX_WORDS = 15
# The reply mirrors the name the sender used.
_OPENER_NAME = {"happy indigenous peoples day": "Indigenous Peoples' Day",
                "happy indigenous peoples' day": "Indigenous Peoples' Day",
                "trick or treat": "Halloween"}


def _norm(text):
    t = (text or "").lower().replace("’", "'").replace("？", "?")
    t = re.sub(r"[^\w\s'?]", " ", t)               # drop emoji and punctuation except ' and ?
    return re.sub(r"\s+", " ", t).strip()


def _only_greeting(rest):
    """True if the words left after removing the wish are all greeting vocabulary or a place."""
    budget = 0
    for w in rest:
        if w in _OK:
            budget = 0
            continue
        if w in _PLACE_AFTER:
            budget = _PLACE_WORDS
            continue
        if budget > 0 and re.fullmatch(r"[a-z0-9']+", w):
            budget -= 1
            continue
        return False
    return True


def match_wish(text, day):
    """(holiday key, opener) for a message that is a holiday wish for today or tomorrow, else
    None. The opener mirrors the sender: "Merry Christmas", "Happy Indigenous Peoples' Day"."""
    t = _norm(text)
    if not t or "?" in t or len(t.split()) > _MAX_WORDS:
        return None
    words = t.split()
    for d in (day, day + timedelta(days=1)):
        for k in on(d):
            for w in sorted(HOLIDAYS[k][1], key=len, reverse=True):
                ww = w.split()
                for i in range(len(words) - len(ww) + 1):
                    seg = words[i:i + len(ww)]
                    if seg == ww or seg[:-1] + [seg[-1].rstrip("s")] == ww:
                        if _only_greeting(words[:i] + words[i + len(ww):]):
                            head = "Merry" if w.startswith("merry") else "Happy"
                            return k, f"{head} {_OPENER_NAME.get(w, name(k))}"
                        return None
    return None


def wished(text, day):
    """The holiday key a message wishes, or None (see match_wish)."""
    m = match_wish(text, day)
    return m[0] if m else None


# --- facts: no repeats until every fact has been used, under a lock -------------------------
def load_facts(path=None):
    """{key: [fact, ...]}; entries without an https source are dropped, never broadcast."""
    try:
        raw = json.load(open(path or FACTS, encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out = {}
    for k, items in (raw.items() if isinstance(raw, dict) else []):
        if k not in HOLIDAYS or not isinstance(items, list):
            continue
        fs = [i["fact"].strip() for i in items if isinstance(i, dict) and isinstance(i.get("fact"), str)
              and i["fact"].strip() and str(i.get("source", "")).startswith("https://")]
        if fs:
            out[k] = fs
    return out


def _clean_state(st):
    """The parts of a state file that have the right shape; anything else is ignored, never
    trusted and never a crash (review 2026-11-26: {"decks": []} crashed presence all day)."""
    st = st if isinstance(st, dict) else {}
    used = st.get("used") if isinstance(st.get("used"), dict) else {}
    last = st.get("last") if isinstance(st.get("last"), dict) else {}
    return {"used": {k: [f for f in v if isinstance(f, str)] for k, v in used.items()
                     if isinstance(k, str) and isinstance(v, list)},
            "last": {k: v for k, v in last.items() if isinstance(k, str) and isinstance(v, str)}}


def draw(key, facts=None, state_path=None, rng=None):
    """The next fact for `key`: one not used since the cycle began, never the one just said.
    Tracks USED facts rather than a remaining deck, so editing the facts file mid-cycle cannot
    bring back facts already said (review 2026-11-26). None if there are no facts or the state
    cannot be written: a fact we cannot record would repeat."""
    try:
        facts = load_facts() if facts is None else facts
        pool = facts.get(key) or []
        if not pool:
            return None
        path = state_path or STATE
        rng = rng or random.Random()
        fd = os.open(path + ".lock", os.O_CREAT | os.O_RDWR, 0o644)
    except Exception:
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            st = _clean_state(json.load(open(path, encoding="utf-8")))
        except (OSError, ValueError):
            st = _clean_state({})
        used = [f for f in st["used"].get(key, []) if f in pool]
        last = st["last"].get(key)
        avail = [f for f in pool if f not in used]
        if not avail:
            used = []
            avail = [f for f in pool if f != last] or pool[:]
        fact = rng.choice(avail)
        st["used"][key] = used + [fact]
        st["last"][key] = fact
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False)
        os.replace(tmp, path)
        return fact
    except Exception:
        return None
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


MAX_CHARS = 200          # Meshtastic text payload; replies here stay well under it


def reply_text(key, fact, opener=None):
    return _clip(f"{opener or 'Happy ' + name(key)}! {fact}")


def announce_text(key, fact):
    head = "Merry" if key in ("christmas", "christmas_eve") else "Happy"
    return _clip(f"{head} {name(key)}, mesh! {fact}")


def _clip(s):
    return s if len(s.encode("utf-8")) <= MAX_CHARS else None


# --- the announcement: once per holiday, from 9 AM local ------------------------------------
ANNOUNCE_FROM_H, ANNOUNCE_UNTIL_H = 9, 21
ANNOUNCE_MIN_GAP_S = 2 * 3600


def pending(cfg, today, announced):
    """Holiday keys today that are armed to be announced and have not been. The ordinary
    presence greeting holds off while this is non-empty, so on a holiday the announcement is the
    day's one post (review 2026-11-26: both used to go out)."""
    if (str(cfg.get("HOLIDAY_ANNOUNCE_ENABLED", "false")).lower() != "true"
            or str(cfg.get("PRESENCE_ENABLED", "false")).lower() != "true" or announced is None):
        return []
    return [k for k in on(today) if f"{today.isoformat()}:{k}" not in announced]


def plan_announce(cfg, today, local_hour, announced, heard_ts, ch_util, now, quiet_s, max_util,
                  last_send_ts=None):
    """(holiday key | None, reason). Pure. `announced` is the set of "YYYY-MM-DD:key" already
    sent. Needs the master switch, PRESENCE_ENABLED and its own flag; same quiet-channel and
    busy-channel rules as the presence greeting; and at least two hours after Cal's last post.
    NOT presence's rarity caps, because the holiday is the reason to speak."""
    if str(cfg.get("RESPONDER_ENABLED", "false")).lower() != "true":
        return None, "responder_disabled"
    if str(cfg.get("HOLIDAY_ANNOUNCE_ENABLED", "false")).lower() != "true":
        return None, "holiday_announce_disabled"
    if str(cfg.get("PRESENCE_ENABLED", "false")).lower() != "true":
        return None, "presence_disabled"
    if announced is None:
        return None, "history_unreadable"
    keys = pending(cfg, today, announced)
    if not keys:
        return None, "no_holiday_to_announce"
    if local_hour is None or not (ANNOUNCE_FROM_H <= local_hour < ANNOUNCE_UNTIL_H):
        return None, "outside_announce_hours"
    if isinstance(last_send_ts, (int, float)) and now - last_send_ts < ANNOUNCE_MIN_GAP_S:
        return None, "too_soon_after_last_post"
    if heard_ts is not None and (heard_ts == "unknown" or now - heard_ts < quiet_s):
        return None, "channel_in_conversation_or_unknown"
    if (not isinstance(ch_util, (int, float)) or isinstance(ch_util, bool) or ch_util != ch_util
            or ch_util < 0 or ch_util > max_util):
        return None, "channel_busy_or_unknown"
    return keys[0], "announce"


if __name__ == "__main__":
    import sys
    y = int(sys.argv[1]) if len(sys.argv) > 1 else date.today().year
    for k, d in sorted(dates(y).items(), key=lambda kv: kv[1]):
        print(d.isoformat(), d.strftime("%a"), name(k))
