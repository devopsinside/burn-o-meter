#!/usr/bin/env bash
#
# Apply the repository settings that cannot live in a file.
#
# Description and topics work on any plan. Branch protection, private
# vulnerability reporting and CodeQL all require the repository to be public, or
# a paid plan — GitHub returns 403/404 otherwise. This script applies whatever
# the current plan allows and reports the rest rather than failing, so it can be
# run now and re-run the moment the repository goes public.
#
#   gh auth login          # once
#   scripts/setup-repo.sh
set -uo pipefail

REPO="${1:-devopsinside/burn-o-meter}"
DESC="See what your AI coding agents really cost — tokens, spend, cache efficiency and rate limits. Works with Claude Code and Codex, more coming. Runs entirely on your machine: no account, no telemetry, no network."

say()  { printf '  %s\n' "$*"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
skip() { printf '  \033[33m—\033[0m %s\n' "$*"; }

echo "Configuring $REPO"

# ---------------------------------------------------------------- always ----
if gh repo edit "$REPO" --description "$DESC" \
    --add-topic claude-code --add-topic codex --add-topic llm \
    --add-topic cost-tracking --add-topic token-usage --add-topic macos \
    --add-topic menubar --add-topic swiftui --add-topic python \
    --add-topic privacy-first \
    --enable-issues --enable-wiki=false --enable-projects=false >/dev/null 2>&1; then
  ok "description and topics"
else
  say "could not set description/topics"
fi

# --------------------------------------------------- public or paid only ----
if gh api -X PUT "/repos/$REPO/private-vulnerability-reporting" >/dev/null 2>&1; then
  ok "private vulnerability reporting"
else
  skip "private vulnerability reporting — public repositories only"
fi

# Free on public repositories, and worth having on a project whose fixtures
# deliberately contain credential-shaped canaries: push protection is what stops
# a real key ever being committed by accident.
if gh api -X PATCH "/repos/$REPO" --input - >/dev/null 2>&1 <<'JSON'
{
  "security_and_analysis": {
    "secret_scanning": { "status": "enabled" },
    "secret_scanning_push_protection": { "status": "enabled" },
    "secret_scanning_non_provider_patterns": { "status": "enabled" }
  }
}
JSON
then
  ok "secret scanning and push protection"
else
  skip "secret scanning — public repositories only"
fi

# Alerts must be on before automated fixes can be.
if gh api -X PUT "/repos/$REPO/vulnerability-alerts" >/dev/null 2>&1; then
  ok "Dependabot vulnerability alerts"
else
  skip "Dependabot vulnerability alerts"
fi

if gh api -X PUT "/repos/$REPO/automated-security-fixes" >/dev/null 2>&1; then
  ok "Dependabot security updates"
else
  skip "Dependabot security updates"
fi

# Required checks are the CI job names. Keeping them in one place here means a
# renamed job is a one-line fix rather than a silently unenforced gate.
CHECKS='["engine (py3.11)","engine (py3.12)","engine (py3.13)","security guarantees","wheel installs clean","macOS app builds"]'

if gh api -X PUT "/repos/$REPO/branches/main/protection" --input - >/dev/null 2>&1 <<JSON
{
  "required_status_checks": { "strict": true, "contexts": $CHECKS },
  "enforce_admins": false,
  "required_pull_request_reviews": null,
  "restrictions": null,
  "required_linear_history": true,
  "allow_force_pushes": false,
  "allow_deletions": false,
  "required_conversation_resolution": true
}
JSON
then
  ok "branch protection on main (CI required, no force pushes)"
else
  skip "branch protection — needs GitHub Pro, or a public repository"
fi

echo
say "CodeQL runs automatically once the repository is public; the workflow"
say "skips itself while private because code scanning needs Advanced Security."
