#!/usr/bin/env python3
"""PRESENCE — the occasional word on the channel, like any other station.

WHY (Dean, 2026-09-24). In seven weeks the mesh sent 337 messages from 108 nodes that are not on
the allow-list, and exactly ONE of them was addressed to Cal. Nobody asks a station they have
never heard. Advertising was ruled out -- no "ask me about sunsets". What a person on the mesh
does instead is say good morning and ask for a radio check, and that is all this does.

WHAT IT SAYS. Only the operator's own phrases, from config, one of:
    PRESENCE_MORNING    07:00-10:00 local
    PRESENCE_AFTERNOON  12:00-16:00 local
    PRESENCE_EVENING    18:00-21:00 local
    PRESENCE_CHECK      any of those windows
    PRESENCE_TEST       any of those windows, only if set (blank by default = not in rotation)
The code's defaults carry no place name: where the station is belongs to the operator's config,
not to a public repo.

WHY IT IS RARE. Every rule below makes it quieter, never chattier:
  * off unless PRESENCE_ENABLED=true, and silent whenever RESPONDER_ENABLED is off (the master
    switch covers everything Cal transmits on his own);
  * at most PRESENCE_MAX_PER_WEEK sends in any rolling 7 days, and never within PRESENCE_MIN_GAP_H
    hours of the last one;
  * a run inside a window fires only with probability PRESENCE_CHANCE, so the time is not a
    fixed schedule that reads as a bot;
  * never the same phrase twice in a row;
  * never into a conversation: if any text was heard on the channel in the last
    PRESENCE_QUIET_S seconds, it waits;
  * never into a busy channel: channel utilization above PRESENCE_MAX_CH_UTIL, or unknown, waits.

PROPOSE-AND-WRITE, like the responder: one entry into outbox/, and the bridge transmits it.
Run it from a timer (deploy/) every 30 minutes with --send; without --send it only reports.
"""
import json
import os
import random
import time
from datetime import datetime

DEFAULTS = {
    "PRESENCE_ENABLED": "false",
    "PRESENCE_MAX_PER_WEEK": "3",
    "PRESENCE_MAX_PER_DAY": "1",       # per calendar day on the operator's clock
    "PRESENCE_MIN_GAP_H": "36",
    "PRESENCE_CHANCE": "0.08",
    "PRESENCE_QUIET_S": "900",
    "PRESENCE_MAX_CH_UTIL": "25",
    "PRESENCE_CHANNEL": "0",
    "PRESENCE_MORNING": "Morning Mesh",
    "PRESENCE_AFTERNOON": "Afternoon Mesh",
    "PRESENCE_EVENING": "Evening Mesh",
    "PRESENCE_CHECK": "Radio check. Anyone out there?",
    # A third phrase for any window, e.g. "Cal testing from <town>" (Dean, 2026-09-25). Blank
    # leaves it out of the rotation entirely rather than failing the run as bad_phrase.
    "PRESENCE_TEST": "",
    # The windows are the OPERATOR's clock, not the host's: a box on UTC would say good morning
    # at 2 a.m. Central (review 2026-09-24). Unparseable fails closed.
    "PRESENCE_TZ": "America/Chicago",
    # The radio's status is only evidence while it is fresh. Older than this and the bridge may
    # be down -- and a greeting queued then would go out whenever it came back, into anything.
    "PRESENCE_STATUS_MAX_AGE_S": "600",
}
# (first hour, last hour exclusive, the greeting key for that window)
WINDOWS = ((7, 10, "PRESENCE_MORNING"), (12, 16, "PRESENCE_AFTERNOON"),
           (18, 21, "PRESENCE_EVENING"))
MAX_CHARS = 64


def _num(cfg, key):
    try:
        v = float(cfg.get(key, DEFAULTS[key]))
        if v != v or v in (float("inf"), float("-inf")):
            raise ValueError
        return v
    except (TypeError, ValueError):
        return float(DEFAULTS[key])


