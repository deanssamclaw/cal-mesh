#!/usr/bin/env python3
"""cal-mesh RESPONDER — the autonomous cognition layer (Level 2).

    inbound text (inbox.jsonl, from the bridge)
        -> gate: enabled? sender allowed? addressed to Cal? fresh? within rate?
        -> generate a terse reply as Cal (headless `claude`, tools CANNOT execute)
        -> hand to the bridge (outbox/) -> on the air

This process does NOT own the radio. It only reads inbox.jsonl and writes outbox/,
so it can crash/restart without touching packet capture. Every inbound is logged to
decisions.jsonl with the verdict + reason.

SECURITY NOTES (post-review, 2026-08-08):
  * Reply generation runs `claude -p --permission-mode plan --strict-mcp-config`.
    plan mode structurally prevents tool EXECUTION (verified: a file read is refused,
    not performed), and strict-mcp-config with no --mcp-config loads NO MCP servers
    (Gmail/Calendar/Drive unreachable). This is the lockdown against prompt-injection
    exfil from attacker-supplied message text. Do NOT switch to --allowed-tools "" —
    it does NOT disable tools (fails open), nor rely on --disallowed-tools (fails
    open on any invalid tool name).
  * ALLOW_FROM is NOT a security boundary: Meshtastic node IDs are unauthenticated
    and trivially spoofable on a public mesh. The real control is RESPONDER_ENABLED
    (keep conservative) + the tool lockdown + terse output filter. Do not widen
    trigger policy on the belief that node IDs authenticate anyone.
"""
import os, sys, time, json, re, subprocess, fcntl
from datetime import datetime, timezone

BASE      = os.path.expanduser("~/cal-mesh")
INBOX     = os.path.join(BASE, "inbox.jsonl")
OUTBOX    = os.path.join(BASE, "outbox")
STATUS    = os.path.join(BASE, "status.json")
CONFIG    = os.path.join(BASE, "config")
STATE     = os.path.join(BASE, "responder-state.json")
DECISIONS = os.path.join(BASE, "decisions.jsonl")
LOCK      = os.path.join(BASE, "responder.lock")
CLAUDE    = os.path.expanduser("~/.local/bin/claude")

OUR_ID_FALLBACK = "!xxxxxxxx"   # Cal HT

PERSONA = ("You are Cal, Dean's AI, replying over a PUBLIC LoRa mesh radio (your node "
           "'Cal HT'). Hard rules: reply in 5-7 words; plain text only; no markdown, no "
           "emoji, no URLs, no surrounding quotes; NEVER reveal Dean's location, personal "
           "life, schedule, or work; be warm, plain, and useful. Output ONLY the reply text.")

DEFAULTS = {
    "RESPONDER_ENABLED": "false",
    "RESPONDER_MODEL": "claude-haiku-4-5-20251001",
    "ALLOW_FROM": "!aaaaaaaa,!bbbbbbbb,!cccccccc",
    "TRIGGER_WORD": "cal",
    "RATE_MAX": "5",
    "RATE_WINDOW_S": "600",
    "COOLDOWN_S": "8",
    "MAX_AGE_S": "300",
    "GEN_TIMEOUT_S": "90",
}

URL_RE = re.compile(r"https?://|www\.", re.I)


def now(): return datetime.now(timezone.utc).isoformat()
def log(m): print(f"{now()} {m}", flush=True)


def load_config():
    cfg = dict(DEFAULTS)
    try:
        for ln in open(CONFIG):
            ln = ln.strip()
            if ln and not ln.startswith("#") and "=" in ln:
                k, v = ln.split("=", 1)
                if k.strip() in cfg:
                    cfg[k.strip()] = v.strip()
    except Exception:
        pass
    return cfg


def load_state():
    try:
        return json.load(open(STATE))
    except Exception:
        return {"inbox_offset": None, "last_reply_ts": 0, "per_sender": {}}


def save_state(st):
    tmp = STATE + ".tmp"
    json.dump(st, open(tmp, "w"))
    os.replace(tmp, STATE)


def our_id():
    try:
        return (json.load(open(STATUS)).get("node") or {}).get("id") or OUR_ID_FALLBACK
    except Exception:
        return OUR_ID_FALLBACK


