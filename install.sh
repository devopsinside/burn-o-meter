#!/usr/bin/env bash
#
# One command to get burn-o-meter running: the CLI, the menu bar app, background
# scanning, and the login item that makes the app come back after a reboot.
#
#   git clone https://github.com/devopsinside/burn-o-meter
#   cd burn-o-meter && ./install.sh
#
# Deliberately NOT a `curl | sh` one-liner. That asks you to execute code you have
# not seen, from a host that could be impersonated, with your own privileges. Here
# you clone first, so this file is on your disk and readable before it runs. See
# SECURITY.md.
#
# Safe to re-run: every step checks before acting.
#
#   --cli-only     skip the menu bar app
#   --no-login     do not register the login item
#   --no-agent     do not schedule background scanning
#
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

WANT_APP=1; WANT_LOGIN=1; WANT_AGENT=1
for arg in "$@"; do
  case "$arg" in
    --cli-only) WANT_APP=0; WANT_LOGIN=0 ;;
    --no-login) WANT_LOGIN=0 ;;
    --no-agent) WANT_AGENT=0 ;;
    -h|--help) awk 'NR>1 && /^#/ {sub(/^# ?/, ""); print; next} NR>1 {exit}' "$0"; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

bold=$'\033[1m'; dim=$'\033[2m'; green=$'\033[32m'; red=$'\033[31m'; off=$'\033[0m'
step() { printf '%s==>%s %s\n' "$bold" "$off" "$1"; }
ok()   { printf '    %s✓%s %s\n' "$green" "$off" "$1"; }
warn() { printf '    %s!%s %s\n' "$red" "$off" "$1"; }
note() { printf '    %s%s%s\n' "$dim" "$1" "$off"; }

[ "$(uname -s)" = "Darwin" ] || { echo "The menu bar app is macOS only. Use --cli-only elsewhere." >&2; [ "$WANT_APP" = 1 ] && exit 1; }

# ---------------------------------------------------------------- the CLI
step "Command line tool"
if command -v burn-o-meter >/dev/null 2>&1; then
  ok "already installed: $(burn-o-meter --version)"
elif command -v brew >/dev/null 2>&1; then
  # Homebrew is chatty: auto-update, env hints, trust notices about unrelated taps,
  # and the formula's own caveats about a step this script is about to perform. Keep
  # the progress this script prints legible by filtering to what actually happened.
  export HOMEBREW_NO_AUTO_UPDATE=1 HOMEBREW_NO_ENV_HINTS=1
  brew tap devopsinside/burn-o-meter https://github.com/devopsinside/burn-o-meter >/dev/null 2>&1 || true
  if brew install devopsinside/burn-o-meter/burn-o-meter 2>&1 | grep -iE '^(==> )?(Pouring|Building|Error)' | sed 's/^/    /'; then
    :
  fi
  command -v burn-o-meter >/dev/null 2>&1 || { warn "Homebrew install did not complete"; exit 1; }
  ok "installed with Homebrew"
elif command -v pipx >/dev/null 2>&1; then
  pipx install "$HERE"
  ok "installed with pipx"
elif command -v uv >/dev/null 2>&1; then
  uv tool install "$HERE"
  ok "installed with uv"
else
  warn "no brew, pipx or uv found — install one, then re-run this script"
  note "https://brew.sh  ·  pipx: python3 -m pip install --user pipx"
  exit 1
fi
hash -r 2>/dev/null || true
command -v burn-o-meter >/dev/null 2>&1 || { warn "burn-o-meter is not on PATH after install"; exit 1; }

# ---------------------------------------------------------------- first scan
step "Reading your agent logs"
burn-o-meter scan
note "nothing left your machine; see: burn-o-meter doctor --security"

# ---------------------------------------------------------------- the app
if [ "$WANT_APP" = 1 ]; then
  step "Menu bar app"
  if ! command -v swift >/dev/null 2>&1; then
    warn "Swift not found, so the app cannot be built"
    note "install Apple's tools with: xcode-select --install"
    note "the command line tool above works without it"
  else
    # --install puts it in /Applications, which macOS requires before it will
    # register a login item at all.
    ./macos/make-app.sh --install >/dev/null
    ok "built and installed to /Applications"

    if [ "$WANT_LOGIN" = 1 ]; then
      if /Applications/burn-o-meter.app/Contents/MacOS/burn-o-meter --enable-login-item >/dev/null 2>&1; then
        ok "starts automatically at login"
      else
        warn "could not register the login item"
        note "allow it in System Settings → General → Login Items"
      fi
    fi

    pkill -f '/Applications/burn-o-meter.app/Contents/MacOS/' 2>/dev/null || true
    sleep 1
    open /Applications/burn-o-meter.app
    ok "running — look for 🔥 in your menu bar"
  fi
fi

# ---------------------------------------------------------------- background scan
if [ "$WANT_AGENT" = 1 ]; then
  step "Background scanning"
  if burn-o-meter agent status 2>/dev/null | grep -q 'loaded'; then
    ok "already scheduled"
  else
    burn-o-meter agent install >/dev/null
    ok "scheduled every 60s at background priority"
  fi
fi

# ---------------------------------------------------------------- summary
step "Done"
printf '\n'
burn-o-meter today || true
printf '\n'
note "burn-o-meter today | models | daily | doctor"
note "remove everything with: ./uninstall.sh"
