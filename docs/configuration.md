# Configuration

All optional — burn-o-meter works with no config at all.

Optional, at `~/.burn-o-meter/config.toml`:

```toml
[privacy]
# full | basename (default) | hash | none
#   basename → "/Users/you/clients/acme/repo" is stored as "repo"
#   hash     → stable but unreadable; use before sharing a screenshot
project_paths = "basename"

[billing]
# auto (default) | subscription | api
#
# auto looks for evidence: Claude's desktop app records plan utilisation, and
# Codex records a plan type — both exist only for an account on a plan. With no
# evidence it assumes subscription, because "what this would have cost" is true
# either way while "what you were charged" may not be.
#
# **If you pay per token against an API key, set this to "api"** — otherwise
# your real spend is labelled API-equivalent, which reads as hypothetical.
# `burn-o-meter doctor` shows which basis is in effect and whether it was
# detected or assumed.
claude_code = "auto"
codex = "auto"

[retention]
# Quota readings are sampled continuously — Claude's desktop app writes one
# every ~15 min and Codex emits one per turn — so they accumulate far faster
# than usage and are only interesting recently.
quota_days = 90
# Usage events are the record of what you spent; 0 keeps them indefinitely.
# Everything is rebuildable from the provider logs with `scan --force` anyway.
events_days = 0
```

Custom or discounted rates go in `~/.burn-o-meter/pricing.toml`:

```toml
[models."claude-opus-5"]
input = 4.0        # your negotiated rate
```
