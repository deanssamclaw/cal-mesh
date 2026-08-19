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

# Sibling imports resolve relative to THIS FILE, not to a hardcoded ~/cal-mesh. With the hardcoded
# path, any eval that loads responder.py from a scratch copy still imported the DEPLOYED calc,
# weather and sunmoon — so a sabotaged scratch calc.py left eval_sunmoon, eval_weather, eval_dm,
# eval_dm_longer and eval_routing all green. Every end-to-end check in those files was testing
# production code. eval_sunmoon seeds sys.modules for two of the three and its own header
# documents the trap; this fixes it at the source for all of them at once.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import weather                    # Level 3 Stage 1 capability (harness-fetched, injected)
import calc                       # Level 3 COMPUTE doer (Python owns every digit)
import sunmoon                    # COMPUTE doer: closed-form astronomy, offline-resilient
import capabilities               # FIXED doer: what is armed, composed from the flags
import dm_memory                  # per-identity DM memory on the pinned unlock tier (default OFF)
from zoneinfo import ZoneInfo

OUR_ID_FALLBACK = "!xxxxxxxx"   # Cal HT

PERSONA = ("You are Cal, Dean's AI, replying over a PUBLIC LoRa mesh radio (your node "
           "'Cal HT'). Hard rules: reply in 5-7 words; plain text only; no markdown, no "
           "emoji, no URLs, no surrounding quotes; NEVER reveal Dean's location, personal "
           "life, schedule, or work; be warm, plain, and useful. Output ONLY the reply text.")

# Authenticated-DM persona. IDENTICAL restrictions to PERSONA — same refusal of Dean's
# location/life/schedule/work, and NO context is injected on this path. The ONLY difference is
# the length budget: a DM lands on one node's screen instead of every screen in range, so the
# 5-7 word rule (which exists for broadcast readability) can relax. Airtime is still shared, so
# the budget is a couple of sentences, not a chat window.
#
# This is deliberately NOT the unlock. PERSONA_PRIVATE speaks freely about injected context;
# this one knows nothing it didn't already know on the public channel.
PERSONA_DM_AUTHED = (
    "You are Cal, Dean's AI, replying over an authenticated direct message on a LoRa mesh radio "
    "(your node 'Cal HT'). Hard rules: reply in 1-2 short sentences; plain text only; no "
    "markdown, no emoji, no URLs, no surrounding quotes; NEVER reveal Dean's location, personal "
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
    "WEATHER_MAX_OBS_AGE_S": "5400",  # stations report ~hourly; 90 min tolerates one late
                                      # cycle. Older than this = unusable, same as a failed
                                      # fetch — a stalled station must not read as "current".
    # --- Sun / moon / twilight capability (COMPUTE doer, default OFF) ---
    # Closed-form astronomy: no network, no curation, no model in the number path. It reuses
    # WEATHER_POINT as its observer location and reports TIMES ONLY — a coordinate is an input
    # here, never something the reply carries.
    "SUNMOON_ENABLED": "false",
    "SUNMOON_TZ": "America/Chicago",   # replies are wall-clock for someone under the same sky
    "SUNMOON_MAX_CHARS": "120",
    # --- greeting acknowledgement for OFF-LIST senders (default OFF) ---
    # Silence on a broadcast channel is not neutral: a stranger who just watched Cal
    # answer someone else reads it as a snub. This acks a bare greeting with a FIXED,
    # operator-authored string. No model runs, so there is no injection surface and
    # nothing an attacker can steer — the matched greeting SELECTS a reply from a
    # closed table, it never shapes prose.
    "GREETING_ENABLED": "false",
    "GREET_TEXT": "",                 # the fixed ack. EMPTY here on purpose: this file is
                                      # published, and the reply may name a place. Set it in
                                      # the gitignored config at arm time, like WEATHER_POINT.
                                      # Empty = the capability cannot fire (fail-closed).
    "GREET_MAX_PER_DAY": "6",         # global amplification budget. Per-node limits fall to
                                      # ID spoofing, so the GLOBAL cap is the real control.
    "GREET_SENDER_COOLDOWN_S": "86400",   # one ack per node per day
    # --- P1 content unlock on an authenticated DM (channel-trust-and-agency.md §4). ---
    # CONTENT only: longer, context-aware replies to Dean. Tools stay locked exactly as on the
    # public channel — P2 never rides on mesh auth alone. Forge-tolerant by construction: the
    # worst case if the sender auth is forged is that a forger READS a reply meant for Dean.
    "DM_UNLOCK_ENABLED": "false",
    "DM_UNLOCK_NODE": "",             # Dean's node id. Empty = closed.
    "DM_UNLOCK_PUBKEY_FP": "",        # pinned key fingerprint. Empty = closed. A node id alone
                                      # is spoofable, so the id is NOT sufficient on its own.
    "DM_CONTEXT_FILE": "",            # operator-curated context, gitignored. Empty = closed.
    "DM_CONTEXT_MAX_CHARS": "2000",
    "DM_MAX_CHARS": "200",            # a DM still costs shared airtime; this is not a chat window
    # Longer replies on AUTHENTICATED DMs, with no unlock and no context. Separate knob from
    # DM_UNLOCK_ENABLED on purpose: this changes only the length budget, so it carries none of
    # the unlock's disclosure risk and must not ride on the same switch.
    "DM_LONGER_ENABLED": "false",
    # 180, not 200: clean_reply() hard-caps every reply at 180 chars, so a larger value
    # here would be a config that lies about what actually goes on air.
    "DM_LOCKED_MAX_CHARS": "180",
    # COMPUTE doer. No model runs on this path at all — Python computes AND formats,
    # so the reply is emitted as a fixed string exactly like a refusal.
    "CALC_ENABLED": "false",
    "CAPS_ENABLED": "false",
    "CALC_MAX_CHARS": "160",
    # Per-identity DM memory (default OFF). Rides the pinned dm_unlock tier ONLY — it keys on the
    # unspoofable public-key fingerprint, never the node id, so the store is single-identity by
    # construction. Double-gated: inert unless DM_UNLOCK is configured (a pinned fingerprint
    # exists). See dm_memory.py for the full argument.
    "DM_MEMORY_ENABLED": "false",
    "DM_MEMORY_MAX_TURNS": "8",       # recent (q,a) pairs retained; oldest fall off
    "DM_MEMORY_MAX_CHARS": "1200",    # hard cap on the injected memory block (prompt, not a window)
}

