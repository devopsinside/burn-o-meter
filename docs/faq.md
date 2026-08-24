# FAQ

### Does it track the Claude and ChatGPT desktop apps, or only the CLIs?

Mostly the CLIs, and the reason is the same for both apps.

| Surface | What you get |
|---|---|
| Claude Code (CLI) | Everything — tokens, cost, per-model, cache |
| Codex (CLI) | Everything, plus exact quota from its own logs |
| Claude desktop app | Quota only — no tokens, no cost |
| ChatGPT desktop app | Nothing |

The **Claude desktop app** does contribute something real: the 5-hour and 7-day
percentages come from a file it writes, and those are plan-wide, so chats you have
in the app already move that bar. But each sample records only a timestamp and two
percentages — there is no per-surface split, so burn-o-meter cannot tell you how
much of your window went to app chats versus Claude Code, and will not guess. No
tokens are recorded either, which is why app usage never appears as dollars.

The **ChatGPT desktop app** stores conversations as opaque files and nothing that
looks like usage, quota or billing. The only ways to get numbers out would be to
read conversation contents — which burn-o-meter refuses to do, see
[SECURITY.md](../SECURITY.md) — or to call an account API with your login, which the
providers' terms do not allow. So it reports nothing rather than guessing.

The pattern: burn-o-meter measures agents that write their own token accounting to
disk. Coding CLIs do; chat GUIs do not.

### A provider I do not use is showing up. Why?

It is only listed if it left evidence on disk. A quota card appears when that
provider's own logs contained a rate-limit reading, and a model row appears when
its logs contained token usage — so a tool you never installed cannot appear at
all. If Codex is showing, its logs exist and were read; `burn-o-meter models` will
show what it actually consumed.

# Troubleshooting

`burn-o-meter doctor` answers most of it. It prints where the data lives, which
agents were found, how many files were read, which models had no price, and where
every price came from — without printing anything from your conversations.

| Symptom | Likely cause and fix |
| --- | --- |
| `command not found` | The install did not put it on PATH. A virtualenv only does that while activated — see [installing](install.md#the-cli). |
| `no agent logs found` | Logs are somewhere non-default. Set `CLAUDE_CONFIG_DIR` or `CODEX_HOME` and re-run `doctor`. |
| Numbers look frozen | Nothing is scanning in the background. `burn-o-meter agent status`, then `agent install` if it is not loaded. |
| Menu bar shows `—` | No data scanned yet. Run `burn-o-meter scan`. |
| Menu bar icon missing after a reboot | Almost always this: nothing is starting the app. macOS only registers a login item for an app in `/Applications`, so one built in place never comes back. Fix both at once with `macos/make-app.sh --install`, then `/Applications/burn-o-meter.app/Contents/MacOS/burn-o-meter --enable-login-item`. |
| Menu bar icon missing, and the app *is* running | Now it is the menu bar being full — macOS drops items that no longer fit without warning, and a notched laptop has less room than it looks. Gear menu → **Menu Bar Shows** → *Icon Only* makes it as small as it gets; ⌘-drag other items to reorder or remove them. |
| Not sure which of those it is | `pgrep -f burn-o-meter.app` — no output means the app is not running, so it is the first row. |
| A model shows `—` for cost | Genuinely unpriced. `doctor` lists these; burn-o-meter will not invent a rate or fall back to `$0.00`. |
| Menu bar % differs from the Claude app | The desktop app writes that figure periodically, so it can lag by minutes. The popover shows the reading's age. |
| Popover looks cut off | Run `--probe-popover` (below). It prints the size the popover actually became, the height its content needs, and the ceiling for your display — please include that in a report. |

Two commands worth knowing:

```bash
burn-o-meter doctor --security      # every file read, every network egress point
/Applications/burn-o-meter.app/Contents/MacOS/burn-o-meter --dump          # what the UI sees, as JSON
/Applications/burn-o-meter.app/Contents/MacOS/burn-o-meter --check-layout  # layout fits on 5 display sizes
/Applications/burn-o-meter.app/Contents/MacOS/burn-o-meter --probe-popover # shows the popover once, reports its real size
```

`--check-layout` measures the view against five representative displays, from an
11-inch Air up to a 27-inch external, so a layout that only fits a big screen fails
in CI rather than on someone's laptop. `--probe-popover` opens the real popover for
a moment and reports what it actually became — the two answer different questions,
and a bug report is far easier to act on with both.

Nothing here is destructive. If you want a clean slate, `rm -rf ~/.burn-o-meter`
loses nothing permanently — a rescan rebuilds it from your agents' own logs.

# Reporting a bug

Before anything else, `./install.sh` is safe to re-run and fixes most setup problems
in one step — it re-checks every part of the install rather than assuming.

Open an issue: **[github.com/devopsinside/burn-o-meter/issues](https://github.com/devopsinside/burn-o-meter/issues)**

Please paste the output of:

```bash
burn-o-meter doctor
burn-o-meter --version && sw_vers -productVersion
```

`doctor` is written to be safe to share — it reports counts, paths of directories
(not files), price sources and versions, never prompt or completion text. Still,
read it before pasting: if your project names are sensitive, set
`project_paths = "hash"` under `[privacy]` in `~/.burn-o-meter/config.toml` and
re-run, and they become stable but unreadable identifiers.

**Never paste a raw agent log.** Those contain your prompts, and often secrets
that were pasted into them. burn-o-meter never reads their content and neither
should an issue thread.

If a number looks wrong, that is the most valuable kind of report — say which
number, what you expected, and what `doctor` shows. Accuracy is the whole point of
this tool, so a disagreement with your bill is a bug worth chasing.

For anything security-related, do not open a public issue — see
[SECURITY.md](../SECURITY.md) for private disclosure.
