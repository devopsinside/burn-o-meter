#!/usr/bin/env bash
#
# End to end, against an installed engine: the Claude Code status line hook
# (install, wrap an existing status line, capture, restore), subagent and local-
# model accounting, and the snapshot the menu bar reads.
#
#   ENGINE=/path/to/burnometer scripts/hook-e2e.sh
#
# Everything happens under a throwaway HOME, BURNOMETER_HOME and CLAUDE_CONFIG_DIR;
# nothing real is read or written. Exits non-zero on any failure, and on being
# unable to run at all - a check that silently does nothing is how this script
# once "passed" after the copy it ran from had been deleted.
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
ENGINE="${ENGINE:-$(command -v burnometer || true)}"
[ -n "$ENGINE" ] && [ -x "$ENGINE" ] || { echo "no engine (set ENGINE=)" >&2; exit 2; }
FIXTURE="$HERE/tests/fixtures/codex/local.jsonl"
[ -f "$FIXTURE" ] || { echo "missing $FIXTURE" >&2; exit 2; }

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT INT TERM
H="$TMP/home"; mkdir -p "$H"
export HOME="$H" BURNOMETER_HOME="$H/.burn-o-meter" CLAUDE_CONFIG_DIR="$H/.claude" COLUMNS=120
# The engine's own directory first, so `statusline install` records this engine.
engine_dir="$(dirname "$ENGINE")"
export PATH="$engine_dir:/usr/bin:/bin"
B="$(basename "$ENGINE")"
checks=0; failed=0
ok()  { checks=$((checks+1)); printf '  \033[32m✓\033[0m %s\n' "$1"; }
bad() { checks=$((checks+1)); failed=$((failed+1)); printf '  \033[31m✗\033[0m %s\n' "$1"; }
mode() { "$PY" -c 'import os,stat,sys; print(oct(stat.S_IMODE(os.stat(sys.argv[1]).st_mode))[2:])' "$1"; }

echo "1. fake agent data"
P="$H/.claude/projects/-Users-e2e-proj"; mkdir -p "$P/sess-1/subagents" "$P/sess-1/tool-results" "$P/memory"
line() { "$PY" -c "import json,sys; print(json.dumps({'type':'assistant','isSidechain':$3,'requestId':'r_'+sys.argv[1],'sessionId':'sess-1','timestamp':'2026-09-28T10:00:00.000Z','cwd':'/Users/e2e/proj','message':{'model':'claude-opus-5','id':sys.argv[1],'content':[{'type':'text','text':'CANARY-E2E-TEXT'}],'usage':{'input_tokens':3,'output_tokens':int(sys.argv[2]),'cache_read_input_tokens':1000}}}))" "$1" "$2"; }
{ line m_main 40 False; line m_main 40 False; } > "$P/sess-1.jsonl"
# A subagent writes while it streams: the first copy holds part of the output.
{ line m_sub 7 True; line m_sub 900 True; } > "$P/sess-1/subagents/agent-x1.jsonl"
echo '{"CANARY-E2E-META":1}' > "$P/sess-1/subagents/agent-x1.meta.json"
echo CANARY-E2E-TOOL > "$P/sess-1/tool-results/t.txt"; echo CANARY-E2E-MEM > "$P/memory/MEMORY.md"
mkdir -p "$H/.codex/sessions/2026/09/28"; cp "$FIXTURE" "$H/.codex/sessions/2026/09/28/rollout-2026-09-28T17-49-10-e2e.jsonl"
echo '{"CANARY-E2E-CRED":"x"}' > "$H/.codex/auth.json"
echo '{"model":"opus","env":{"ANTHROPIC_API_KEY":"sk-CANARY-E2E-KEY"},"statusLine":{"type":"command","command":"echo mybar","padding":1}}' > "$H/.claude/settings.json"
chmod 600 "$H/.claude/settings.json"

echo "2. statusline install wraps the existing status line"
"$B" statusline install >/dev/null
if "$PY" - "$H" <<'PY'
import json, sys
d = json.load(open(sys.argv[1] + "/.claude/settings.json"))
c = d["statusLine"]["command"]
assert "statusline --then" in c, c
assert d["env"]["ANTHROPIC_API_KEY"] == "sk-CANARY-E2E-KEY" and d["model"] == "opus", d
assert d["statusLine"]["padding"] == 1
PY
then ok "other settings kept, existing command wrapped"; else bad "settings after install"; fi
[ "$(mode "$H/.claude/settings.json")" = 600 ] && ok "settings mode kept 600" || bad "settings mode changed"