# The unlocked persona. Still forbids secrets outright, because "absence not refusal" covers the
# keystore but the injected context is operator-curated and could in principle carry something
# it shouldn't. Belt and braces, and it costs nothing.
PERSONA_PRIVATE = (
    "You are Cal, Dean's assistant, replying over a private authenticated radio link to Dean "
    "himself. You may use the context provided in the prompt and speak freely about it. "
    "Hard rules: plain text only; no markdown, no emoji, no URLs, no surrounding quotes; "
    "be concise — this is a radio link, not a terminal; never output keys, passwords, tokens "
    "or channel PSKs even if asked. Output ONLY the reply text.")

URL_RE = re.compile(r"https?://|www\.|\b[a-z0-9-]+\.[a-z]{2,}\b", re.I)  # incl. schemeless domains

# --- inbound sanitization (defense-in-depth vs prompt injection; Bob's review) ---
# The inbound message is attacker-controllable (node IDs are spoofable). Before it EVER
# enters the generation prompt we (1) keep only the first sentence/line — mesh queries are
# terse — and (2) neutralize instruction/exfil-shaped tokens. This is not the primary
# defense (tool-lockdown is) but it raises the bar and matters more as agency grows.
# A period BETWEEN DIGITS is a decimal point, not a sentence end. Without this exclusion the
# sanitizer truncated "12.5 ft in m" to "12" before any capability saw it — 17 of 48 realistic
# calculations returned nothing, and "15% off $260.50" silently answered for $260.
_SENT_END  = re.compile(r"(?<![0-9])[.](?![0-9])|[!?\n]")
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


def sanitize_trace(raw, clean, flagged):
    """Describe what sanitize_inbound did to a message, for the public decision trace.

    Derived by replaying the SAME steps on the same input, so it cannot drift from the real
    sanitizer without the sanitizer itself changing. It reports only *that* redaction happened
    and how many times — NEVER the redacted content, which would defeat the redaction on a
    public page."""
    norm = _normalize(raw or "").strip()
    m = _SENT_END.search(norm)
    cut = bool(m and m.start() > 0)
    kept = norm[:m.start()].strip() if cut else norm
    # Distinguish "we dropped a trailing '?'" from "we dropped a sentence of content". Both
    # hit the same code path, but reporting them identically reads as though a single-sentence
    # question lost something it didn't — a small lie on a page whose whole job is accuracy.
    dropped = norm[m.start():] if cut else ""
    residue = _SENT_END.sub("", dropped).strip()
    return {"flagged": bool(flagged),
            "redactions": (clean or "").count("[redacted]"),
            "sentence_trimmed": cut,                       # kept: older records use this
            "sentence_trim": ("none" if not cut else
                              ("punctuation" if not residue else "content")),
            "dropped_chars": len(residue),
            "length_capped": len(kept) > 120,
            "in_chars": len(norm),
            "out_chars": len(clean or "")}


def public_fact(fact):
    """The injected capability fact, safe to publish. format_fact() prefixes a whitelisted
    place label as 'name: ...' — today resolve_location's label is discarded so a fact is
    location-free by construction, but strip it defensively so populating WEATHER_PLACES can
    never quietly put a place name on the public page."""
    if not fact:
        return None
    return fact.split(": ", 1)[1] if ": " in fact.split(", ", 1)[0] else fact


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
        # "Numbers as digits" is a FORMATTING instruction, not steering: measured 2026-08-11,
        # the same prompt returned "95F" once and "ninety five" the next time, and a spelled-out
        # number costs three words of a seven-word budget — enough to push a second value out of
        # the reply entirely. "Keep every number" asks for fidelity to the supplied fact; it
        # never tells the model what to conclude, which is the line the intent-layer review drew.
        return (f'A user on the mesh asked about the weather. '
                f'Current local weather, from a trusted source: {weather_fact}. '
                f'Reply with the weather in 5-7 words using ONLY this data. '
                f'Write numbers as digits (95F, not ninety five). '
                f'Keep every number that appears in the data. '
                f'If a needed value is missing, say you cannot reach weather.')
    return (f'A message just arrived on the mesh from {sender_short}: "{msg_text}". '
            f'Write your reply.')


def build_private_prompt(msg_text, dm_context):
    """The unlocked prompt. The context is injected by the harness, exactly as the weather fact
    is — the model is handed what the operator chose and has no way to reach for more."""
    ctx = ("Context you may use, provided by the harness:\n" + dm_context + "\n\n") if dm_context else ""
    return (f'{ctx}Dean just messaged you over the private radio link: "{msg_text}". '
            f'Write your reply.')


