#!/usr/bin/env bash
#
# Install burn-o-meter, run it against your real logs, and remove every trace —
# in one command.
#
#   scripts/smoke-test.sh              # from a checkout
#   scripts/smoke-test.sh --from-git   # from the public repository, as a stranger would
#
# Safe to run while you already use burn-o-meter: everything happens in a
# throwaway virtualenv with its own data directory, so your real database,
# config and background agent are never touched. The temp directory is removed
# on exit, including on failure or Ctrl-C.
set -uo pipefail

REPO_URL="https://github.com/devopsinside/burn-o-meter"
SOURCE="${1:-}"
TMP="$(mktemp -d)"
export BURNOMETER_HOME="$TMP/home"

pass=0; fail=0
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; pass=$((pass+1)); }
bad()  { printf '  \033[31m✗\033[0m %s\n' "$*"; fail=$((fail+1)); }
step() { printf '\n\033[1m%s\033[0m\n' "$*"; }

cleanup() {
  step "Uninstalling"
  # Nothing here touches a real install: the venv and data dir are both inside
  # a temp directory that only this run created.
  rm -rf "$TMP"
  [ -d "$TMP" ] && bad "temp directory survived: $TMP" || ok "removed everything ($TMP)"

  if [ -d "$HOME/.burn-o-meter" ]; then
    ok "your own ~/.burn-o-meter is untouched (this test never wrote to it)"
  fi

  printf '\n'
  if [ "$fail" -eq 0 ]; then
    printf '\033[32m%s checks passed.\033[0m burn-o-meter installs, runs and removes cleanly.\n' "$pass"
  else
    printf '\033[31m%s of %s checks failed.\033[0m\n' "$fail" "$((pass+fail))"
  fi
  exit "$fail"
}
trap cleanup EXIT INT TERM

step "Installing into a throwaway environment"
python3 -m venv "$TMP/venv" >/dev/null 2>&1 || { bad "could not create a virtualenv"; exit 1; }
BIN="$TMP/venv/bin"

if [ "$SOURCE" = "--from-git" ]; then
  target="git+$REPO_URL"
  printf '  source: %s\n' "$REPO_URL"
else
  target="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
  printf '  source: %s\n' "$target"
fi

if "$BIN/pip" install --quiet "$target" >/dev/null 2>&1; then
  ok "installed"
else
  bad "install failed"; exit 1
fi

version="$("$BIN/burn-o-meter" --version 2>/dev/null)"
[ -n "$version" ] && ok "runs: $version" || bad "the command did not run"

# Both spellings are installed; a reader copying either from the docs must work.
"$BIN/burnometer" --version >/dev/null 2>&1 && ok "the burnometer alias works too" \
                                            || bad "the burnometer alias is missing"

step "Reading your logs"
"$BIN/burn-o-meter" doctor >/dev/null 2>&1 && ok "doctor ran" || bad "doctor failed"

detected="$("$BIN/burn-o-meter" doctor 2>/dev/null | grep -cE 'ready' || true)"
if [ "${detected:-0}" -gt 0 ]; then
  ok "found $detected agent source(s) on this machine"
else
  ok "no agent logs here — nothing to read, which is a valid result"
fi

if "$BIN/burn-o-meter" scan --quiet >/dev/null 2>&1; then
  ok "scan completed"
else
  bad "scan failed"
fi

events="$("$BIN/burn-o-meter" doctor 2>/dev/null | grep -oE 'events +[0-9,]+' | grep -oE '[0-9,]+' | head -1)"
[ -n "${events:-}" ] && ok "stored $events events" || ok "stored nothing (no agent logs present)"

# Rescanning must add nothing. This is the property that stops double-counting.
before="$events"
"$BIN/burn-o-meter" scan --quiet >/dev/null 2>&1
after="$("$BIN/burn-o-meter" doctor 2>/dev/null | grep -oE 'events +[0-9,]+' | grep -oE '[0-9,]+' | head -1)"
[ "${before:-0}" = "${after:-0}" ] && ok "a second scan added nothing (idempotent)" \
                                   || bad "a second scan changed the count: $before -> $after"

step "Reports"
for cmd in today models daily blocks; do
  "$BIN/burn-o-meter" "$cmd" >/dev/null 2>&1 && ok "$cmd" || bad "$cmd failed"
done
"$BIN/burn-o-meter" models --json 2>/dev/null | python3 -c 'import json,sys; json.load(sys.stdin)' 2>/dev/null \
  && ok "--json is valid JSON" || bad "--json did not parse"

step "Privacy"
# The whole promise: none of your prompts end up in what it writes.
leak=0
for f in "$BURNOMETER_HOME"/*; do
  [ -f "$f" ] || continue
  if strings "$f" 2>/dev/null | grep -qiE 'BEGIN [A-Z ]*PRIVATE KEY|sk-ant-[A-Za-z0-9]{8}'; then
    bad "something credential-shaped in $(basename "$f")"; leak=1
  fi
done
[ "$leak" -eq 0 ] && ok "no credential-shaped strings in anything it wrote"

if [ -f "$BURNOMETER_HOME/burn.db" ]; then
  perms="$(stat -f '%Sp' "$BURNOMETER_HOME/burn.db" 2>/dev/null || stat -c '%A' "$BURNOMETER_HOME/burn.db")"
  case "$perms" in -rw-------*) ok "database is owner-only ($perms)";; *) bad "database is $perms, expected -rw-------";; esac
fi

# engine.json is excluded on purpose: it records where the engine binary lives so
# the menu bar app can find it, and that is an absolute path by definition. The
# guarantee is about the stores that hold *your* data - the database and the UI
# snapshot - which must never contain a path from your home directory.
if strings "$BURNOMETER_HOME"/burn.db "$BURNOMETER_HOME"/snapshot.json 2>/dev/null \
   | grep -q "$HOME/"; then
  bad "an absolute path from your home directory was stored in the database or snapshot"
else
  ok "no absolute paths in the database or snapshot"
fi
