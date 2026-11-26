#!/usr/bin/env python3
"""ON THIS DAY — one midday post, only on a date with a strong entry: a dated US event and the
practical lesson it teaches (Dean, 2026-11-26).

THE BAR is a rule, not taste: every entry in data/on_this_day.json pairs an EVENT with a
TAKEAWAY a listener can act on, and BOTH carry an https source and a verbatim excerpt, re-checked
by an independent pass before any of it was broadcast. Trivia with no lesson is not in the table.

Dean's calls: midday delivery; only days with a strong entry (no entry, no post -- about one day
in six); Kansas City-area events preferred where there is one; United States only. In the table,
`local: true` means Missouri or Kansas -- the region, not only the metro -- and sorts first.

Delivery rides the presence timer, like the holiday announcement, and obeys the same rules: the
master switch, PRESENCE_ENABLED and its own flag; never into a conversation or a busy channel;
at least two hours after Cal's last post. On a holiday it stands down -- that day's post is the
holiday's -- and on an "on this day" date the ordinary greeting holds off until it has gone out,
so the day still has one post from presence. Nothing here is written by a model.
"""
import json
import os
import re
from datetime import date

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data", "on_this_day.json")
MAX_CHARS = 200          # Meshtastic text payload
WINDOW = (12, 14)        # midday on the operator's clock: from 12:00 to before 14:00
MIN_GAP_S = 2 * 3600

_MMDD = re.compile(r"^(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")


def _ok(e):
    return (isinstance(e, dict) and isinstance(e.get("date"), str) and _MMDD.match(e["date"])
            and isinstance(e.get("year"), int) and not isinstance(e.get("year"), bool)
            and 1600 <= e["year"] <= date.today().year
            and isinstance(e.get("event"), str) and e["event"].strip()
            and isinstance(e.get("takeaway"), str) and e["takeaway"].strip()
            and str(e.get("event_source", "")).startswith("https://")
            and str(e.get("takeaway_source", "")).startswith("https://"))


def load(path=None):
    """{"MM-DD": [entry, ...]} with Kansas City-area entries first. An entry missing either
    source, or malformed in any way, is dropped -- never broadcast."""
    try:
        raw = json.load(open(path or DATA, encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out = {}
    for e in raw if isinstance(raw, list) else []:
        if _ok(e) and text(e) is not None:
            out.setdefault(e["date"], []).append(e)
    for d in out:
        out[d].sort(key=lambda e: (not e.get("local"), e["year"]))
    return out


def entry_id(e):
    """Date and year: stable when the wording of an entry is edited (review 2026-11-26: keying on
    the text let an edited entry air again before the rest of its date's rotation). The table
    holds at most one entry per date and year; eval_ontheday asserts it."""
    return f"{e['date']}:{e['year']}"


def text(e):
    s = f"On this day in {e['year']}: {e['event'].strip()} {e['takeaway'].strip()}"
    return s if len(s.encode("utf-8")) <= MAX_CHARS else None


def choose(entries, used):
    """The first entry not yet used for this date (local first); when all have been, start over.
    `used` is the list of entry ids already broadcast for this date in earlier years."""
    if not entries:
        return None
    for e in entries:
        if entry_id(e) not in used:
            return e
    return entries[0]


def plan_post(cfg, today, local_hour, posted, heard_ts, ch_util, now, quiet_s, max_util,
              last_send_ts=None, holiday_today=False, entries=None):
    """(entry | None, reason). Pure. `posted` is the set of "YYYY-MM-DD" already posted."""
    if str(cfg.get("RESPONDER_ENABLED", "false")).lower() != "true":
        return None, "responder_disabled"
    if str(cfg.get("ON_THIS_DAY_ENABLED", "false")).lower() != "true":
        return None, "on_this_day_disabled"
    if str(cfg.get("PRESENCE_ENABLED", "false")).lower() != "true":
        return None, "presence_disabled"
    if posted is None:
        return None, "history_unreadable"
    if today.isoformat() in posted:
        return None, "already_posted"
    todays = (entries if entries is not None else load()).get(today.strftime("%m-%d"))
    if not todays:
        return None, "no_entry_today"
    if holiday_today:
        return None, "holiday_today"
    if local_hour is None or not (WINDOW[0] <= local_hour < WINDOW[1]):
        return None, "outside_midday"
    if isinstance(last_send_ts, (int, float)) and now - last_send_ts < MIN_GAP_S:
        return None, "too_soon_after_last_post"
    if heard_ts is not None and (heard_ts == "unknown" or now - heard_ts < quiet_s):
        return None, "channel_in_conversation_or_unknown"
    if (not isinstance(ch_util, (int, float)) or isinstance(ch_util, bool) or ch_util != ch_util
            or ch_util < 0 or ch_util > max_util):
        return None, "channel_busy_or_unknown"
    return todays, "post"


def pending(cfg, today, posted, holiday_today=False, entries=None):
    """True while today has an armed, unposted entry and is not a holiday. The ordinary presence
    greeting holds off while this is true, so the day keeps one post from presence."""
    if (str(cfg.get("ON_THIS_DAY_ENABLED", "false")).lower() != "true"
            or str(cfg.get("PRESENCE_ENABLED", "false")).lower() != "true"
            or posted is None or holiday_today or today.isoformat() in posted):
        return False
    return bool((entries if entries is not None else load()).get(today.strftime("%m-%d")))


if __name__ == "__main__":
    for d, es in sorted(load().items()):
        for e in es:
            print(d, "KC" if e.get("local") else "US", text(e))