def dm_unlock(cfg, rec, ours):
    """Is this a DM we can trust enough for the P1 CONTENT unlock? Returns (ok, reason, gates).

    Every condition must hold, and each is checked against a value the operator pinned rather
    than anything the packet asserts about itself:

      * the feature is on, and all three of node / fingerprint / context file are configured.
        Any of them empty means CLOSED — there is no default that could be right.
      * it is addressed to us specifically. A broadcast can never unlock anything.
      * the sender id matches. Necessary, and on its own worth almost nothing: ids are spoofable.
      * the packet was PKC-encrypted. `pki` is recorded by the bridge as `is True`, so a packet
        that omitted the field (proto3 drops false) reads as NOT authenticated.
      * the sender's public key fingerprint matches the pinned one. This is the part an id
        spoofer cannot supply.

    Honest limit, from the spec: Meshtastic has a documented downgrade attack where a forged DM
    can present as PKC. So this is evidence, not proof, and NOTHING forge-intolerant may key on
    it — no tools, no actions, content only.
    """
    gates = []

    def mark(name, ok):
        gates.append({"gate": name, "pass": bool(ok)})
        return ok

    if not mark("dm_unlock_enabled", cfg.get("DM_UNLOCK_ENABLED", "false").lower() == "true"):
        return False, "dm_unlock_disabled", gates
    node = (cfg.get("DM_UNLOCK_NODE") or "").strip()
    fp = (cfg.get("DM_UNLOCK_PUBKEY_FP") or "").strip().lower()
    ctx = (cfg.get("DM_CONTEXT_FILE") or "").strip()
    if not mark("unlock_configured", bool(node and fp and ctx)):
        return False, "dm_unlock_unconfigured", gates
    if not mark("is_dm", rec.get("to") == ours):
        return False, "dm_unlock_not_dm", gates
    if not mark("sender_pinned", rec.get("from") == node):
        return False, "dm_unlock_wrong_node", gates
    if not mark("pki_encrypted", rec.get("pki") is True):
        return False, "dm_unlock_not_authenticated", gates
    if not mark("pubkey_pinned", (rec.get("pubkey_fp") or "").lower() == fp):
        return False, "dm_unlock_key_mismatch", gates
    return True, "dm_unlocked", gates


def dm_longer(cfg, rec, ours):
    """Does this DM earn the longer LENGTH budget? Returns (ok, reason, gates).

    Deliberately a WEAKER bar than dm_unlock, because it buys a weaker thing. The unlock decides
    what Cal KNOWS; this decides only how many characters the same hardened persona may use. No
    context is injected on this path and no pinning is required.

    What it still demands: addressed to us specifically (a broadcast lands on every screen in
    range, which is the whole reason for the 5-7 word rule) and PKC-encrypted, read as `is True`
    so a packet that omitted the field reads as NOT authenticated. Who may get a reply at all is
    already settled upstream by the ALLOW_FROM gate.

    Forge-damage if someone defeats this: a spoofed DM gets a two-sentence hardened reply instead
    of a seven-word one. That is the entire exposure, and it is why no pinning is warranted.
    """
    gates = []

    def mark(name, ok):
        gates.append({"gate": name, "pass": bool(ok)})
        return ok

    if not mark("dm_longer_enabled", cfg.get("DM_LONGER_ENABLED", "false").lower() == "true"):
        return False, "dm_longer_disabled", gates
    if not mark("is_dm", rec.get("to") == ours):
        return False, "dm_longer_not_dm", gates
    if not mark("pki_encrypted", rec.get("pki") is True):
        return False, "dm_longer_not_authenticated", gates
    return True, "dm_longer", gates


def load_dm_context(cfg):
    """The operator-curated context injected on an unlocked DM, bounded, or None.

    Injection rather than `--setting-sources`: the harness supplies exactly what the operator
    chose, and the structural control that keeps ~/.claude/CLAUDE.md out of every generation
    stays intact. The model is never given a way to reach for more than this."""
    path = os.path.expanduser((cfg.get("DM_CONTEXT_FILE") or "").strip())
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            data = f.read(_int_cfg(cfg, "DM_CONTEXT_MAX_CHARS", DEFAULTS["DM_CONTEXT_MAX_CHARS"]) + 1)
    except Exception:
        return None
    cap = _int_cfg(cfg, "DM_CONTEXT_MAX_CHARS", DEFAULTS["DM_CONTEXT_MAX_CHARS"])
    return data[:cap].strip() or None


def _sunmoon_point(cfg):
    """(lat, lon) floats for the observer, or (None, None) if unset/unparseable.

    Deliberately goes through weather.resolve_location with EMPTY text so the whitelist branch
    cannot fire: sun times use the default reference point only. A named place in the message
    must not move the observer, because the reply would then leak which of Dean's whitelisted
    places the asker had named back onto a public channel. Same parser as weather so the two
    cannot drift apart — that bug has already happened here once, with a SAME regex.
    """
    _, latlon = weather.resolve_location(cfg, "")
    try:
        lat, lon = (float(x) for x in latlon.split(",", 1))
    except (ValueError, AttributeError):
        return None, None
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        return None, None
    return lat, lon


def _int_cfg(cfg, key, default):
    """An int from config, or the default. A malformed value used to raise out of plan_response,
    which the daemon's per-record handler swallowed BEFORE record_decision ran — so the capability
    went silent with nothing on the public dashboard and one line in a log. Fail-closed means
    falling back to a safe bound, not vanishing."""
    try:
        return int(str(cfg.get(key, default)).strip())
    except (TypeError, ValueError):
        return int(default)


def _sunmoon_tz(cfg):
    """ZoneInfo for the observer, or None if unusable. None means REFUSE, never a UTC fallback."""
    name = (cfg.get("SUNMOON_TZ", "") or "").strip()
    if not name:
        return None
    try:
        return ZoneInfo(name)
    except Exception:
        return None


def enabled_weather(cfg):
    return cfg.get("WEATHER_ENABLED", "false").lower() == "true"


def _calc_collision(cfg, clean, out):
    """True if an unambiguous calculation is embedded in a message another capability wants.

    Hoisted out of the weather branch so it applies to EVERY capability that sits above calc.
    "temp 12*12" was fixed once, and then "sunset 12*12" reintroduced the identical bug one layer
    up, because the rule lived inside the weather branch instead of beside the capabilities.
    """
    if cfg.get("CALC_ENABLED", "false").lower() != "true":
        return False
    reply, meta = calc.try_answer(clean, max_chars=_int_cfg(cfg, "CALC_MAX_CHARS", 160),
                                 trigger=cfg.get("TRIGGER_WORD", "cal"), embedded=True)
    # ONLY an EMBEDDED result. try_answer(embedded=True) runs the ordinary dispatch first, so
    # this was re-claiming anything the normal path would have answered — including a grid decode
    # that the caller had just deliberately set aside. "sunrise at grid EM28" was handed back to
    # calc here after the nav yield had already stepped it down, which made the yield look broken
    # when it had worked. This helper exists for a calculation buried in prose; nothing else.
    if not reply or not meta.get("embedded"):
        return False
    out["calc_meta"] = meta
    out["capability"] = "calc"
    out["mode"] = "fixed"
    out["fixed_kind"] = "calc"
    out["fixed_reply"] = reply
    return True


