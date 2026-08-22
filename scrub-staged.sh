#!/bin/bash
# Pre-push scrub, run on the STAGED set. Exits non-zero on a hit so it can HALT a commit.
# History: (1) it was once chained into the push with && so it reported and pushed anyway;
# (2) the first fix used `grep -qv`, which on this system returns SUCCESS for empty input, so it
# aborted on a clean diff. Test for CONTENT, never for an exit code.
cd ~/cal-mesh || exit 2
DEP=$(grep '^WEATHER_POINT=' config | cut -d= -f2)
FAIL=0
if [ -n "$DEP" ] && git diff --cached | grep -qF "$DEP"; then
  echo "  SCRUB FAIL: deployed observer point in the staged set"; FAIL=1
else
  echo "  scrub: observer point clean"
fi
# (3) it blocked a commit whose only node id was one this repo PUBLISHED on 2026-08-11 and has
# carried in four tracked files ever since — a new copy of an already-public string is not a new
# disclosure, and a check that cannot tell those apart teaches you to wave it through, which is
# strictly worse than not having it. An id already in HEAD is reported and allowed; anything
# else still halts. HEAD, not the worktree: an id you added but have not committed is new.
CAND=$(git diff --cached | grep -oE '![0-9a-f]{8}' | sort -u \
      | grep -vE '^!(aaaaaaaa|bbbbbbbb|cccccccc|deadbeef|xxxxxxxx)$' || true)
IDS=""; KNOWN=""
for id in $CAND; do
  if [ -n "$(git grep -lF "$id" HEAD -- . 2>/dev/null)" ]; then
    KNOWN="$KNOWN $id"
  else
    IDS="$IDS $id"
  fi
done
if [ -n "$KNOWN" ]; then
  echo "  scrub: node ids already published in HEAD (allowed):$KNOWN"
fi
if [ -n "$IDS" ]; then
  echo "  SCRUB FAIL: node ids not previously published:"
  for id in $IDS; do echo "    $id"; done; FAIL=1
else
  echo "  scrub: no new node ids"
fi
# Channel keys. A Meshtastic channel URL carries the PSK in its fragment, so pasting one into a
# comment, a doc, a fixture or an example command publishes the key to everyone reading this
# repo -- and a channel is only worth anything while its key is secret. The mechanism here is
# meant to be copied; the key never is. Added before the first channel was created, deliberately:
# a guard that arrives after the paste is not a guard.
SEC=$(git diff --cached | grep -oiE 'sk-ant-[A-Za-z0-9_-]{10,}|AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{20,}|BEGIN [A-Z ]*PRIVATE KEY|meshtastic\.org/e/#[A-Za-z0-9_+/=-]{8,}|\bpsk["'\'']?\s*[:=]\s*["'\'']?[A-Za-z0-9+/_-]{16,}={0,2}' || true)
if [ -n "$SEC" ]; then echo "  SCRUB FAIL: credential-shaped string"; FAIL=1; else echo "  scrub: no credential shapes"; fi
[ "$FAIL" -eq 0 ] && echo "  scrub: PASS" || echo "  scrub: BLOCKED"
exit "$FAIL"