def local_hour_now(cfg, now):
    """The hour in PRESENCE_TZ, or None if the zone is unusable (fail closed)."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.fromtimestamp(now, ZoneInfo(str(cfg.get("PRESENCE_TZ", DEFAULTS["PRESENCE_TZ"])))).hour
    except Exception:
        return None


def _local_date(cfg, ts):
    try:
        from zoneinfo import ZoneInfo
        return datetime.fromtimestamp(ts, ZoneInfo(str(cfg.get("PRESENCE_TZ", DEFAULTS["PRESENCE_TZ"])))).date()
    except Exception:
        return None


def window(local_hour):
    """The greeting key for this hour, or None outside every window."""
    for lo, hi, key in WINDOWS:
        if lo <= local_hour < hi:
            return key
    return None


UNKNOWN = object()     # "we could not tell" -- distinct from "nothing heard"


def plan(cfg, history, heard_ts, ch_util, now=None, local_hour=None, rng=random.random):
    """(text|None, reason). Pure: reads nothing, writes nothing, transmits nothing.
    history   -- list of {"ts": epoch, "key": ...} for past sends, oldest first; None if the
                 record could not be read (then nothing is sent: the caps would be blind)
    heard_ts  -- epoch of the last text heard on the channel; None if the channel has never
                 carried text; UNKNOWN if the inbox could not be read
    ch_util   -- channel utilization percent from a FRESH radio status, or None if unknown"""
    now = time.time() if now is None else now
    if local_hour is None:
        local_hour = local_hour_now(cfg, now)
    if local_hour is None:
        return None, "bad_timezone"
    if history is None:
        return None, "history_unreadable"
    if str(cfg.get("RESPONDER_ENABLED", "false")).lower() != "true":
        return None, "responder_disabled"
    if str(cfg.get("PRESENCE_ENABLED", "false")).lower() != "true":
        return None, "presence_disabled"
    wkey = window(local_hour)
    if wkey is None:
        return None, "outside_windows"
    sends = [h for h in history if isinstance(h, dict) and isinstance(h.get("ts"), (int, float))]
    if sends and now - sends[-1]["ts"] < _num(cfg, "PRESENCE_MIN_GAP_H") * 3600:
        return None, "min_gap"
    if sum(1 for h in sends if now - h["ts"] < 7 * 86400) >= _num(cfg, "PRESENCE_MAX_PER_WEEK"):
        return None, "weekly_cap"
    today = _local_date(cfg, now)
    if today is None:
        return None, "bad_timezone"
    if sum(1 for h in sends if _local_date(cfg, h["ts"]) == today) >= _num(cfg, "PRESENCE_MAX_PER_DAY"):
        return None, "daily_cap"
    if heard_ts is UNKNOWN:
        return None, "channel_unknown"
    if heard_ts is not None and now - heard_ts < _num(cfg, "PRESENCE_QUIET_S"):
        return None, "channel_in_conversation"
    # Unknown utilization is a reason to wait, not a reason to transmit. NaN and negatives are
    # not measurements.
    if (not isinstance(ch_util, (int, float)) or isinstance(ch_util, bool) or ch_util != ch_util
            or ch_util < 0 or ch_util > _num(cfg, "PRESENCE_MAX_CH_UTIL")):
        return None, "channel_busy_or_unknown"
    if rng() >= _num(cfg, "PRESENCE_CHANCE"):
        return None, "not_this_time"
    last_key = sends[-1].get("key") if sends else None
    optional = ["PRESENCE_TEST"] if str(cfg.get("PRESENCE_TEST", "")).strip() else []
    choices = [k for k in [wkey, "PRESENCE_CHECK"] + optional if k != last_key]
    key = choices[int(rng() * len(choices)) % len(choices)] if choices else wkey
    text = str(cfg.get(key, DEFAULTS[key])).strip()
    if not text or len(text) > MAX_CHARS:
        return None, "bad_phrase"
    return {"key": key, "text": text}, "send"


def _load_cfg(base):
    cfg = dict(DEFAULTS)
    try:
        import re
        for line in open(os.path.join(base, "config"), encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                cfg[k.strip()] = re.split(r"\s+#", v, maxsplit=1)[0].strip()
    except OSError:
        pass
    return cfg


def _last_heard(base, channel):
    """Epoch of the newest non-reaction text on this channel; None if there is none; UNKNOWN if
    the inbox cannot be read or its newest matching record has no usable time. A missing inbox
    is unknown, not quiet (review 2026-09-24: both used to read as "quiet" and let a send through)."""
    path = os.path.join(base, "inbox.jsonl")
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 65536))
            lines = f.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return UNKNOWN
    for ln in reversed(lines):
        try:
            r = json.loads(ln)
            ch = int(r.get("channel") or 0)
        except (ValueError, TypeError, AttributeError):
            continue
        if r.get("reaction") or ch != channel:
            continue
        try:
            return datetime.fromisoformat(r["ts"]).timestamp()
        except (KeyError, ValueError, TypeError):
            return UNKNOWN
    return None


def _history(state):
    """The list of past sends, or None if the record is malformed (fail closed)."""
    h = state.get("sends", []) if isinstance(state, dict) else None
    if not isinstance(h, list) or not all(isinstance(x, dict) and isinstance(x.get("ts"), (int, float))
                                          and not isinstance(x.get("ts"), bool) for x in h):
        return None
    return h


def _fresh_util(base, cfg, now):
    """Channel utilization from status.json, only if the status is fresh. Else None."""
    try:
        st = json.load(open(os.path.join(base, "status.json")))
        age = now - datetime.fromisoformat(st["ts"]).timestamp()
        if not (0 <= age <= _num(cfg, "PRESENCE_STATUS_MAX_AGE_S")):
            return None
        return float(st["metrics"]["chUtil"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _holiday_local(cfg, now):
    """Now on HOLIDAY_TZ's clock (PRESENCE_TZ if unset), or None if the zone is unusable."""
    from zoneinfo import ZoneInfo
    try:
        tz = str(cfg.get("HOLIDAY_TZ") or cfg.get("PRESENCE_TZ", DEFAULTS["PRESENCE_TZ"]))
        return datetime.fromtimestamp(now, ZoneInfo(tz))
    except Exception:
        return None