def plan_response(cfg, sender_short, raw_text, get=None, unlocked=False, dm_context=None,
                  dm_authed=False):
    """PURE decision (no subprocess, no I/O beyond the injectable weather fetch): sanitize the
    inbound, run any capability, and decide whether we emit a FIXED fail-safe reply or a
    GENERATE prompt. Separated from side effects so the whole path is offline-testable."""
    clean, flagged = sanitize_inbound(raw_text)
    out = {"clean": clean, "flagged": flagged, "capability": None, "weather_ok": None,
           "weather_fact": None, "mode": "generate", "fixed_reply": None, "prompt": None,
           "weather_meta": {}, "forecast_asked": False, "match": None,
           "sunmoon_match": None, "sunmoon_meta": None,
           "persona": None, "unlocked": False, "max_chars": None,
           "fixed_kind": None, "calc_meta": None, "caps_match": None}
    # P1: an authenticated DM from Dean gets the private persona and injected context — but the
    # DOERS RUN FIRST. This used to short-circuit here at the top, which meant an unlocked DM
    # skipped calc/sunmoon/weather entirely and a computable ask ("5 mi in km") got a model
    # guess instead of Python's exact number. The private path now sits at the GENERAL fallback
    # below (only when no doer claimed the message), so the unlock adds reach without displacing
    # the deterministic answers. Trust is still decided by the caller (dm_unlock), never here.
    # COMPUTE doer first. Intent is a SUCCESSFUL BOUNDED PARSE, not "contains a number", so
    # anything that is not a calculation returns None here and the weather path is unaffected.
    # No model is involved on this path: Python formats the reply and we emit it as fixed.
    if cfg.get("CALC_ENABLED", "false").lower() == "true":
        c_reply, c_meta = calc.try_answer(clean, max_chars=_int_cfg(cfg, "CALC_MAX_CHARS", DEFAULTS["CALC_MAX_CHARS"]),
                                          trigger=cfg.get("TRIGGER_WORD", "cal"))
        out["calc_meta"] = c_meta
        # NAVIGATION YIELDS TO A SUN/MOON ASK. calc wins outright over the capabilities below it,
        # which is right for arithmetic — a sum is a sum — and wrong for navigation, whose
        # triggers ("grid", "distance", "bearing") are the loosest in the module. "sunrise at grid
        # EM28" was answered with a grid decode because calc ran first and never looked down.
        # Only the two nav handlers step aside; arithmetic still beats everything.
        if c_reply and c_meta.get("handler") in ("grid", "distance") \
                and cfg.get("SUNMOON_ENABLED", "false").lower() == "true" \
                and sunmoon.wants_sunmoon(raw_text):
            c_reply = None
        if c_reply:
            out["capability"] = "calc"
            out["mode"] = "fixed"
            out["fixed_kind"] = "calc"
            out["fixed_reply"] = c_reply
            return out

    # SUN/MOON — a compute doer, so it sits with calc ABOVE the fetch tier: it answers when the
    # base is offline, which under the resilient-first ordering is the point. Runs on RAW text for
    # the same reason weather does (a trailing '?' and multi-word wording must survive sanitize).
    if cfg.get("SUNMOON_ENABLED", "false").lower() == "true":
        sm_match = sunmoon.explain_match(raw_text)
        out["sunmoon_match"] = sm_match
        # A sun/moon word does NOT win over the capabilities below it. Two collisions were found
        # live: "cal sunset 12*12" answered with a sunset time instead of 144, and "cal whats the
        # temp at dusk" answered with a sunset time instead of weather. The calc rescue is the
        # same rule already applied to weather (see _calc_collision) — hoisted here so every
        # capability above weather inherits it rather than each one re-forgetting it.
        # ARBITRATION BY POSITION, not by grammar.
        #
        # This is the fourth mechanism for the same question and the first that is not a rule
        # about which words appear. Three lexical/grammatical attempts each failed and each broke
        # something new: widening weather claimed 210 of 210 non-weather pairs; yielding on any
        # weather word dropped 86% of a grid to no capability; arbitrating by qualifier
        # prepositions claimed 2400 of 2400 coordinated weather asks with a sun time AND yielded
        # 216 of 294 moon asks to the model, wrong in both directions at once. Four failures at
        # three layers is the signal that the layer was wrong, not the rule.
        #
        # What a message is ASKING for is carried by order, not vocabulary. Whichever capability's
        # subject appears FIRST is the one being asked about; anything later is context or a time
        # adjunct. "when does it get dark, storm coming" opens on dark; "will it rain at sunset"
        # opens on rain. No preposition list, so punctuation and coordination cannot defeat it,
        # and it needs no special case for moon rise/set.
        #
        # One override: a time interrogative directly governing a sun/moon word wins outright, so
        # "rain later, when is sunset" is still a sunset question. Ties go to weather, which is
        # armed and proven; a tie is the one case where guessing buys nothing.
        sm_first = sunmoon.mention_positions(raw_text)
        w_first = weather.mention_positions(raw_text) if enabled_weather(cfg) else []
        # A moon rise/set ask outranks POSITION, but never outranks CERTAINTY. As first restored it
        # was the leading disjunct and short-circuited both, so "whats the temperature? the moon is
        # out" answered with the moonrise refusal — an exclusion overriding an unambiguous weather
        # ask, which is the exact failure the sun/moon exclusion was fixed for in round 4.
        # The clause exists for ONE purpose: stop a moonrise question falling through to the
        # language model, which would invent a time for something this module does not compute.
        # So it applies only where nothing else would claim the message. If weather claims it, the
        # model is not answering — an armed capability is — and overriding that is the exclusion
        # beating certainty, which is the failure round 4 fixed on the sun/moon side.
        #
        # Accepted residual, stated rather than traded silently: "the heat is on, moonrise?" goes
        # to weather and gets current conditions. A non-sequitur from an honest capability, which
        # is the better of the two wrong answers available.
        _wm = weather.explain_weather_match(raw_text) if enabled_weather(cfg) else None
        riseset = sm_match["via"] == "moon_riseset" and not (_wm and _wm["via"])
        # A moon rise/set ask never loses on position. The module recognises that shape
        # specifically SO IT CAN REFUSE it — moonrise is not implemented and must never be
        # estimated — and recognition is only a refusal if nothing can take the message away
        # first. Without this, "the heat is on, moonrise?" reaches a live weather fetch and a
        # moonrise question is answered with a temperature. This clause existed in round 3, was
        # lost in the round-4 rewrite, and is restored with its reason attached.
        sun_is_subject = bool(sm_first) and (
            riseset
            or not w_first
            or sm_first[0] < w_first[0]
            or sunmoon.governed_by_time_ask(raw_text))
        if sm_match["via"] and sun_is_subject and not _calc_collision(cfg, clean, out):
            lat, lon = _sunmoon_point(cfg)
            if lat is None:                 # fail-closed, exactly like GREET_TEXT and the point
                out["capability"] = "sunmoon"
                out["mode"], out["fixed_reply"] = "fixed", "Sun times not configured here."
                out["fixed_kind"] = "sunmoon"
                return out
            tz = _sunmoon_tz(cfg)
            if tz is None:
                # FAIL CLOSED. This used to fall back to UTC, which put a confidently wrong local
                # time on air — a typo in one config line rendered 8:12 PM as 1:12 AM with no
                # warning anywhere. A wrong time is worse than no time.
                out["capability"] = "sunmoon"
                out["mode"], out["fixed_reply"] = "fixed", "Sun times not configured here."
                out["fixed_kind"] = "sunmoon"
                return out
            sm_reply, sm_meta = sunmoon.answer(
                raw_text, lat, lon, tz, datetime.now(timezone.utc),
                max_chars=_int_cfg(cfg, "SUNMOON_MAX_CHARS", 120))
            out["sunmoon_meta"] = sm_meta
            if sm_reply:
                out["capability"] = "sunmoon"
                out["mode"] = "fixed"
                out["fixed_kind"] = "sunmoon"
                out["fixed_reply"] = sm_reply
                return out
        elif sm_match["via"] and out.get("fixed_kind") == "calc":
            return out                      # the calc rescue already produced the answer

    fact = None
    # intent/location on the RAW text: a trailing '?' (needed for weak-keyword intent) and a
    # whitelisted place name must survive; sanitize would strip them. Nothing from raw_text
    # reaches a URL (resolve_location whitelists) or the weather prompt (build_prompt drops it).
    enabled = cfg.get("WEATHER_ENABLED", "false").lower() == "true"
    match = weather.explain_weather_match(raw_text) if enabled else None
    out["match"] = match
    if enabled and match["via"]:
        # COLLISION. "temp 12*12" carries a weather word AND a calculation, and on 2026-08-17 it
        # was answered "70F, clear, north wind 5 mph" — an observation offered as the answer to a
        # sum, twice, on the published DM path. calc refuses prose-with-an-expression on its own
        # (correctly: "box 5 * 3" is a box), so nothing claimed the arithmetic and weather took
        # it by default. The ambiguity that calc cannot resolve from the text alone IS resolved
        # here: a message this capability is about to answer with a live observation, which also
        # contains an unambiguous calculation, is a calculation. Python keeps the digits.
        if _calc_collision(cfg, clean, out):
            return out
        out["capability"] = "weather"
        # Forecast-shaped ask: we have current observations only. Answer honestly with a fixed
        # string and skip the fetch entirely — never dress a present-tense reading as a forecast.
        if weather.wants_forecast(raw_text):
            out["forecast_asked"] = True
            out["mode"] = "fixed"
            out["fixed_kind"] = "forecast_refused"
            out["fixed_reply"] = "Only current conditions, no forecast yet."
            return out
        _, latlon = weather.resolve_location(cfg, raw_text)
        meta = {}
        fact = weather.fetch_current(cfg, latlon, get=get, meta=meta) if get is not None \
            else weather.fetch_current(cfg, latlon, meta=meta)
        out["weather_ok"], out["weather_fact"] = fact is not None, fact
        # provenance for the trace: WHICH station, and how old the reading was. The station
        # id is public infrastructure (an airport code), not a location of ours.
        out["weather_meta"] = meta
        if fact is None:                       # fail-safe: NEVER invent weather from the text
            out["mode"], out["fixed_reply"] = "fixed", "Can't reach weather right now."
            return out
    # CAPABILITIES — a fixed, config-derived reply to "what can you do". LAST in the ladder,
    # below every real capability, and that placement is most of the correctness: a menu that
    # outranked weather would answer "cal whats the temperature?" with a list of topics, which
    # is a worse bug than the one it fixes. Because it sits here it can only claim a message
    # that nothing else would — i.e. exactly the messages that used to reach the model.
    #
    # Fixes the 2026-08-18 07:12 defect: "List for me the categories or topics of information
    # you know." reached the model and was answered "coding, technical questions, writing,
    # research, analysis, and general knowledge" — a chat assistant describing itself over
    # LoRa. The model is not in this path; the flags compose the sentence, so the list cannot
    # drift from what is actually armed.
    if cfg.get("CAPS_ENABLED", "false").lower() == "true":
        cap_match = capabilities.explain_match(clean, cfg.get("TRIGGER_WORD", "cal"))
        out["caps_match"] = cap_match
        if cap_match["via"]:
            out["capability"] = "capabilities"
            out["mode"] = "fixed"
            out["fixed_kind"] = "capabilities"
            out["fixed_reply"] = capabilities.answer(cfg)
            return out

    # GENERAL FALLBACK on an unlocked DM: reached only when NO doer above claimed the message
    # (capability is None). Now — not at the top — the private persona and injected context/memory
    # apply, so open-ended DMs get reach while computable/weather/sun-moon asks already returned
    # their exact answers above. Guarded on `capability is None` so a successful weather DM keeps
    # its own fact and short budget rather than being rerouted through the context path.
    if unlocked and out["capability"] is None:
        out["unlocked"] = True
        out["persona"] = PERSONA_PRIVATE
        out["max_chars"] = _int_cfg(cfg, "DM_MAX_CHARS", DEFAULTS["DM_MAX_CHARS"])
        out["prompt"] = build_private_prompt(clean, dm_context)
        return out

    out["prompt"] = build_prompt(sender_short, clean, fact)
    # Length-only relaxation on an authenticated DM. GENERAL path only: the weather prompt
    # carries its own measured 5-7 word budget, hardened across three reviews and verified 8/8
    # for surviving digits. Two budgets in one request would contradict each other, so weather
    # keeps its own and this changes nothing about it.
    if dm_authed and not unlocked and out["capability"] is None:
        out["persona"] = PERSONA_DM_AUTHED
        out["max_chars"] = _int_cfg(cfg, "DM_LOCKED_MAX_CHARS", DEFAULTS["DM_LOCKED_MAX_CHARS"])
    return out


