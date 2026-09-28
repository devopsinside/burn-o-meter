# burn-o-meter

**See what your AI coding agents really cost.** Tokens, spend, cache efficiency and
rate limits for Claude Code, Codex, OpenCode and Kimi Code — read from the logs
they already write on your machine. No account, no telemetry, nothing sent
anywhere.

<img src="https://raw.githubusercontent.com/devopsinside/burn-o-meter/main/docs/demo.gif" alt="The burn-o-meter menu bar popover: spend for the day, a per-hour chart, Claude and Codex rate limits, cache efficiency and a per-model breakdown." width="440">

## Install

```bash
pipx install burn-o-meter      # or: uv tool install burn-o-meter
```

Then:

```bash
burn-o-meter scan && burn-o-meter today
burn-o-meter statusline install   # Claude's rate-limit %, live from Claude Code
```

Requires Python 3.11+. The macOS menu bar app is a separate, optional build — see
[installing](https://github.com/devopsinside/burn-o-meter/blob/main/docs/install.md).

## What it gets right

Every figure is checked against real logs, because each agent's format hides a
trap that documentation never mentions:

- Claude Code writes the same message up to 7 times — summing naively overcounts
  by about 2.5×. Subagents keep their turns in separate files, and write a
  message while it streams.
- Codex repeats its final usage event, and restates each turn in a second record.
- A published rate of `0` means "included in your plan", not "free"; a model on
  your own hardware is *not metered*, never `$0.00`.
- Ollama truncates prompts past its context window and reports what it read.

Costs on a subscription are labelled API-equivalent, never presented as a bill,
and a model with no known rate shows as unpriced rather than guessed.

## Private by construction

It never reads prompts or completions, never opens credential files, and makes no
network call except `burn-o-meter pricing refresh`, which you run yourself. Each
of those is enforced by tests rather than promised —
[SECURITY.md](https://github.com/devopsinside/burn-o-meter/blob/main/SECURITY.md)
lists them.

[Documentation](https://github.com/devopsinside/burn-o-meter#readme) ·
[Changelog](https://github.com/devopsinside/burn-o-meter/blob/main/CHANGELOG.md) ·
[Issues](https://github.com/devopsinside/burn-o-meter/issues)