def _announced(state, history):
    """The set of "YYYY-MM-DD:key" already announced, or None if the record is malformed."""
    if not isinstance(state, dict) or history is None:
        return None
    a = state.get("holidays", [])
    return set(a) if isinstance(a, list) and all(isinstance(x, str) for x in a) else None


def _state_for_write(state):
    """The state to write back: every key kept, but the holiday and on-this-day records are
    REPAIRED to well-formed values rather than carried forward broken (review 2026-11-26: a
    malformed "holidays" value preserved forever silenced the holiday announcement for good)."""
    rec = dict(state) if isinstance(state, dict) else {}
    for k in ("holidays", "onthisday"):
        v = rec.get(k, [])
        rec[k] = v if isinstance(v, list) and all(isinstance(x, str) for x in v) else []
    rec["onthisday_used"] = _otd_used(rec)
    return rec


def _last_tx(base, history):
    """When Cal last transmitted ANYTHING: the newest of presence's own sends and sent.jsonl
    (replies, signal reports). The two-hour gap is about the channel, not about presence."""
    best = None
    for h in history or []:
        if isinstance(h, dict) and isinstance(h.get("ts"), (int, float)) and not isinstance(h.get("ts"), bool):
            best = h["ts"] if best is None else max(best, h["ts"])
    try:
        with open(os.path.join(base, "sent.jsonl"), "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 65536))
            for ln in f.read().decode("utf-8", "replace").splitlines():
                try:
                    t = datetime.fromisoformat(json.loads(ln)["ts"]).timestamp()
                except (ValueError, KeyError, TypeError, AttributeError):
                    continue
                if t == t and t not in (float("inf"), float("-inf")):
                    best = t if best is None else max(best, t)
    except OSError:
        pass
    return best


def _posted(state, history):
    """The set of "YYYY-MM-DD" with an on-this-day post, or None if the record is malformed."""
    if not isinstance(state, dict) or history is None:
        return None
    a = state.get("onthisday", [])
    return set(a) if isinstance(a, list) and all(isinstance(x, str) for x in a) else None


def _otd_used(state):
    u = state.get("onthisday_used", {}) if isinstance(state, dict) else {}
    if not isinstance(u, dict):
        return {}
    return {k: [x for x in v if isinstance(x, str)] for k, v in u.items()
            if isinstance(k, str) and isinstance(v, list)}


def _on_this_day(cfg, state, history, heard, util, now, base, channel, send, stamp, state_path):
    """Midday, only on a date with an entry: "On this day in <year>: <event> <takeaway>"
    (ontheday.py). Same contract as _holiday_announce: it IS that day's presence post, recorded
    before the outbox write, under main's lock. Outbox source "onthisday"."""
    import ontheday as _otd
    import usholidays as _hol
    local = _holiday_local(cfg, now)
    if local is None:
        return False
    posted = _posted(state, history)
    entries = _otd.load(os.path.join(base, "data", "on_this_day.json"))
    sends = history or []
    todays, reason = _otd.plan_post(
        cfg, local.date(), local.hour, posted, "unknown" if heard is UNKNOWN else heard, util, now,
        _num(cfg, "PRESENCE_QUIET_S"), _num(cfg, "PRESENCE_MAX_CH_UTIL"),
        last_send_ts=_last_tx(base, sends),
        holiday_today=bool(_hol.on(local.date())), entries=entries)
    if not todays:
        if reason not in ("no_entry_today", "on_this_day_disabled", "already_posted",
                          "outside_midday"):
            print(f"{stamp} presence: on-this-day {reason}")
        return False
    mmdd = local.date().strftime("%m-%d")
    used = _otd_used(state)
    e = _otd.choose(todays, used.get(mmdd, []))
    text = _otd.text(e) if e else None
    if not text:
        return False
    if not send:
        print(f"{stamp} presence: would post on-this-day {text!r}; dry run")
        return True
    ids = used.get(mmdd, [])
    if _otd.entry_id(e) in ids:                      # every entry used: this starts a new round
        ids = []
    used[mmdd] = ids + [_otd.entry_id(e)]
    rec = _state_for_write(state)
    rec.update({"sends": (history + [{"ts": now, "key": "ONTHISDAY"}])[-50:],
                "onthisday": sorted(posted | {local.date().isoformat()})[-60:],
                "onthisday_used": used})
    with open(state_path + ".tmp", "w") as f:
        json.dump(rec, f)
    os.replace(state_path + ".tmp", state_path)
    outbox = os.path.join(base, "outbox")
    os.makedirs(outbox, exist_ok=True)
    name = f"onthisday.{int(now * 1000)}.{os.getpid()}.{random.randrange(10 ** 6)}"
    tmp = os.path.join(outbox, "." + name)
    with open(tmp, "w") as f:
        json.dump({"text": text, "dest": "^all", "channel": channel, "source": "onthisday"}, f)
    os.replace(tmp, os.path.join(outbox, name))
    print(f"{stamp} presence: posted on-this-day {text!r}")
    return True


def _holiday_announce(cfg, state, history, heard, util, now, base, channel, send, stamp, state_path):
    """On a holiday, once, from 9 AM local: "Happy Thanksgiving, mesh! <fact>" (usholidays.py).

    Runs ahead of the ordinary greeting and, when it sends, IS that day's presence post: it is
    written into the same history, so the daily cap keeps a "Morning Mesh" from following it, and
    while it is still pending the ordinary greeting holds off (see main). On HOLIDAY_TZ's clock.
    Outbox source is "holiday", not "presence": a reply to it is not a reply to Cal's radio check
    and must not earn a signal report. Returns True if it sent (or would have, on a dry run)."""
    import usholidays as _hol
    local = _holiday_local(cfg, now)
    if local is None:
        return False
    announced = _announced(state, history)
    sends = history or []
    key, reason = _hol.plan_announce(
        cfg, local.date(), local.hour, announced,
        "unknown" if heard is UNKNOWN else heard, util, now,
        _num(cfg, "PRESENCE_QUIET_S"), _num(cfg, "PRESENCE_MAX_CH_UTIL"),
        last_send_ts=_last_tx(base, sends))
    if not key:
        if reason not in ("no_holiday_to_announce", "holiday_announce_disabled"):
            print(f"{stamp} presence: holiday {reason}")
        return False
    if not send:
        print(f"{stamp} presence: would announce {_hol.name(key)}; dry run")
        return True
    fact = _hol.draw(key, state_path=os.path.join(base, "holiday-state.json"),
                     facts=_hol.load_facts(os.path.join(base, "data", "holiday_facts.json")))
    text = _hol.announce_text(key, fact) if fact else None
    if not text:
        print(f"{stamp} presence: holiday {key} has no usable fact; not announced")
        return False
    tag = f"{local.date().isoformat()}:{key}"
    # Recorded BEFORE the outbox, as below: a failed write skips one announcement, never repeats it.
    rec = _state_for_write(state)
    rec.update({"sends": (history + [{"ts": now, "key": "HOLIDAY"}])[-50:],
                "holidays": (sorted(announced | {tag}))[-40:]})
    with open(state_path + ".tmp", "w") as f:
        json.dump(rec, f)
    os.replace(state_path + ".tmp", state_path)
    outbox = os.path.join(base, "outbox")
    os.makedirs(outbox, exist_ok=True)
    name = f"holiday.{int(now * 1000)}.{os.getpid()}.{random.randrange(10 ** 6)}"
    tmp = os.path.join(outbox, "." + name)
    with open(tmp, "w") as f:
        json.dump({"text": text, "dest": "^all", "channel": channel, "source": "holiday"}, f)
    os.replace(tmp, os.path.join(outbox, name))
    print(f"{stamp} presence: announced {text!r}")
    return True


def main(argv=None, base=None, now=None):
    import sys
    argv = sys.argv[1:] if argv is None else argv
    base = base or os.path.dirname(os.path.abspath(__file__))
    now = time.time() if now is None else now
    cfg = _load_cfg(base)
    state_path = os.path.join(base, "presence-state.json")
    # ONE RUN AT A TIME (review 2026-11-26: two concurrent runs both announced, 20 of 20 trials).
    # The lock covers read, decide and write; a second run waits, then sees the first one's send.
    import fcntl
    try:
        lock_fd = os.open(state_path + ".lock", os.O_CREAT | os.O_RDWR, 0o644)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
    except OSError:
        print(f"{datetime.fromtimestamp(now).isoformat(timespec='seconds')} presence: lock_unavailable")
        return 0
    try:
        return _run(argv, base, now, cfg, state_path)
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)