echo "3. Claude Code runs the hook - the exact command string, through sh"
CMD=$("$PY" -c "import json;print(json.load(open('$H/.claude/settings.json'))['statusLine']['command'])")
RESET=$(( $(date +%s) + 3 * 3600 ))
OUT=$(printf '{"cwd":"/Users/e2e/CANARY-E2E-CWD","transcript_path":"/x/CANARY-E2E-T","session_id":"CANARY-E2E-S","rate_limits":{"five_hour":{"used_percentage":91.6,"resets_at":%s},"seven_day":{"used_percentage":22,"resets_at":%s}}}' "$RESET" "$((RESET + 86400))" | sh -c "$CMD")
[ "$OUT" = "mybar" ] && ok "the wrapped status line still prints its own output" || bad "wrapped output was '$OUT'"
CAP="$BURNOMETER_HOME/claude-rate-limits.json"
[ -f "$CAP" ] && [ "$(mode "$CAP")" = 600 ] && ok "capture written, 0600" || bad "capture missing or not 0600"
grep -q CANARY "$CAP" 2>/dev/null && bad "payload content reached the capture" || ok "capture holds no payload content"
OUT2=$(echo 'garbage{' | "$B" statusline; echo "exit=$?")
[ "$OUT2" = "exit=0" ] && ok "garbage input: silent, exit 0" || bad "garbage input gave '$OUT2'"

echo "4. scan, today, doctor"
"$B" scan >/dev/null 2>&1 && ok "scan" || bad "scan failed"
# Captured, then matched: `cmd | grep -q` under pipefail fails with SIGPIPE when
# grep exits at the first match, reporting a failure for text that was there.
today_out="$("$B" today 2>&1)"
grep -q "claude five_hour  92% used" <<<"$today_out" && ok "today shows the live 5h figure (91.6 -> 92%)" || bad "today lacks the live figure"
if "$PY" - "$BURNOMETER_HOME" <<'PY'
import sqlite3, sys
home = sys.argv[1]
db = sqlite3.connect(home + "/burn.db")
rows = db.execute("select provider, model, upstream_provider, cost_basis, sum(output_tokens), count(*) from usage_events group by 1,2,3,4").fetchall()
got = {(r[0], r[1]): (r[4], r[5]) for r in rows}
assert got == {("claude_code", "claude-opus-5"): (940, 2), ("codex", "qwen3:0.6b"): (83, 1)}, got
assert [r[3] for r in rows if r[0] == "codex"] == ["not_metered"], rows
assert b"CANARY" not in open(home + "/burn.db", "rb").read()
PY
then ok "subagent's finished copy counted (40+900); Codex on Ollama not metered; no canary stored"; else bad "stored usage is wrong"; fi
doctor_out="$("$B" doctor 2>/dev/null)"
grep -q "live, via" <<<"$doctor_out" && ok "doctor lists the live source" || bad "doctor output"

echo "5. the snapshot the menu bar reads"
if "$PY" - "$BURNOMETER_HOME/snapshot.json" <<'PY'
import json, sys
q = [x for x in json.load(open(sys.argv[1]))["quotas"] if x["provider"] == "claude"]
assert any(x["window"] == "five_hour" and x["used_percent"] == 91.6 and x["exact"] for x in q), q
PY
then ok "snapshot carries the live 91.6%"; else bad "snapshot lacks the live figure"; fi

echo "6. uninstall restores"
"$B" statusline uninstall >/dev/null
"$PY" -c "import json; d=json.load(open('$H/.claude/settings.json')); assert d['statusLine']=={'type':'command','command':'echo mybar','padding':1}, d['statusLine']; assert d['env']['ANTHROPIC_API_KEY']=='sk-CANARY-E2E-KEY'" \
  && ok "original status line restored exactly" || bad "restore"
grep -rl "CANARY-E2E-KEY" "$BURNOMETER_HOME" >/dev/null 2>&1 && bad "API key copied into the data directory" || ok "API key never copied"

printf '\n'
EXPECTED=13
if [ "$checks" -ne "$EXPECTED" ]; then echo "$checks of $EXPECTED checks ran - refusing to call that a pass" >&2; exit 1; fi
[ "$failed" -eq 0 ] && printf '\033[32mhook end to end: %s checks passed\033[0m\n' "$checks" || printf '\033[31m%s of %s checks failed\033[0m\n' "$failed" "$checks"
exit "$failed"