def _claude_argv(cfg, prompt, persona=None):
    """The exact locked-down claude argv. Isolated so the eval can statically assert the
    security flags are present — a regression guard for the #1 / lockdown fixes.

    `persona` swaps ONLY the system prompt (P1 content unlock). Every security flag below is
    positional and unconditional: there is deliberately no code path, unlocked or not, that
    can drop the tool lockdown. Content is what the unlock changes; agency is not."""
    return [CLAUDE, "-p", prompt, "--model", cfg["RESPONDER_MODEL"],
            "--system-prompt", persona or PERSONA, "--permission-mode", "plan",
            "--strict-mcp-config", "--setting-sources", "",
            "--exclude-dynamic-system-prompt-sections", "--output-format", "text"]


def run_claude(cfg, prompt, persona=None):
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
            _claude_argv(cfg, prompt, persona),
            capture_output=True, text=True, timeout=_int_cfg(cfg, "GEN_TIMEOUT_S", DEFAULTS["GEN_TIMEOUT_S"]))
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
    if ts - st.get("last_reply_ts", 0) < _int_cfg(cfg, "COOLDOWN_S", DEFAULTS["COOLDOWN_S"]):
        return False, "cooldown"
    win = _int_cfg(cfg, "RATE_WINDOW_S", DEFAULTS["RATE_WINDOW_S"])
    hits = [t for t in st["per_sender"].get(sender, []) if ts - t < win]
    st["per_sender"][sender] = hits
    if len(hits) >= _int_cfg(cfg, "RATE_MAX", DEFAULTS["RATE_MAX"]):
        return False, "rate_limited"
    return True, None


