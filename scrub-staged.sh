#!/bin/bash
# Pre-push scrub, run on the STAGED set. Exits non-zero on a hit so it can HALT a commit.
# History: (1) it was once chained into the push with && so it reported and pushed anyway;
# (2) the first fix used `grep -qv`, which on this system returns SUCCESS for empty input, so it
# aborted on a clean diff. Test for CONTENT, never for an exit code.
# The REPOSITORY THIS COMMIT IS IN, not a hardcoded path. `cd ~/cal-mesh` read the main
# checkout's index while the commit came from a worktree, so `git diff --cached` was EMPTY and
# every scan below passed on nothing. There are eight live worktrees; most of today's commits
# came from one, and each was scrubbed against an empty staged set. A guard that inspects the
# wrong index is worse than none, because it prints PASS.
TOP=$(git rev-parse --show-toplevel 2>/dev/null) || exit 2
cd "$TOP" || exit 2
# config is gitignored and lives only in the main checkout, so read it from there; a worktree
# has no copy and an empty DEP would silently disable the observer-point scan.
MAIN=$(git rev-parse --path-format=absolute --git-common-dir 2>/dev/null)
MAIN=${MAIN%/.git}
DEP=$(grep '^WEATHER_POINT=' "$MAIN/config" 2>/dev/null | cut -d= -f2)
[ -z "$DEP" ] && DEP=$(grep '^WEATHER_POINT=' config 2>/dev/null | cut -d= -f2)
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
# Filename guard for the operational logs that hold MESSAGE TEXT and MODEL PROSE. The content
# scans above cannot see this class: a stranger's message and a drafted reply match no
# coordinate, no node id and no credential shape, so nothing above would fire. The only control
# that works is refusing the file by name, and .gitignore is one `add -f` from being moot.
NAMES=$(git diff --cached --name-only | grep -E '(^|/)(drafts\.jsonl|draft-grades\.json|decisions\.jsonl|inbox\.jsonl|dm-memory\.json|dm-context\.txt)$' || true)
if [ -n "$NAMES" ]; then
  echo "  SCRUB FAIL: operational log staged (message text / model prose):"
  for n in $NAMES; do echo "    $n"; done; FAIL=1
else
  echo "  scrub: no operational logs staged"
fi
[ "$FAIL" -eq 0 ] && echo "  scrub: PASS" || echo "  scrub: BLOCKED"
exit "$FAIL"
