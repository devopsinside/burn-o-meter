#!/usr/bin/env bash
#
# Removes everything burn-o-meter installed: the login item, the background scan,
# the menu bar app, the command line tool, and — only if you ask — your data.
#
#   ./uninstall.sh              # keeps ~/.burn-o-meter
#   ./uninstall.sh --purge      # deletes it too
#
# Your agents' own logs are never touched. burn-o-meter only ever read them.
#
set -euo pipefail

PURGE=0
for arg in "$@"; do
  case "$arg" in
    --purge) PURGE=1 ;;
    -h|--help) sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

bold=$'\033[1m'; dim=$'\033[2m'; green=$'\033[32m'; off=$'\033[0m'
step() { printf '%s==>%s %s\n' "$bold" "$off" "$1"; }
ok()   { printf '    %s✓%s %s\n' "$green" "$off" "$1"; }
note() { printf '    %s%s%s\n' "$dim" "$1" "$off"; }

APP=/Applications/burn-o-meter.app

step "Login item"
if [ -x "$APP/Contents/MacOS/burn-o-meter" ]; then
  "$APP/Contents/MacOS/burn-o-meter" --disable-login-item >/dev/null 2>&1 || true
  ok "unregistered"
else
  note "app not present; nothing to unregister"
fi

step "Background scanning"
if command -v burn-o-meter >/dev/null 2>&1; then
  burn-o-meter agent uninstall >/dev/null 2>&1 || true
fi
# brew services writes its own plist under a different label.
if command -v brew >/dev/null 2>&1; then
  brew services stop burn-o-meter >/dev/null 2>&1 || true
fi
rm -f ~/Library/LaunchAgents/com.burn-o-meter.scan.plist \
      ~/Library/LaunchAgents/homebrew.mxcl.burn-o-meter.plist
ok "stopped and removed"

step "Menu bar app"
pkill -f 'burn-o-meter.app/Contents/MacOS/' 2>/dev/null || true
sleep 1
rm -rf "$APP" macos/build/burn-o-meter.app
ok "removed"

step "Command line tool"
command -v brew >/dev/null 2>&1 && { brew uninstall burn-o-meter >/dev/null 2>&1 || true; \
                                     brew untap devopsinside/burn-o-meter >/dev/null 2>&1 || true; }
command -v pipx >/dev/null 2>&1 && pipx uninstall burn-o-meter >/dev/null 2>&1 || true
command -v uv   >/dev/null 2>&1 && uv tool uninstall burn-o-meter >/dev/null 2>&1 || true
hash -r 2>/dev/null || true
if command -v burn-o-meter >/dev/null 2>&1; then
  note "still on PATH at $(command -v burn-o-meter) — installed some other way?"
else
  ok "removed"
fi

step "Your data"
if [ "$PURGE" = 1 ]; then
  rm -rf ~/.burn-o-meter
  ok "$HOME/.burn-o-meter deleted"
else
  if [ -d ~/.burn-o-meter ]; then
    note "kept at $HOME/.burn-o-meter — delete with: ./uninstall.sh --purge"
    note "nothing is lost by deleting it; a rescan rebuilds it from your agents' logs"
  fi
fi

step "Done"
note "your Claude Code and Codex logs were never modified"