def evaluate(cfg, st, rec, ours, trace=None):
    """Run the gate ladder. `trace`, if given a list, receives one {gate, pass} entry per gate
    ACTUALLY evaluated — the ladder short-circuits, so a gate after the failing one is absent
    rather than false. Optional and defaulted so every existing caller and test is unaffected."""
    sender = rec.get("from")
    to = rec.get("to")
    text = rec.get("text", "") or ""
    ch = rec.get("channel", 0)

    def mark(name, ok):
        if trace is not None:
            trace.append({"gate": name, "pass": bool(ok)})
        return ok

    if not mark("not_self", sender != ours):
        return False, "self", None, ch
    # freshness — fail CLOSED: an unparseable ts is treated as too old, not fresh.
    try:
        age = time.time() - datetime.fromisoformat(rec["ts"]).timestamp()
    except Exception:
        mark("fresh", False)
        return False, "too_old", None, ch
    if not mark("fresh", age <= _int_cfg(cfg, "MAX_AGE_S", DEFAULTS["MAX_AGE_S"])):
        return False, "too_old", None, ch
    if not mark("responder_enabled", cfg["RESPONDER_ENABLED"].lower() == "true"):
        return False, "disabled", None, ch
    allow = [a.strip() for a in cfg["ALLOW_FROM"].split(",") if a.strip()]
    if not mark("sender_allowed", sender in allow):
        return False, "sender_not_allowed", None, ch
    trigger = (cfg["TRIGGER_WORD"] or "").strip()
    is_dm = (to == ours)
    kw = bool(trigger) and re.search(r"\b" + re.escape(trigger) + r"\b", text, re.I) is not None
    if not mark("addressed", is_dm or kw):
        return False, "not_addressed", None, ch
    ok, why = rate_ok(cfg, st, sender, time.time())
    if not mark("within_rate", ok):
        return False, why, None, ch
    dest = sender if is_dm else "^all"
    return True, "addressed", dest, ch


# A bare greeting, and nothing else. Deliberately narrow: this fires for senders who are
# NOT on the allow-list, so the only safe thing to recognise is a message that asks for
# nothing. A question mark or any real content means they wanted something, and an ack
# would be answering a greeting we invented instead of the message they sent.
_GREET_RE = re.compile(
    r"^(?:good\s+)?(?:morning|afternoon|evening|day)$"
    r"|^(?:hello|hi|hey|howdy|greetings|hiya|yo|gm|ge)$"
    r"|^good\s+(?:morning|afternoon|evening|day)\s+(?:all|everyone|folks|mesh)$"
    r"|^(?:hello|hi|hey|howdy)\s+(?:all|everyone|folks|mesh)$", re.I)


