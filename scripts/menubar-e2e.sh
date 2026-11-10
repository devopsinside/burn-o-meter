#!/usr/bin/env bash
#
# End to end: a Claude Code transcript on disk -> engine scan -> snapshot -> the
# title the compiled menu bar app would show. Every unit test can pass while this
# chain is broken, and once it was: the menu bar showed "—" at 13:30 in India,
# because usage at 01:21 local had been filed under yesterday's UTC date.
#
#   scripts/menubar-e2e.sh                       # engine on PATH, app from macos/build
#   ENGINE=.venv/bin/burnometer APP=/path/to/burn-o-meter scripts/menubar-e2e.sh
#
# Runs in a throwaway home under several time zones. The usage is placed one
# minute after LOCAL midnight, which is yesterday in UTC for every zone east of
# Greenwich - so a UTC day boundary anywhere in the chain fails it. Nothing
# outside the temp directory is read or written.
set -uo pipefail

# A Python new enough for the engine and for these helpers (datetime.UTC is
# 3.11+). Bare `python3` is not that everywhere: with full Xcode selected,
# /usr/bin/python3 is Xcode's 3.9, and every helper below failed on it - which
# the menu bar check then reported as "no spend" instead of as a broken setup.
PY=""
for candidate in python3.14 python3.13 python3.12 python3.11 python3; do
  if command -v "$candidate" >/dev/null 2>&1 \
     && "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
    PY="$(command -v "$candidate")"; break
  fi
done
[ -n "$PY" ] || { echo "needs Python 3.11 or newer on PATH" >&2; exit 2; }

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENGINE="${ENGINE:-$(command -v burnometer || echo "$HERE/.venv/bin/burnometer")}"
APP="${APP:-$HERE/macos/build/burn-o-meter.app/Contents/MacOS/burn-o-meter}"
ZONES="${ZONES:-Asia/Kolkata Pacific/Kiritimati America/Los_Angeles UTC}"

[ -x "$ENGINE" ] || { echo "no engine at $ENGINE (set ENGINE=)" >&2; exit 2; }
[ -x "$APP" ] || { echo "no app at $APP — run macos/make-app.sh (or set APP=)" >&2; exit 2; }

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT INT TERM
fail=0
ok()  { printf '  \033[32m✓\033[0m %s\n' "$*"; }
bad() { printf '  \033[31m✗\033[0m %s\n' "$*"; fail=$((fail+1)); }

for zone in $ZONES; do
  printf '\n\033[1m%s\033[0m\n' "$zone"
  H="$TMP/$(echo "$zone" | tr '/' '_')"
  P="$H/.claude/projects/-Users-e2e-demo"
  mkdir -p "$P"

  # Assistant turns written as Claude Code writes them (each repeated, as it
  # repeats them): one a minute after local midnight, one five minutes ago -
  # inside the five-hour window the status line reports below - and one four
  # hours ago, before that window opened. Each is a different size, so a spend
  # that counts the wrong ones cannot come out right by accident.
  TZ="$zone" "$PY" - "$P/sess-e2e.jsonl" <<'PY'
import json, sys
from datetime import UTC, datetime, timedelta
now = datetime.now().astimezone()
midnight = datetime(now.year, now.month, now.day).astimezone()
turns = [("midnight", midnight + timedelta(minutes=1), 800),
         ("recent", now - timedelta(minutes=5), 3000),
         ("early", now - timedelta(hours=4), 50000)]
with open(sys.argv[1], "w") as out:
    for name, at, output in turns:
        line = json.dumps({
            "type": "assistant", "requestId": f"req_{name}", "sessionId": "sess-e2e",
            "timestamp": at.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "cwd": "/Users/e2e/demo",
            "message": {"model": "claude-opus-5", "id": f"msg_{name}",
                        "usage": {"input_tokens": 1200, "output_tokens": output,
                                  "cache_read_input_tokens": 40000}},
        })
        out.write(line + "\n" + line + "\n")
