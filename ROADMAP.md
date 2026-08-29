# Roadmap

What is built, what is next, and what will not be built. Updated as things ship
rather than written once — if a line here is stale, that is a bug.

**One at a time.** Each agent is installed, run, and its real output read before
any adapter is written. Every accounting trap found so far was invisible in
documentation and obvious in the data:

- Claude Code writes the same message up to 7 times — a naive sum overcounts 2.5×
- Codex repeats its final event, so summing its own per-turn figure added
  1,001,920 phantom tokens in one session
- A published rate of `0` means "included in your plan", not "free"
- Kimi Code writes two `token_counting` records restating each turn's usage;
  adding them to the usage record would roughly double every figure
- Ollama truncates a prompt larger than `num_ctx` and reports the tokens it
  actually processed — 8,000 tokens sent against the default 4,096-token window
  are reported as 2,050. The figure is honest; it is just not the size of what
  you typed. Verified directly against Ollama at two window sizes

None of those would have been caught by reading docs. So: install it, run it,
read what it wrote, *then* write the adapter.

---

## Shipped — v0.6.0

| | |
|---|---|
| **Agents** | Claude Code, Codex CLI, OpenCode, Kimi Code (the last two route to anything, including local models) |
| **Quota** | Claude (exact, via the desktop app's plan records), Codex (exact) |
| **Pricing** | 287 models · a 1-hour cache-write rate no public database carries |
| **Surfaces** | CLI (`today`, `models`, `daily`, `projects`, `sessions`, `blocks`, `doctor`) · macOS menu bar · background agent |
| **Quality** | 326 tests · security guarantees enforced in CI · reconciled against real logs |
| **Analytics** | per-model cost, cache hit rate, effective $/Mtok, cache savings, rolling windows |
| **Install** | `./install.sh` does everything · Homebrew tap with prebuilt bottles · pipx · uv · a release archive needing no toolchain |
| **macOS app** | app icon · popover sized to the display it opens on · four menu bar title widths |

---

## Next, in order

### 1. Other providers through the agents we already support

Claude Code and Codex can both be pointed at another provider, and rates for 290
models already ship — so **this may need no new code at all**. Point Codex at
DeepSeek via `model_provider`, run a task, and check the model is named, priced,
and that the integrity check still reconciles.

A provider emitting a different `token_count` shape would surface as a *failed
reconciliation* rather than a wrong number. That is what the check is for.

*Small. Validates a claim the pricing table already makes.*

### 2. GitHub Copilot CLI

Two possible sources — an opt-in OpenTelemetry export, and a session-state
events log that may exist without it. Which to use gets decided against a real
install, not from documentation.

### 3. Budgets and alerts

"Tell me when today crosses $20" or "when this window is 3× my median" — the
numbers to answer both already exist; what is missing is a threshold to store and a
way to say so.

Three things have to be settled first, because getting them wrong makes the feature
worse than not having it:

- **What a threshold means on a subscription.** A dollar budget is real spend on an
  API key and a counterfactual on Claude Max. Alerting "you have spent $20" to
  someone who cannot spend money would be the same fabrication this project refuses
  everywhere else. Subscription budgets likely have to be expressed against the
  rate-limit window instead.
- **Notification without a daemon that talks.** The background agent exists to scan
  and is silent by design. Giving it a voice means it needs a reason to wake, state
  to remember what it has already said, and a rule against saying it twice.
- **Not crying wolf.** A threshold that fires every day is noise, and a rolling
  window that spikes normally will fire constantly. It probably needs to compare
  against your own history, the way `blocks` already does with its median.

*Deliberately after the adapters: an alert about two agents is less useful than an
accurate figure across many.*

### Then

Amp, Droid, Goose, Kilo, OpenClaw, Qwen Code. Locations are mapped; each needs
the same install-and-verify pass.

---

## Not building, and why

**Ollama, directly.** Verified on a real install rather than cited: it *does*
return `prompt_eval_count` and `eval_count` per request, and persists none of it.
`~/.ollama` holds an SSH keypair and a model-recommendations cache, nothing else,
and `/api/history` and `/api/usage` are both 404 ([ollama#11118][o1],
[ollama#8573][o2]). Local inference is measurable only through a harness that
records it — which is now demonstrated end to end: OpenCode pointed at
`localhost:11434` produced a session burn-o-meter reads like any other.

**Antigravity.** Credit-based rather than token-based, stored as undocumented
protobuf — and its state table holds an OAuth token plus the account holder's
name and email beside the data. Wrong trade for a tool whose promise is that it
touches neither.

**Gemini CLI.** Deprecated in favour of Antigravity. Checked anyway: its session
files carry no usage-shaped keys at all, and the only `token` fields in `~/.gemini`
are in `oauth_creds.json` — credentials the deny-list exists to keep us away from.

**Cursor.** `~/.cursor/ai-tracking/ai-code-tracking.db` sounds exactly right and is
not. Its schema has no token, cost or cache column anywhere; `model` appears only as
a label on code-provenance rows. What it records is how much of your code was
AI-written — `linesAdded`, `humanLinesAdded`, `v2AiPercentage` — never what it cost.
Its usage tables were also empty. Measurable only where the tool records it, same as
Ollama.

**Anything requiring a proxy.** Asking users to run LiteLLM in front of
everything would collect beautiful data and would be a different product.

**Live/authenticated quota lookup.** Anthropic's terms prohibit automated access
to their service using subscription credentials, and enforcement lands on the
*user's* account. See [SECURITY.md](SECURITY.md).

[o1]: https://github.com/ollama/ollama/issues/11118
[o2]: https://github.com/ollama/ollama/issues/8573

---

## Also wanted

- A signed, notarised app and a Homebrew **cask** for it (needs an Apple Developer
  account). The CLI already installs via a Homebrew **tap** with prebuilt bottles;
  it is the `.app` that cannot be distributed without notarisation
- Publishing to PyPI
- Windows and Linux tray apps — the engine is already portable; only the shell is not

## Helping

The most useful contribution right now is **running it on a machine that is not
the author's** and reporting where the numbers disagree with your bill. Adding an
agent is a single file plus a registry line — see
[docs/adding-an-agent.md](docs/adding-an-agent.md) and
[CONTRIBUTING.md](CONTRIBUTING.md).