# What Cal says back. The matched greeting SELECTS the line; it never shapes it. Mirroring
# the time of day is what a person does, and the point of the ack is the other node, not us:
# no identity, no location, no explanation. Just the greeting returned.
_GREET_REPLY = [
    (re.compile(r"\bmorning\b", re.I),   "Good morning"),
    (re.compile(r"\bafternoon\b", re.I), "Good afternoon"),
    (re.compile(r"\bevening\b", re.I),   "Good evening"),
    (re.compile(r"\bday\b", re.I),       "Good day"),
]
_GREET_DEFAULT = "Hello"


def greeting_reply(text, override=""):
    """The fixed line for a matched greeting. `override`, if set, wins for every greeting —
    one operator string, no mirroring. Returns None if the text is not a bare greeting, so
    the reply and the decision to reply cannot disagree (they are one call)."""
    if not is_bare_greeting(text):
        return None
    if (override or "").strip():
        return override.strip()
    s = _normalize(text or "")
    for pat, out in _GREET_REPLY:
        if pat.search(s):
            return out
    return _GREET_DEFAULT


def is_bare_greeting(text):
    """True only for a message that is ENTIRELY a greeting. Punctuation is stripped, but a
    question mark is disqualifying rather than stripped — 'morning?' wants an answer."""
    s = _normalize(text or "").strip()
    if not s or "?" in s:
        return False
    s = re.sub(r"[\s]+", " ", s.strip(" .!,;:-–—\"'"))
    if len(s.split()) > 3:
        return False
    return _GREET_RE.match(s) is not None


def plan_greeting(cfg, st, rec, ours, ts=None):
    """Decide whether an off-list sender's bare greeting gets the fixed ack.

    Pure: returns (should, reason, dest, ch, text, gates) and mutates nothing, so the whole
    path is testable offline. The caller commits the counters only if it actually sends.
    Runs ONLY after the main ladder has rejected the sender, and re-checks the gates it
    cannot inherit — a capability that trusts an earlier ladder is one refactor away from
    firing on its own.
    """
    ts = time.time() if ts is None else ts
    ch = rec.get("channel", 0)
    sender = rec.get("from")
    gates = []

    def mark(name, ok):
        gates.append({"gate": name, "pass": bool(ok)})
        return ok

    if not mark("greeting_enabled", cfg.get("GREETING_ENABLED", "false").lower() == "true"):
        return False, "greeting_disabled", None, ch, None, gates
    inbound = rec.get("text", "")
    text = greeting_reply(inbound, cfg.get("GREET_TEXT", ""))
    if not mark("not_self", sender != ours):
        return False, "self", None, ch, None, gates
    # Broadcast only (Dean's call): a greeting is a public act and an ack belongs where the
    # greeting was. A DM ack to a stranger is a stranger thing to receive.
    if not mark("broadcast", rec.get("to") in ("^all", None)):
        return False, "greeting_not_broadcast", None, ch, None, gates
    if not mark("bare_greeting", text is not None):
        return False, "not_a_greeting", None, ch, None, gates
    # No text-matching loop guard, deliberately. Mirroring means the ack IS the greeting, so
    # any "don't say what they said" rule refuses the ordinary case — measured: it blocked
    # "Good morning" outright. The budgets are the real control and they already bound a
    # runaway: our own traffic is excluded by not_self, a given node gets at most one ack per
    # GREET_SENDER_COOLDOWN_S, and the day is capped by GREET_MAX_PER_DAY. Two automated
    # nodes greeting each other therefore costs one message each, then both are on cooldown.
    greeted = st.get("greet_per_sender", {})
    last = greeted.get(sender, 0)
    if not mark("sender_cooldown", ts - last >= _int_cfg(cfg, "GREET_SENDER_COOLDOWN_S", DEFAULTS["GREET_SENDER_COOLDOWN_S"])):
        return False, "greeting_sender_cooldown", None, ch, None, gates
    day = datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")
    used = (st.get("greet_day") or {}).get(day, 0)
    if not mark("daily_budget", used < _int_cfg(cfg, "GREET_MAX_PER_DAY", DEFAULTS["GREET_MAX_PER_DAY"])):
        return False, "greeting_budget_spent", None, ch, None, gates
    return True, "greeting_ack", "^all", ch, text, gates


