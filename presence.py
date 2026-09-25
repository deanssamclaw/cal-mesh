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
    "PRESENCE_MIN_GAP_H": "36",
    "PRESENCE_CHANCE": "0.08",
    "PRESENCE_QUIET_S": "900",
    "PRESENCE_MAX_CH_UTIL": "25",
    "PRESENCE_CHANNEL": "0",
    "PRESENCE_MORNING": "Morning Mesh",
    "PRESENCE_AFTERNOON": "Afternoon Mesh",
    "PRESENCE_EVENING": "Evening Mesh",
    "PRESENCE_CHECK": "Radio check. Anyone out there?",
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
    choices = [k for k in (wkey, "PRESENCE_CHECK") if k != last_key]
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


def main(argv=None, base=None, now=None):
    import sys
    argv = sys.argv[1:] if argv is None else argv
    base = base or os.path.dirname(os.path.abspath(__file__))
    now = time.time() if now is None else now
    cfg = _load_cfg(base)
    state_path = os.path.join(base, "presence-state.json")
    if os.path.exists(state_path):
        try:
            state = json.load(open(state_path))
        except (OSError, ValueError):
            state = None                      # unreadable is not "never sent" -- fail closed
    else:
        state = {}
    history = _history(state)
    channel = int(_num(cfg, "PRESENCE_CHANNEL"))
    got, reason = plan(cfg, history, _last_heard(base, channel), _fresh_util(base, cfg, now), now=now)
    stamp = datetime.fromtimestamp(now).isoformat(timespec="seconds")
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
        json.dump({"sends": history[-50:]}, f)
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