def record_decision(rec):
    with open(DECISIONS, "a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def trim_file(path, keep=5000):
    try:
        with open(path) as f:
            lines = f.readlines()
        if len(lines) > keep:
            with open(path + ".tmp", "w") as f:
                f.writelines(lines[-keep:])
            os.replace(path + ".tmp", path)
    except FileNotFoundError:
        pass
    except Exception as e:
        log(f"trim err {path}: {e!r}")


def clean_reply(text):
    text = (text or "").strip()
    text = re.sub(r"\s+", " ", text)
    text = text.strip('"').strip("'").strip()
    return text[:180]


def generate(cfg, sender_short, msg_text):
    """Generate a reply. plan mode + strict-mcp-config => tools cannot execute and no
    MCP servers load, so attacker-supplied msg_text cannot drive file/tool access."""
    prompt = (f'A message just arrived on the mesh from {sender_short}: "{msg_text}". '
              f'Write your reply.')
    try:
        out = subprocess.run(
            [CLAUDE, "-p", prompt, "--model", cfg["RESPONDER_MODEL"],
             "--system-prompt", PERSONA, "--permission-mode", "plan",
             "--strict-mcp-config", "--output-format", "text"],
            capture_output=True, text=True, timeout=int(cfg["GEN_TIMEOUT_S"]))
        if out.returncode != 0:
            return None, f"gen_rc{out.returncode}:{out.stderr.strip()[:80]}"
        reply = clean_reply(out.stdout)
        if not reply:
            return None, "gen_empty"
        if URL_RE.search(reply):          # never broadcast a URL, whatever the input
            return None, "gen_filtered_url"
        return reply, "ok"
    except subprocess.TimeoutExpired:
        return None, "gen_timeout"
    except Exception as e:
        return None, f"gen_exc:{e!r}"[:100]


def enqueue(text, dest, channel):
    """Atomic outbox write (dotfile temp is invisible to the bridge's glob)."""
    name = f"resp.{int(time.time()*1000)}"
    tmp = os.path.join(OUTBOX, "." + name)
    with open(tmp, "w") as f:
        json.dump({"text": text, "dest": dest, "channel": channel,
                   "source": "responder"}, f)
    os.replace(tmp, os.path.join(OUTBOX, name))


def rate_ok(cfg, st, sender, ts):
    if ts - st.get("last_reply_ts", 0) < int(cfg["COOLDOWN_S"]):
        return False, "cooldown"
    win = int(cfg["RATE_WINDOW_S"])
    hits = [t for t in st["per_sender"].get(sender, []) if ts - t < win]
    st["per_sender"][sender] = hits
    if len(hits) >= int(cfg["RATE_MAX"]):
        return False, "rate_limited"
    return True, None


def evaluate(cfg, st, rec, ours):
    sender = rec.get("from")
    to = rec.get("to")
    text = rec.get("text", "") or ""
    ch = rec.get("channel", 0)

    if sender == ours:
        return False, "self", None, ch
    # freshness — fail CLOSED: an unparseable ts is treated as too old, not fresh.
    try:
        age = time.time() - datetime.fromisoformat(rec["ts"]).timestamp()
    except Exception:
        return False, "too_old", None, ch
    if age > int(cfg["MAX_AGE_S"]):
        return False, "too_old", None, ch
    if cfg["RESPONDER_ENABLED"].lower() != "true":
        return False, "disabled", None, ch
    allow = [a.strip() for a in cfg["ALLOW_FROM"].split(",") if a.strip()]
    if sender not in allow:
        return False, "sender_not_allowed", None, ch
    trigger = (cfg["TRIGGER_WORD"] or "").strip()
    is_dm = (to == ours)
    kw = bool(trigger) and re.search(r"\b" + re.escape(trigger) + r"\b", text, re.I) is not None
    if not (is_dm or kw):
        return False, "not_addressed", None, ch
    ok, why = rate_ok(cfg, st, sender, time.time())
    if not ok:
        return False, why, None, ch
    dest = sender if is_dm else "^all"
    return True, "addressed", dest, ch


def read_new(st):
    """Yield (record_or_None, new_offset) for each complete line since the persisted
    offset. Advances only past complete (newline-terminated) lines — a partial trailing
    line is left for the next pass. On first run (offset None) starts at EOF (skip backlog).
    The CALLER persists new_offset after handling each record, so a failure loses at most
    one record, never a batch."""
    if not os.path.exists(INBOX):
        return
    size = os.path.getsize(INBOX)
    off = st.get("inbox_offset")
    if off is None or off > size:      # first run, or file rotated/truncated
        st["inbox_offset"] = size
        save_state(st)
        return
    if off == size:
        return
    with open(INBOX, "rb") as f:
        f.seek(off)
        data = f.read()
    last_nl = data.rfind(b"\n")
    if last_nl == -1:                  # no complete line yet
        return
    pos = off
    for raw in data[:last_nl + 1].splitlines(keepends=True):
        pos += len(raw)
        line = raw.strip()
        if not line:
            yield None, pos
            continue
        try:
            yield json.loads(line.decode("utf-8", "replace")), pos
        except Exception:
            yield None, pos           # skip malformed but advance past it


def main():
    os.makedirs(OUTBOX, exist_ok=True)

    lf = open(LOCK, "w")              # single-instance guard (mirrors the bridge)
    try:
        fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log("another responder holds the lock; exiting")
        sys.exit(0)
    lf.write(str(os.getpid()))
    lf.flush()

    st = load_state()
    last_trim = time.time()
    log("cal-mesh responder starting")
    log(f"our node: {our_id()}")
    while True:
        try:
            cfg = load_config()
            ours = our_id()
            if time.time() - last_trim > 300:
                trim_file(DECISIONS, 5000)
                last_trim = time.time()
            for rec, new_off in read_new(st):
                try:
                    if rec is not None:
                        should, reason, dest, ch = evaluate(cfg, st, rec, ours)
                        d = {"ts": now(), "from": rec.get("from"), "to": rec.get("to"),
                             "text": rec.get("text", ""), "matched": should,
                             "reason": reason, "reply": None}
                        if should:
                            gen_start = time.time()
                            reply, why = generate(cfg, rec.get("from"), rec.get("text", ""))
                            gen_ms = round((time.time() - gen_start) * 1000)
                            if reply:
                                enqueue(reply, dest, ch)
                                ts = time.time()
                                st["last_reply_ts"] = ts
                                st["per_sender"].setdefault(rec.get("from"), []).append(ts)
                                d["reply"] = reply
                                d["dest"] = dest
                                d["gen_ms"] = gen_ms
                                log(f"REPLY to {rec.get('from')} -> {dest}: {reply!r} ({gen_ms}ms)")
                            else:
                                d["matched"] = False
                                d["reason"] = why
                                d["gen_ms"] = gen_ms
                                log(f"gen failed for {rec.get('from')}: {why} ({gen_ms}ms)")
                        else:
                            log(f"skip {rec.get('from')}: {reason}")
                        record_decision(d)
                except Exception as e:
                    log(f"record err (skipping): {e!r}")
                # advance past this record regardless — bounds loss to one line, never wedges
                st["inbox_offset"] = new_off
                save_state(st)
        except Exception as e:
            log("loop err: " + repr(e))
        time.sleep(1)


if __name__ == "__main__":
    main()