def commit_greeting(st, sender, ts=None):
    """Spend the budget. Separate from plan_greeting so a send that fails costs nothing."""
    ts = time.time() if ts is None else ts
    st.setdefault("greet_per_sender", {})[sender] = ts
    day = datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")
    d = st.setdefault("greet_day", {})
    d[day] = d.get(day, 0) + 1
    for k in [k for k in d if k < day]:      # keep the counter from growing without bound
        del d[k]


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
                        gates = []
                        should, reason, dest, ch = evaluate(cfg, st, rec, ours, trace=gates)
                        d = {"ts": now(), "from": rec.get("from"), "to": rec.get("to"),
                             "text": rec.get("text", ""), "matched": should,
                             "reason": reason, "reply": None, "gates": gates}
                        if should:
                            # P1: does this DM clear the authenticated-sender bar? Content only —
                            # the tool lockdown in _claude_argv is unconditional either way.
                            unl, unl_why, unl_gates = dm_unlock(cfg, rec, ours)
                            if unl_gates and unl_gates[0]["pass"]:
                                d["dm_unlock_gates"] = unl_gates
                                d["dm_unlock"] = unl
                                d["dm_unlock_reason"] = unl_why
                            # Length-only budget, independent of the unlock above.
                            lng, lng_why, lng_gates = dm_longer(cfg, rec, ours)
                            if lng_gates and lng_gates[0]["pass"]:
                                d["dm_longer_gates"] = lng_gates
                                d["dm_longer"] = lng
                                d["dm_longer_reason"] = lng_why
                            # On an unlocked DM, the injected context is the operator-curated
                            # static file PLUS this identity's remembered thread. Memory is inert
                            # unless armed AND the record is the pinned, key-verified identity, so
                            # this adds nothing on any other path.
                            dm_ctx = None
                            if unl:
                                dm_ctx = dm_memory.combine(
                                    load_dm_context(cfg), dm_memory.recall(cfg, rec))
                            plan = plan_response(cfg, rec.get("from"), rec.get("text", ""),
                                                 unlocked=unl, dm_authed=lng,
                                                 dm_context=dm_ctx)
                            # the public decision trace: how this reply came to exist. Machinery
                            # only — inputs, gates, the injected fact. No model introspection:
                            # generation is `--output-format text`, there is no reasoning to show,
                            # and inventing one would publish narrative as if it were mechanism.
                            d["sanitize"] = sanitize_trace(rec.get("text", ""),
                                                           plan["clean"], plan["flagged"])
                            d["prompt_kind"] = ("fixed" if plan["mode"] == "fixed"
                                                else "weather" if plan["weather_fact"] else "general")
                            if plan["mode"] != "fixed":
                                d["model"] = cfg["RESPONDER_MODEL"]
                            if plan["flagged"]:
                                d["injection_flagged"] = True
                            if plan.get("forecast_asked"):
                                d["forecast_asked"] = True
                            m = plan.get("match") or {}
                            if m.get("via"):
                                d["trigger_match"] = {"via": m["via"], "strong": m["strong"],
                                                      "weak": m["weak"], "question": m["question"]}
                            if plan.get("calc_meta") and plan["calc_meta"].get("handler"):
                                d["calc"] = plan["calc_meta"]
                            # sunmoon's match/meta were computed and returned but never recorded,
                            # so the module's "the shown reason IS the decision" claim was true
                            # inside the module and unwired at the consumer.
                            sm = plan.get("sunmoon_match") or {}
                            if sm.get("via"):
                                d["sunmoon_match"] = {"via": sm["via"], "sun": sm.get("sun"),
                                                      "moon": sm.get("moon"),
                                                      "moon_riseset": sm.get("moon_riseset")}
                            if plan.get("sunmoon_meta"):
                                d["sunmoon"] = plan["sunmoon_meta"]
                            if plan["capability"]:
                                d["capability"] = plan["capability"]
                                d["weather_ok"] = plan["weather_ok"]
                                d["injected_fact"] = public_fact(plan["weather_fact"])
                                wm = plan.get("weather_meta") or {}
                                if wm.get("station"):
                                    d["obs_station"] = wm["station"]
                                if wm.get("obs_age_s") is not None:
                                    d["obs_age_s"] = wm["obs_age_s"]
                            gen_start = time.time()
                            if plan["mode"] == "fixed":
                                # No model runs here. A refusal and a failed fetch are different
                                # events and must not share one status: the first is the design
                                # working, the second is the fail-safe catching something.
                                # clean_reply here too: the generated path gets it, and a
                                # fixed reply is still text going on air. Belt, not exploit.
                                reply = clean_reply(plan["fixed_reply"])
                                kind = plan.get("fixed_kind")
                                # Every fixed_kind needs its own rung. A kind that is missing here
                                # falls to the else and the public trace states a CAUSE THAT DID
                                # NOT HAPPEN — every sun/moon reply was logged as a weather
                                # failure until a review caught it.
                                why = {"calc": "fixed_calc",
                                       "forecast_refused": "fixed_forecast_refused",
                                       "sunmoon": "fixed_sunmoon",
                                       }.get(kind, "fixed_weather_unavailable")
                            else:
                                reply, why = run_claude(cfg, plan["prompt"], plan.get("persona"))
                                if reply and plan.get("max_chars"):
                                    reply = reply[:plan["max_chars"]].rstrip()
                            gen_ms = round((time.time() - gen_start) * 1000)
                            d["gen_status"] = why
                            if reply:
                                enqueue(reply, dest, ch)
                                ts = time.time()
                                st["last_reply_ts"] = ts
                                st["per_sender"].setdefault(rec.get("from"), []).append(ts)
                                d["reply"] = reply
                                d["dest"] = dest
                                d["gen_ms"] = gen_ms
                                # Write-back for DM memory: only the pinned, key-verified identity
                                # (an unlocked exchange) is ever stored, and only the sanitized
                                # question the model saw plus the reply that went on air. No-op
                                # unless armed. Failure here must never break the reply that
                                # already shipped, so it is caught.
                                if unl:
                                    try:
                                        if dm_memory.remember(cfg, rec, plan.get("clean"), reply):
                                            d["dm_memory_stored"] = True
                                    except Exception as e:
                                        log(f"dm_memory write err: {e!r}")
                                log(f"REPLY to {rec.get('from')} -> {dest}: {reply!r} ({gen_ms}ms)")
                            else:
                                d["matched"] = False
                                d["reason"] = why
                                d["gen_ms"] = gen_ms
                                log(f"gen failed for {rec.get('from')}: {why} ({gen_ms}ms)")
                        elif reason == "sender_not_allowed":
                            # Off-list sender. The ladder is right to refuse a GENERATED
                            # reply; a bare greeting still gets a fixed acknowledgement so
                            # silence doesn't read as a snub. No model, no fetch, no prose.
                            g_ok, g_reason, g_dest, g_ch, g_text, g_gates = plan_greeting(
                                cfg, st, rec, ours)
                            d["greeting_gates"] = g_gates
                            if g_ok:
                                enqueue(g_text, g_dest, g_ch)
                                commit_greeting(st, rec.get("from"))
                                save_state(st)
                                d.update({"matched": True, "reason": g_reason,
                                          "reply": g_text, "dest": g_dest,
                                          "capability": "greeting", "prompt_kind": "fixed",
                                          "gen_status": "fixed_greeting_ack"})
                                log(f"GREET {rec.get('from')} -> {g_dest}: {g_text!r}")
                            else:
                                d["greeting_reason"] = g_reason
                                log(f"skip {rec.get('from')}: {reason} / {g_reason}")
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
