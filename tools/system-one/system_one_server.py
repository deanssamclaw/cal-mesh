#!/usr/bin/env python3
"""system_one_server.py — a System One scorer that runs in the house.

WHY. cal-mesh's s1route.py (named jevroute until 2026-09-23) asks one typed question on the
fallthrough: which capability should answer this message? That question went to TypeSafe.
Measured on 319 real inbound messages, a frozen Qwen3.5-4B scored by SemIf's own scorer, with one
temperature fitted on the messages NOT addressed to Cal, makes exactly the same three rescues as
the cloud model and breaks nothing (docs/proposals/local-system-one.md in cal-mesh). This serves
that locally, so no message text leaves the house.

WHAT IT IS NOT. It is not Jev, it does not claim Jev's numbers, and it answers in TypeSafe's
request/response SHAPE only so the caller needs one config line instead of a second parser. The
`model` field always names what actually ran.

THE RULES:
1. ONE MODEL, LOADED ONCE, ONE AT A TIME. llama.cpp holds a single scoring context; a lock
   serialises requests. A second caller is REFUSED at once with 503, not queued: queued, it
   waited out the first request and then ran its own, and review (2026-09-24) measured the
   second of two concurrent calls at 28.8 s against the caller's 30 s deadline -- a timeout
   the caller reads as an outage (5 min off) where a 503 reads as busy (2 min), and the heat is
   spent on an answer nobody is waiting for.
2. THE SCORING IS SEMIF'S, NOT MINE. `llamacpp_backend.score` is the measured path. This file
   builds rows, applies the fitted temperature and shapes the reply.
3. THERMALLY HONEST. The reference box (a 2019 MacBook Pro running Linux) reaches ~100 C on any
   inference. Above BUSY_TEMP_C this returns 503 and the caller falls open to the behaviour it
   has without this service.
4. NO MESSAGE TEXT IS LOGGED. Only question ids, timings and the chosen option.
"""
import json, os, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE = os.path.dirname(os.path.abspath(__file__))
GGUF = os.environ.get("S1_GGUF", os.path.expanduser("~/jev-eval/gguf/Qwen_Qwen3.5-4B-Q4_K_M.gguf"))
SOURCE = os.environ.get("S1_SOURCE", "Qwen/Qwen3.5-4B")
REVISION = os.environ.get("S1_REVISION", "851bf6e806efd8d0a36b00ddf55e13ccb7b8cd0a")
THREADS = int(os.environ.get("S1_THREADS", "8"))
# Fitted on the 263 messages NOT addressed to Cal and applied to the 56 that were; refit with
# tools/local-system-one/semif_analyze.py if the traffic changes shape.
TEMPERATURE = float(os.environ.get("S1_TEMPERATURE", "1.45"))
PORT = int(os.environ.get("S1_PORT", "8799"))
# Loopback by default. The unit binds the TAILSCALE address instead, because the caller
# (cal-mesh, on another machine) is another node on the tailnet: reachable by this operator's own
# devices, not by the LAN and not by the internet. There is no auth on this port -- the tailnet is
# the boundary, so do not bind 0.0.0.0.
HOST = os.environ.get("S1_HOST", "127.0.0.1")
BUSY_TEMP_C = int(os.environ.get("S1_BUSY_TEMP_C", "95"))
ABORT_TEMP_C = int(os.environ.get("S1_ABORT_TEMP_C", "99"))
# TWO THRESHOLDS, because they answer different questions. BUSY_TEMP_C decides whether to START
# work: at or above it the request is refused at the door and the caller falls back. ABORT_TEMP_C
# is the in-flight stop, and it has to sit at the hardware's own limit rather than beside the door
# value -- on this laptop ONE question takes the package from 40 C to ~98 C, so an in-flight check
# at 95 aborted almost every real request and the router failed open on every message. What bounds
# the cost of a request is the caps above (4 questions, 4000 characters), not the abort.
# A REQUEST'S WORK MUST BE BOUNDED, not just its byte count. A security review measured one
# near-token-limit question at 90 s and 97 C: the old caps (256 KiB, 16 questions) allowed a
# single unauthenticated caller to pin this laptop for ~24 minutes. The caller (cal-mesh
# s1route.py) sends ONE state of a couple of hundred characters and THREE questions, so these
# sit just above real use and far below what the hardware can be made to do.
MAX_QUESTIONS = int(os.environ.get("S1_MAX_QUESTIONS", "4"))
MAX_STATE_CHARS = int(os.environ.get("S1_MAX_STATE_CHARS", "4000"))
MAX_INSTR_CHARS = int(os.environ.get("S1_MAX_INSTR_CHARS", "4000"))
READ_TIMEOUT_S = int(os.environ.get("S1_READ_TIMEOUT_S", "10"))
# MACHINE-SPECIFIC: thermal_zone2 is the package sensor on the reference laptop; find yours under
# /sys/class/thermal (see README.md). Injectable so the guard can be TESTED: point S1_ZONE at a
# file you control and the abort path can be exercised without heating the machine. A guard that
# cannot be tested is decoration.
ZONE = os.environ.get("S1_ZONE", "/sys/class/thermal/thermal_zone2/temp")
MODEL_NAME = "semif-qwen3.5-4b-q4km"

