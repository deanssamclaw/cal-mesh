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
  * PRIVATE DATA IS KEPT OUT OF CONTEXT (adversarial review 2026-08-09): the `claude`
    CLI otherwise auto-loads ~/.claude/CLAUDE.md (which names Dean's state). run_claude
    passes --setting-sources "" so NO CLAUDE.md/settings load, making that data ABSENT
    rather than persona-guarded. This is what actually stops on-air exfil of Dean's
    location under prompt injection — the persona line is only a backstop.
"""
import os, sys, time, json, re, subprocess, fcntl, unicodedata
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

sys.path.insert(0, BASE)
import weather                    # Level 3 Stage 1 capability (harness-fetched, injected)

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
    # --- Level 3 Stage 1: weather capability (default OFF) ---
    "WEATHER_ENABLED": "false",
    "WEATHER_POINT": "",            # 'lat,lon' default public reference point (set at arm time)
    "WEATHER_PLACES": "",           # optional whitelist: 'name:lat,lon;name2:lat,lon'
    "WEATHER_UA": "cal-mesh/1.0 (github.com/deanssamclaw/cal-mesh)",
    "WEATHER_TIMEOUT_S": "4",       # per-request; station is cached so steady state is 1 GET
}

URL_RE = re.compile(r"https?://|www\.|\b[a-z0-9-]+\.[a-z]{2,}\b", re.I)  # incl. schemeless domains

# --- inbound sanitization (defense-in-depth vs prompt injection; Bob's review) ---
# The inbound message is attacker-controllable (node IDs are spoofable). Before it EVER
# enters the generation prompt we (1) keep only the first sentence/line — mesh queries are
# terse — and (2) neutralize instruction/exfil-shaped tokens. This is not the primary
# defense (tool-lockdown is) but it raises the bar and matters more as agency grows.
_SENT_END  = re.compile(r"[.!?\n]")
_INJECT_RE = re.compile(
    r"\b(ignore|disregard|forget|override|overrule|instead|reveal|exfiltrat\w*|"
    r"system\s+prompt|previous\s+instructions|prior\s+instructions|delete|remove|"
    r"password|passwd|credential\w*|secret\w*|token|api[\s_-]?key|ssh|id_rsa|"
    r"\.env|/etc/|~/\.)\b", re.I)


def _normalize(s):
    """NFKC-normalize and drop control/format chars (e.g. zero-width spaces used to split
    denylisted tokens), keeping newlines. Defeats the unicode-bypass of the redaction."""
    s = unicodedata.normalize("NFKC", s or "")
    return "".join(c for c in s if c == "\n" or (ord(c) >= 32 and unicodedata.category(c)[0] != "C"))


def sanitize_inbound(text):
    """Reduce an attacker-controllable message to a safe query subject.
    Returns (clean_text, flagged). Defense-in-depth only — NOT the primary control (that's the
    tool lockdown + keeping private data out of context via --setting-sources). Normalizes
    unicode, keeps the first sentence, redacts instruction/exfil-shaped tokens."""
    norm = _normalize(text)
    t = norm.strip()
    m = _SENT_END.search(t)
    if m and m.start() > 0:
        t = t[:m.start()].strip()
    t = t[:120]
    flagged = bool(_INJECT_RE.search(norm))
    clean = _INJECT_RE.sub("[redacted]", t).strip()
    return clean, flagged


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


def build_prompt(sender_short, msg_text, weather_fact=None):
    """PURE: the user-turn prompt handed to the tool-locked model. msg_text must already be
    sanitized. On the weather path the attacker's message is NOT echoed at all — the model
    sees only the harness-fetched fact, so there is no injected text (or fake number) beside it."""
    if weather_fact:
        return (f'A user on the mesh asked about the weather. '
                f'Current local weather, from a trusted source: {weather_fact}. '
                f'Reply with the weather in 5-7 words using ONLY this data. '
                f'If a needed value is missing, say you cannot reach weather.')
    return (f'A message just arrived on the mesh from {sender_short}: "{msg_text}". '
            f'Write your reply.')


def plan_response(cfg, sender_short, raw_text, get=None):
    """PURE decision (no subprocess, no I/O beyond the injectable weather fetch): sanitize the
    inbound, run any capability, and decide whether we emit a FIXED fail-safe reply or a
    GENERATE prompt. Separated from side effects so the whole path is offline-testable."""
    clean, flagged = sanitize_inbound(raw_text)
    out = {"clean": clean, "flagged": flagged, "capability": None, "weather_ok": None,
           "weather_fact": None, "mode": "generate", "fixed_reply": None, "prompt": None}
    fact = None
    # intent/location on the RAW text: a trailing '?' (needed for weak-keyword intent) and a
    # whitelisted place name must survive; sanitize would strip them. Nothing from raw_text
    # reaches a URL (resolve_location whitelists) or the weather prompt (build_prompt drops it).
    if cfg.get("WEATHER_ENABLED", "false").lower() == "true" and \
       weather.wants_weather(raw_text):
        out["capability"] = "weather"
        _, latlon = weather.resolve_location(cfg, raw_text)
        fact = weather.fetch_current(cfg, latlon, get=get) if get is not None \
            else weather.fetch_current(cfg, latlon)
        out["weather_ok"], out["weather_fact"] = fact is not None, fact
        if fact is None:                       # fail-safe: NEVER invent weather from the text
            out["mode"], out["fixed_reply"] = "fixed", "Can't reach weather right now."
            return out
    out["prompt"] = build_prompt(sender_short, clean, fact)
    return out


def run_claude(cfg, prompt):
    """SIDE EFFECT: run the tool-locked model on a prompt.
      * --permission-mode plan + --strict-mcp-config => tools cannot execute, no MCP loads.
      * --setting-sources "" => load NO user/project/local settings, so Dean's global
        ~/.claude/CLAUDE.md ("Dean is in Kansas...") is NOT in context. Private data is
        ABSENT, not merely persona-guarded — verified: the neutral-prompt "what state is
        Dean in" returns NONE with this flag, "Kansas" without it. Auth is unaffected (Keychain).
      * --exclude-dynamic-system-prompt-sections => strip cwd/env/paths/git from context.
    Do NOT drop --setting-sources: it is the structural control against on-air exfil."""
    try:
        out = subprocess.run(
            [CLAUDE, "-p", prompt, "--model", cfg["RESPONDER_MODEL"],
             "--system-prompt", PERSONA, "--permission-mode", "plan",
             "--strict-mcp-config", "--setting-sources", "",
             "--exclude-dynamic-system-prompt-sections", "--output-format", "text"],
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
                            plan = plan_response(cfg, rec.get("from"), rec.get("text", ""))
                            if plan["flagged"]:
                                d["injection_flagged"] = True
                            if plan["capability"]:
                                d["capability"] = plan["capability"]
                                d["weather_ok"] = plan["weather_ok"]
                            gen_start = time.time()
                            if plan["mode"] == "fixed":
                                reply, why = plan["fixed_reply"], "ok_weather_unavailable"
                            else:
                                reply, why = run_claude(cfg, plan["prompt"])
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
