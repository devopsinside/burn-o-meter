# Changelog

Notable changes per release. Dates are the release date; the format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[semantic versioning](https://semver.org/spec/v2.0.0.html) with the caveat that
this is alpha software and the `0.x` line may still move things.

Findings are recorded with the evidence that produced them, because a number
without provenance is the thing this project exists to avoid.

## [Unreleased]

### Fixed

- **A pricing refresh that returned nothing was saved, silently unpricing every
  model.** A refreshed snapshot shadows the packaged one completely, so a file
  written with zero models left the machine unable to price anything — the model
  carrying 97% of one user's spend rendered as an em dash, with no error anywhere
  and no symptom but a cost that had stopped existing. Upstream was healthy when
  checked, so the bad response was transient and we persisted it. `refresh_snapshot`
  now refuses a result below a plausibility floor and leaves the existing file
  alone, and a refreshed snapshot has to *earn* its precedence on every read —
  because refusing to write only helps machines that do not already have a bad
  file. An empty, truncated or wrong-shaped file now falls back rather than
  raising, since that check runs on the path of every command.
- **`reprice` relabelled locally-served events as `unpriced`.** It made the pricing
  decision itself instead of calling the function the scan path calls, and never
  learned about `not_metered` — so one `burn-o-meter reprice` turned "there is no
  rate, it ran on your own hardware" into "we do not know the rate". Invisible,
  because both render as an em dash. `iter_priceable` did not even select
  `upstream_provider`. The decision now lives in one function, `decide_cost`, that
  both callers use; the duplication was what let them drift.
- The menu bar's `·` between percentage and spend read as a stray full stop at
  that size. Replaced with a wider gap, which is also narrower than `" · "` was.
- **The menu bar glyph sat low against the number beside it.** The dial's lower
  ends reach further below its centre than the apex reaches above, so the shape
  drawn from those proportions was not centred in its own box — measured margins
  of 0.117 at the bottom against 0.201 at the top, putting the ink's centre at
  0.458. AppKit centres an image's *frame*, not what is drawn inside it, so that
  margin became a visible drop.

  Centring the ink in its frame was necessary and **not sufficient** — it was still
  visibly low. The frame's centre is not where the text's ink sits: a title of
  digits and a percent sign has no descender, so it rides high in the font box
  AppKit centres. Aligning to the text means sitting slightly *above* the frame's
  centre, at 0.514.

  That number is measured rather than reasoned. `--probe-alignment` renders the
  real status-bar button and compares the alpha-weighted centroid of the glyph's
  ink against the title's; sweeping the offset put the crossing at 0.083, leaving
  0.003pt of residual against 0.194pt. `--check-layout` fails if the ink moves off
  that target, and rejects every earlier value.

  It took three wrong measurements to get one right, and two of them reported
  perfect alignment for an icon that visibly was not: taking the midpoint of the
  ink's extremes, which at these sizes rounds every candidate to the same pixel;
  rendering the button into a hand-built bitmap, which draws nothing for a
  status-bar button and left the measurements reading uninitialised memory; and
  splitting glyph from title at a hardcoded column, which sliced into the digits
  and contaminated the centroid. The probe now uses the rep AppKit provides, an
  alpha-weighted centroid, and a split found by locating the blank gutter between
  the two.
- **The rate-limit rows hid the one thing needed to read them.** A quota's age was
  shown only once the reading had aged past its sampling interval, and the note
  explaining that Claude records these about every 15 minutes was suppressed on
  exactly the same condition. So when the figure looked fresh — which is when
  someone is most likely to be comparing it against the Claude app — the popover
  said "healthy" and offered nothing to explain a one-point difference. The age now
  shows for a periodically-sampled source whenever it is a minute or more old, and
  the note shows whenever such a source is present, because the caveat is a
  property of the source rather than an occasional condition.

### Changed

- **The status item shows the meter-and-flame glyph** from the app icon and the org
  avatar, in place of the flame emoji. It is a template image, so macOS tints it for
  the bar it lands in and inverts it while the item is clicked; the brand orange
  stays with the app icon and popover, where there is a background to own it.
  `--preview-menubar-icon` renders it at 1x/2x/3x on both a light and a dark bar,
  because a template is only ever seen tinted.
- **Every GitHub Action is pinned to a commit SHA.** A tag can be repointed by its
  owner, which is how the trivy-action and kics-github-action compromises reached
  their users. Versions already in use are unchanged — this pins, it does not
  upgrade — and Dependabot keeps the pins current, now with a seven-day cooldown so
  a freshly published release is not proposed within hours of appearing.
- Workflow inputs reach shell scripts through `env` rather than `${{ }}`
  interpolation, which is substituted before the shell sees the text at all.
- **The pricing refresh pins both scheme and host.** SECURITY.md has always said
  there is exactly one egress; it is now a property of the code rather than a
  statement of intent, enforced by tests. Without it the one function allowed to
  open a socket would also open `file:///etc/passwd`.
- A test asserts the macOS app knows every cost basis the engine can write. It
  decodes with `?? .unpriced`, so an unknown basis would not render as unknown — it
  would claim "no published rate", which is a specific and wrong statement.

## [0.6.1] — 2026-09-10

### Fixed

- **The menu bar only updated when you clicked it.** The poll timer re-read the
  payload every two seconds but nothing regenerated it: the only code path that
  scans ran on launch, on opening the popover, and from *Scan now*. The design
  assumed the background agent kept the file current, and that agent is optional
  and off by default — so on most machines the number moved only when looked at.
  The timer now scans for itself whenever the payload is older than a minute,
  which is skipped entirely when the agent *is* running, so the two never
  duplicate work. Verified by watching the payload regenerate on a 60-second
  cadence with the popover never opened.
- **A stale quota reading was presented as current, so the percentage could sit
  at an old value after usage had moved on** — reported as being stuck at 85%
  after the limit was actually reached. A reading's age came from the payload's
  own `age_seconds`, which the writer computes once and never revises, so with
  nothing rewriting the payload that number stayed frozen: a four-hour-old
  reading still classified as `current`, kept leading the menu bar, and kept
  showing its percentage as a statement about now. Age is now derived from
  `observed_at` against the clock, so it grows, and a reading that ages out is
  drawn back instead of being trusted. The "as of X ago" labels and the stale
  banner were reading the same frozen number and are fixed by the same change.
- `--check-freshness` joins `--check-layout` as a CI self-check on the app
  binary. It asserts the classification boundaries, that a stale reading never
  reaches the menu bar, that 100% does, and that a payload without `observed_at`
  still falls back cleanly. Reintroducing either bug fails it.

### Changed

- The FAQ's "numbers look frozen" row documented the first bug as expected
  behaviour and told users to install the background agent to work around it. It
  now says that only applies to the CLI.

## [0.6.0] — 2026-08-29

### Added

- **Kimi Code**, read from `~/.kimi-code/sessions/*/*/agents/*/wire.jsonl`. Like
  OpenCode it routes to any provider, so it covers Moonshot's own models and
  anything you run locally. Relocatable via `KIMI_CODE_HOME`.
- **The packaged pricing snapshot now carries 287 models from 16 vendors, not 132
  from 6.** `DEFAULT_VENDORS` had been extended with Moonshot, Zhipu, Alibaba,
  MiniMax and the inference hosts, but the file was never regenerated — so a
  fresh install could not price Kimi, GLM, Qwen or MiniMax at all, while every
  test that touched the catalog passed on a machine holding a refreshed copy.
  Two tests now hold the file to the vendor list and to pricing one model per
  supported agent, and a third holds the documented model count to what actually
  ships (the docs said 290; the file had 132).
- **`billing.<provider>` now works for every agent, not two.** `BillingConfig`
  named `claude_code` and `codex` as fields and resolved with `getattr`, so a
  setting for any adapter added since parsed, validated, and was then discarded —
  while `doctor` printed advice telling users to set exactly that.

### Verified

- **Kimi's usage is per turn, not cumulative.** `usageScope` reads `turn` and a
  three-turn session recorded outputs of 276, 239 and 202 — falling, so not a
  running total. Checked because Codex looked identical and was cumulative.
- **Its two `token_counting` records restate the usage record rather than adding
  to it.** Across 6/6 real turns, `tokens == inputOther + output` exactly. Summing
  them would have roughly doubled every figure. The identity ships as this
  adapter's integrity check.
- **Ollama truncates a prompt larger than `num_ctx` and reports what it actually
  processed.** 8,000 tokens sent against the default 4,096-token window are
  reported as 2,050; at `num_ctx=16384` the same prompt reports 8,011. The figure
  is honest — it is what the model read — and Kimi records it faithfully. It is
  just not the size of what you typed.

### Removed

- `deepseek-chat` and `deepseek-reasoner` no longer carry a rate. models.dev
  dropped both when regenerating the snapshot, in favour of versioned names like
  `deepseek-v4-flash`. No rate was carried forward for them: the last figure we
  held was 8 days old and could not be re-verified without adding a second
  network destination, and a stale price shown as current is the failure mode
  this project exists to avoid. Set your own in `~/.burn-o-meter/pricing.toml`
  if you need them.

### Fixed

- **A `config.toml` that omitted any key could not be loaded at all.**
  `load_config` read its fallbacks off the dataclasses themselves, but those use
  `slots=True`, so the class attribute is a slot descriptor rather than the
  default value. All five defaults were affected, and the only file that loaded
  was one setting every key — which is the one the docs show and nobody writes.
- **`doctor` advised a setting that did nothing.** It printed
  `set billing.<provider> = "api"` while resolving the mode with its own copy of
  the broken lookup. A test now asserts that following the instruction changes
  what `doctor` reports.
- **The network-egress table let rich elide the destination**, so in a narrow
  pane the single opt-in egress rendered as `https://models.dev/api…` — a
  disclosure table dropping the thing it discloses. The column folds now.
- `across 1 requests`, and three more counts that read `file(s)` / `model(s)`.

### Changed

- Report tests no longer depend on the width of the terminal running them. One
  assertion was green locally at 120 columns and red on CI at 80; sweeping 30 to
  400 found two more that passed only at CI's width. CI now runs the suite at 60
  and 200 as well. The G6 test mattered most: it asserts the home path never
  appears in `doctor` output, and a path wrapped mid-token would have slipped
  past a plain substring check while being fully disclosed.

## [0.5.0] — 2026-08-29

### Verified

- **Local models are measurable through OpenCode**, end to end: Ollama running
  `qwen3:0.6b` on this machine, OpenCode pointed at `localhost:11434`, and the
  session read, reconciled and reported like any other.
- **Ollama persists nothing**, confirmed on a real install rather than cited. It
  returns `prompt_eval_count` and `eval_count` per request and keeps none of it;
  `~/.ollama` holds an SSH keypair and a cache, and `/api/history` and `/api/usage`
  are both 404.

### Added

- **Local models report `not_metered` rather than `unpriced`.** The first means "we
  do not know the rate"; the second, "there is no rate" — your own hardware served
  the tokens. Both show no dollar figure, but only one explains why. `doctor` and
  the scan summary count them apart.
- `usage_events.upstream_provider` records who actually served the tokens, since a
  router like OpenCode reports OpenAI, DeepSeek and a local model through one
  adapter and only this distinguishes them.

### Fixed

- **The OpenCode test fixture was never committed.** `.gitignore` carries `*.db` to
  keep the user's own database out of the repository, and it caught the fixture too
  — so those tests passed locally and errored on every CI run. Fixtures are source,
  not user data; a test now asserts this one is tracked.
- **Six tests decayed with the calendar.** The Claude plan-usage fixture carried
  absolute timestamps while the adapter drops samples older than seven days, so
  they were always going to expire, and did, exactly a week later. The fixture is
  now shifted to end at "now", preserving the spacing the tests rely on.

### Changed

- **Schema version 2, with the project's first migration.** `CREATE TABLE IF NOT
  EXISTS` never alters an existing table, so an upgrade would silently keep the old
  shape and fail on the first insert. Existing OpenCode rows are dropped so the next
  scan rebuilds them with the new field — they cannot be repaired in place, because
  `upsert_events` is `DO NOTHING` by design. Nothing is lost: every row derives from
  logs the provider still holds. Scan offsets are left alone.

## [0.4.0] — 2026-08-25

### Added

- **OpenCode adapter.** One adapter reaches DeepSeek, Kimi, GLM, Qwen and MiniMax —
  whatever OpenCode is pointed at is measured the same way — and it is the only
  route to **local models**, which record nothing themselves. Its own per-session
  totals are used as an integrity check, the way Codex's running total is.
- `OPENCODE_DATA` relocates OpenCode's data directory, alongside the existing
  `CLAUDE_CONFIG_DIR` and `CODEX_HOME`.

### Fixed

- **Reasoning tokens were being dropped for OpenCode.** Its `output` field excludes
  reasoning, but reasoning is billed at the output rate — the opposite of both
  existing adapters. Our price for a real billed session came out **1.87% under**
  the figure OpenCode recorded for itself, and the gap was exactly its reasoning
  count at the output rate. Folding reasoning into output makes the two agree to
  the cent.
- **OpenCode's `cost` column is not trusted at zero.** The same model produced
  `0.0` on a ChatGPT subscription and `0.00457125` on an API key, so zero means
  either "free" or "not billed per token". Reading it as money spent would
  reproduce the `$0.00` failure this project exists to avoid; tokens are priced
  from our own catalog instead.

### Verified

- **The reasoning assumption in the shipped adapters**, before porting anything.
  Codex counts reasoning *inside* output — 174 of 174 real blocks with non-zero
  reasoning satisfy `input + output == total`. Claude Code has no reasoning field
  at all, and its `output_tokens_details.thinking_tokens` is a breakdown *within*
  output across 2,444 blocks. Both are correct as shipped; OpenCode is the outlier.
  All three semantics are now pinned by tests.
- **Cursor cannot be supported honestly.** `ai-code-tracking.db` sounds exactly
  right and has no token, cost or cache column anywhere — it measures how much of
  your code was AI-written, not what it cost.
- **Gemini CLI** records no usage-shaped keys in its session files; the only
  `token` fields in `~/.gemini` are OAuth credentials.

### Security

- The OpenCode database holds `account`, `control_account` and `credential` tables
  with access tokens, and `part` with conversation text, in the same file as the
  usage. A filename deny-list cannot help when the secret is a column in the next
  table, so every query names its columns, the readable tables are declared in
  code, and a test asserts the declaration matches what is actually queried.

## [0.3.4] — 2026-08-24

### Fixed

- **The menu bar app could not start itself.** macOS refuses to register a login
  item for an app outside `/Applications`, so one built in place worked until the
  first reboot — after which the icon was gone and Spotlight was the only way back.
  `macos/make-app.sh --install` builds and installs in one step.
- Builds from before the repository moved pointed their update check at a
  repository that is now private.

### Added

- `--enable-login-item` / `--disable-login-item`, so setup is scriptable and, more
  usefully, verifiable.
- `./install.sh` and `./uninstall.sh` — one command for the CLI, the app, the login
  item and background scanning. Deliberately not a `curl | sh` one-liner.

### Changed

- The docs now say plainly that `brew`, `pipx` and `uv` install the **command line
  tool only**; the menu bar app is a separate, optional build.
- Bottles build from the tag's source archive rather than a release asset, so one
  release can hold everything. GitHub releases are immutable: assets attach only at
  creation, and a tag consumed by one can never be reused.

## [0.3.1] — 2026-08-23

### Added

- `burn-o-meter pricing refresh`, which was documented in three places and did not
  exist.
- Manual **Check for Updates** in the popover and gear menu, with an **Upgrade Now**
  that runs your own package manager. Nothing checks automatically.

### Fixed

- The app labelled itself `0.1.0` for three releases, from a hardcoded fallback.
- The Homebrew formula installed v0.2.0 while the package was 0.3.0 — which meant
  the bottles workflow built 0.2.0 bottles for the v0.3.0 release.

## [0.3.0] — 2026-08-23

### Added

- Homebrew tap with prebuilt bottles for arm64 Tahoe, Sequoia and Sonoma.
- macOS app icon; popover sized to the display it opens on; four menu bar title
  widths.
- Per-project drill-down, cache efficiency panel, rate-limit reset countdowns.

### Fixed

- The popover clipped its own content once there was enough data — AppKit clips an
  oversized popover rather than scrolling it, and it clips the top.
- Quota readings whose window has already reset are dropped rather than shown.

## [0.2.0] — 2026-08-23

### Added

- 290 priced models, including a 1-hour cache-write rate no public database carries.
- `models`, `projects` and `blocks` reports; `--json` on every report.

### Fixed

- A published rate of `0` means "included in your plan", not "free".

## [0.1.0] — 2026-08-22

First alpha. Claude Code and Codex adapters, TTL-aware pricing, SQLite storage,
the macOS menu bar app, and the security guarantees with their enforcing tests.

[0.6.1]: https://github.com/devopsinside/burn-o-meter/releases/tag/v0.6.1
[0.6.0]: https://github.com/devopsinside/burn-o-meter/releases/tag/v0.6.0
[0.5.0]: https://github.com/devopsinside/burn-o-meter/releases/tag/v0.5.0
[0.4.0]: https://github.com/devopsinside/burn-o-meter/releases/tag/v0.4.0
[0.3.4]: https://github.com/devopsinside/burn-o-meter/releases/tag/v0.3.4
[0.3.1]: https://github.com/devopsinside/burn-o-meter/releases/tag/v0.3.1
[0.3.0]: https://github.com/devopsinside/burn-o-meter/releases/tag/v0.3.0
[0.2.0]: https://github.com/devopsinside/burn-o-meter/releases/tag/v0.2.0
[0.1.0]: https://github.com/devopsinside/burn-o-meter/releases/tag/v0.1.0