from semif_phase1 import llamacpp_backend as B   # noqa: E402

_lock = threading.Lock()
_model = _tok = None


def log(m):
    print(f"{time.strftime('%FT%TZ', time.gmtime())} {m}", flush=True)


class Busy(Exception):
    """The box is too hot to keep going. Raised between questions, not only at the door."""


def cpu_c():
    """FAIL CLOSED. An unreadable sensor used to read as 0 C, which turned the thermal guard
    off entirely -- rename the zone and the service would cook the laptop without complaint."""
    try:
        return int(open(ZONE).read()) // 1000
    except Exception:
        return 999


def _rows(state, questions):
    """TypeSafe-shaped questions -> SemIf rows. One row per question; the row's options are the
    answer space, so nothing outside it can come back."""
    text = state if isinstance(state, str) else json.dumps(state, ensure_ascii=False)
    out = []
    for qid, q in questions.items():
        if len(str(q.get("instructions"))) > MAX_INSTR_CHARS:
            raise ValueError(f"question {qid!r}: instructions too long")
        typ = q.get("type")
        instr = q.get("instructions")
        instr = instr if isinstance(instr, str) else json.dumps(instr, ensure_ascii=False)
        crit = q.get("criteria")
        if typ == "choice":
            opts = [{"id": k, "description": v if isinstance(v, str) else json.dumps(v)}
                    for k, v in (crit or {}).items()]
        elif typ == "noul":
            c = crit or {}
            opts = [{"id": "yes", "description": str(c.get("true", "Yes."))},
                    {"id": "no", "description": str(c.get("false", "No."))}]
        elif typ == "score":
            opts = [{"id": str(i), "description": d if isinstance(d, str) else json.dumps(d)}
                    for i, d in enumerate(crit or [])]
        else:
            raise ValueError(f"unknown question type {typ!r}")
        if len(opts) < 2:
            raise ValueError(f"question {qid!r} needs at least two options")
        out.append((qid, typ, {"id": qid, "state": text, "question": instr, "options": opts}))
    return out


def _soft(logits, T):
    m = max(logits)
    e = [pow(2.718281828459045, (x - m) / T) for x in logits]
    s = sum(e)
    return [x / s for x in e]