def _run(argv, base, now, cfg, state_path):
    if os.path.exists(state_path):
        try:
            state = json.load(open(state_path))
        except (OSError, ValueError):
            state = None                      # unreadable is not "never sent" -- fail closed
    else:
        state = {}
    history = _history(state)
    channel = int(_num(cfg, "PRESENCE_CHANNEL"))
    stamp = datetime.fromtimestamp(now).isoformat(timespec="seconds")
    heard = _last_heard(base, channel)
    util = _fresh_util(base, cfg, now)
    if _holiday_announce(cfg, state, history, heard, util, now, base, channel, "--send" in argv,
                         stamp, state_path):
        return 0
    try:
        import usholidays as _hol
        local = _holiday_local(cfg, now)
        held = bool(local and local.hour < _hol.ANNOUNCE_UNTIL_H
                    and _hol.pending(cfg, local.date(), _announced(state, history)))
    except Exception:
        held = False
    if held:
        print(f"{stamp} presence: holiday announcement pending; the ordinary greeting waits")
        return 0
    if _on_this_day(cfg, state, history, heard, util, now, base, channel, "--send" in argv,
                    stamp, state_path):
        return 0
    try:
        import ontheday as _otd
        import usholidays as _hol
        local = _holiday_local(cfg, now)
        otd_held = bool(local and local.hour < _otd.WINDOW[1]
                        and _otd.pending(cfg, local.date(), _posted(state, history),
                                         holiday_today=bool(_hol.on(local.date()))))
    except Exception:
        otd_held = False
    if otd_held:
        print(f"{stamp} presence: on-this-day post pending; the ordinary greeting waits")
        return 0
    got, reason = plan(cfg, history, heard, util, now=now)
    if not got:
        print(f"{stamp} presence: {reason}")
        return 0
    if "--send" not in argv:
        print(f"{stamp} presence: would send {got['text']!r} ({got['key']}); dry run")
        return 0
    # The record is written BEFORE the outbox: if the outbox write fails we have skipped one
    # greeting, which is the quiet direction; the other order could send without remembering.
    history = history + [{"ts": now, "key": got["key"]}]
    with open(state_path + ".tmp", "w") as f:
        rec = _state_for_write(state)
        rec["sends"] = history[-50:]
        json.dump(rec, f)
    os.replace(state_path + ".tmp", state_path)
    outbox = os.path.join(base, "outbox")
    os.makedirs(outbox, exist_ok=True)
    name = f"presence.{int(now * 1000)}"
    tmp = os.path.join(outbox, "." + name)
    with open(tmp, "w") as f:
        json.dump({"text": got["text"], "dest": "^all", "channel": channel,
                   "source": "presence"}, f)
    os.replace(tmp, os.path.join(outbox, name))     # atomic: the bridge globs outbox/
    print(f"{stamp} presence: sent {got['text']!r} ({got['key']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