PY

  # A setup that failed must stop the run, not be reported as the product's
  # fault: a helper crash once left no transcript, and the check said "no spend".
  [ -s "$P/sess-e2e.jsonl" ] || { echo "could not write the test transcript" >&2; exit 2; }

  export HOME="$H" BURNOMETER_HOME="$H/.burn-o-meter" CLAUDE_CONFIG_DIR="$H/.claude" TZ="$zone"

  # A live rate-limit reading, through the real status line hook.
  reset=$(( $(date +%s) + 3 * 3600 ))
  shown=$(printf '{"cwd":"/x","rate_limits":{"five_hour":{"used_percentage":42,"resets_at":%s}}}' \
          "$reset" | "$ENGINE" statusline)
  [ "$shown" = "5h 42%" ] && ok "status line hook printed: $shown" || bad "status line printed '$shown'"

  "$ENGINE" scan --quiet >/dev/null 2>&1 && ok "engine scan" || bad "engine scan failed"

  # Both styles that show a percentage and a spend. `-menuBarStyle x` overrides
  # the saved preference for this process only, so the result never depends on
  # how the machine running the check has its menu bar set.
  for style in full compact; do
  dump=$(perl -e 'alarm 30; exec @ARGV' "$APP" --dump -menuBarStyle "$style" 2>/dev/null)
  if [ -z "$dump" ]; then bad "the app produced no --dump output ($style)"; continue; fi

  STYLE="$style" "$PY" - "$dump" "$BURNOMETER_HOME/snapshot.json" <<'PY' || fail=$((fail+1))
import json, os, sys
from datetime import datetime, timedelta
d = json.loads(sys.argv[1])
snap = json.load(open(sys.argv[2]))
title = d.get("menu_bar_title", "")
problems = []
if title in ("", "—"):
    problems.append(f"menu bar title is {title!r}: today's usage was not counted")
if "42%" not in title:
    problems.append(f"menu bar title {title!r} lacks the live 42%")
if "$" not in title:
    problems.append(f"menu bar title {title!r} shows no spend")
else:
    # Real usage was recorded, so the spend must not read as zero ("$0.0", "$0.00").
    amount = title.split("$", 1)[1].split()[0]
    if amount.replace("0", "").replace(".", "") == "":
        problems.append(f"menu bar title {title!r} shows a real spend as zero")
if not d.get("rows"):
    problems.append("the popover has no model rows for today")
# The spend beside the percentage is the five-hour window's own: the turn five
# minutes ago, the midnight one only while it is inside the window, never the one
# from four hours ago - and never simply today's total. At 01:57 the bar once read
# "76% ~$2.9", today's spend beside a window that held $31.66.
window = next(q for q in snap["quotas"] if q["window"] == "five_hour")
spend = window.get("spend") or {}
now = datetime.now().astimezone()
start = datetime.fromisoformat(spend.get("since", "1970-01-01T00:00:00+00:00"))
after_midnight = datetime(now.year, now.month, now.day).astimezone() + timedelta(minutes=1)
expected = 1 + (after_midnight >= start)
if abs((start - (now - timedelta(hours=2))).total_seconds()) > 120:
    problems.append(f"window starts {start}, not five hours before its reset")
if spend.get("requests") != expected:
    problems.append(f"window spend counts {spend.get('requests')} turns, expected {expected}")
amount = spend.get("subtotals", {}).get("api_equivalent")
if amount is None:
    problems.append(f"the snapshot carries no window spend: {spend!r}")
else:
    if os.environ["STYLE"] == "full" or amount < 1:
        shown = f"${amount:.2f}"
    else:
        shown = f"${amount:.0f}" if amount >= 10 else f"${amount:.1f}"
    if f"~{shown}" not in title:
        problems.append(f"menu bar title {title!r} does not show the window's spend ~{shown}")
for p in problems:
    print(f"  \033[31m✗\033[0m {p}")
if problems:
    sys.exit(1)
print(f"  \033[32m✓\033[0m menu bar ({os.environ['STYLE']}) would show: {title}")
PY
  done
  # The hourly chart's first bar is local midnight, and the usage sits in it.
  TZ="$zone" "$PY" - "$BURNOMETER_HOME/snapshot.json" <<'PY' || fail=$((fail+1))
import json, sys
from datetime import datetime
snap = json.load(open(sys.argv[1]))
today = next(r for r in snap["ranges"] if r["key"] == "today")
want = datetime.now().astimezone().strftime("%Y-%m-%dT00")
first, spent = today["points"][0]["label"], [p["label"] for p in today["points"] if p["total"]]
if first == want and spent and spent[0] == want:
    print(f"  \033[32m✓\033[0m chart starts at local midnight ({first}) and the bar is there")
else:
    print(f"  \033[31m✗\033[0m chart starts {first}, spend at {spent}; expected {want}")
    sys.exit(1)
PY
  unset HOME
done

printf '\n'
if [ "$fail" -eq 0 ]; then printf '\033[32mmenu bar end to end: all zones pass\033[0m\n'; else printf '\033[31m%s failure(s)\033[0m\n' "$fail"; fi
exit "$fail"