def answer(state, questions):
    rows = _rows(state, questions)
    answers = {}
    if not _lock.acquire(blocking=False):
        raise Busy("already scoring")
    try:
        for qid, typ, row in rows:
            # Between questions, not only at the door: a request accepted at 58 C used to run
            # all the way through 97 C because the temperature was read once.
            if cpu_c() >= ABORT_TEMP_C:
                raise Busy(f"cpu {cpu_c()}C mid-request")
            res = B.score(_model, _tok, row, {})
            logits = res.get("option_logits") or []
            probs = _soft(logits, TEMPERATURE)
            ids = [o["id"] for o in row["options"]]
            if typ == "noul":
                answers[qid] = {"type": "noul", "noul": round(probs[0], 4)}
            elif typ == "choice":
                k = max(range(len(probs)), key=lambda i: probs[i])
                n = len(probs)
                answers[qid] = {"type": "choice", "choice": ids[k],
                                "probabilities": {i: round(p, 4) for i, p in zip(ids, probs)},
                                "confidence": round(max(0.0, (n * probs[k] - 1) / (n - 1)), 4)}
            else:
                exp = sum(i * p for i, p in enumerate(probs))
                k = max(range(len(probs)), key=lambda i: probs[i])
                n = len(probs)
                answers[qid] = {"type": "score", "score": round(exp, 4),
                                "legend": {str(i): o["description"] for i, o in enumerate(row["options"])},
                                "probabilities": {str(i): round(p, 4) for i, p in enumerate(probs)},
                                "confidence": round(max(0.0, (n * probs[k] - 1) / (n - 1)), 4)}
    finally:
        _lock.release()
    return answers


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    # Without this the read below blocks forever on a body that never arrives, and every
    # tailnet node can hold a thread each (slowloris). BaseHTTPRequestHandler defaults to None.
    timeout = READ_TIMEOUT_S

    def log_message(self, *a):        # rule 4: the default handler logs the request line
        pass

    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self._send(200, {"ok": _model is not None, "model": MODEL_NAME, "cpu_c": cpu_c(),
                             "busy_above_c": BUSY_TEMP_C, "temperature": TEMPERATURE})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path not in ("/v1/systemone", "/systemone"):
            return self._send(404, {"error": "not found"})
        t = cpu_c()
        if t >= BUSY_TEMP_C:
            log(f"busy: cpu {t}C >= {BUSY_TEMP_C}C")
            return self._send(503, {"error": "too hot", "cpu_c": t})
        try:
            n = int(self.headers.get("Content-Length", 0))
            if n <= 0 or n > 262144:
                return self._send(413, {"error": "bad length"})
            req = json.loads(self.rfile.read(n).decode("utf-8"))
            state, questions = req.get("state"), req.get("questions")
            if state is None or not isinstance(questions, dict) or not questions:
                return self._send(422, {"error": "state and questions are required"})
            if len(questions) > MAX_QUESTIONS:
                return self._send(422, {"error": f"at most {MAX_QUESTIONS} questions per request"})
            if len(state if isinstance(state, str) else json.dumps(state)) > MAX_STATE_CHARS:
                return self._send(422, {"error": f"state exceeds {MAX_STATE_CHARS} characters"})
            t0 = time.time()
            ans = answer(state, questions)
            ms = round((time.time() - t0) * 1000)
            log(f"scored {len(questions)} question(s) in {ms} ms, cpu {cpu_c()}C, "
                + ",".join(f"{k}={v.get('choice', v.get('noul', v.get('score')))}" for k, v in ans.items()))
            self._send(200, {"model": MODEL_NAME, "answers": ans,
                             "usage": {"input_tokens": 0, "output_tokens": 0}, "ms": ms})
        except Busy as e:
            log(f"busy: {e}")
            self._send(503, {"error": "busy", "detail": str(e)[:60], "cpu_c": cpu_c()})
        except ValueError as e:
            self._send(422, {"error": str(e)[:200]})
        except Exception as e:
            log(f"error: {type(e).__name__}")
            self._send(500, {"error": type(e).__name__})


def main():
    global _model, _tok
    log(f"loading {os.path.basename(GGUF)} threads={THREADS} T={TEMPERATURE}")
    t0 = time.time()
    _model, _tok, meta = B.load_model(SOURCE, REVISION, GGUF, threads=THREADS)
    log(f"backend metadata: {json.dumps(meta)[:200]}")
    log(f"loaded in {time.time()-t0:.1f}s; serving on {HOST}:{PORT}")
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    srv.timeout = READ_TIMEOUT_S
    srv.serve_forever()


if __name__ == "__main__":
    main()
