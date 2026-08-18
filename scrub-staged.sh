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
IDS=$(git diff --cached | grep -oE '![0-9a-f]{8}' | sort -u \
      | grep -vE '^!(aaaaaaaa|bbbbbbbb|cccccccc|deadbeef|xxxxxxxx)$' || true)
if [ -n "$IDS" ]; then
  echo "  SCRUB FAIL: non-placeholder node ids:"; echo "$IDS" | sed 's/^/    /'; FAIL=1
else
  echo "  scrub: node ids clean"
fi
SEC=$(git diff --cached | grep -oiE 'sk-ant-[A-Za-z0-9_-]{10,}|AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{20,}|BEGIN [A-Z ]*PRIVATE KEY' || true)
if [ -n "$SEC" ]; then echo "  SCRUB FAIL: credential-shaped string"; FAIL=1; else echo "  scrub: no credential shapes"; fi
[ "$FAIL" -eq 0 ] && echo "  scrub: PASS" || echo "  scrub: BLOCKED"
exit "$FAIL"
