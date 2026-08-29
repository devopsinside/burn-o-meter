# Changelog

Notable changes per release. Dates are the release date; the format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[semantic versioning](https://semver.org/spec/v2.0.0.html) with the caveat that
this is alpha software and the `0.x` line may still move things.

Findings are recorded with the evidence that produced them, because a number
without provenance is the thing this project exists to avoid.

## [0.6.0] — 2026-08-29

### Added

- **Kimi Code**, read from `~/.kimi-code/sessions/*/*/agents/*/wire.jsonl`. Like
  OpenCode it routes to any provider, so it covers Moonshot's own models and
  anything you run locally. Relocatable via `KIMI_CODE_HOME`.
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

[0.6.0]: https://github.com/devopsinside/burn-o-meter/releases/tag/v0.6.0
[0.5.0]: https://github.com/devopsinside/burn-o-meter/releases/tag/v0.5.0
[0.4.0]: https://github.com/devopsinside/burn-o-meter/releases/tag/v0.4.0
[0.3.4]: https://github.com/devopsinside/burn-o-meter/releases/tag/v0.3.4
[0.3.1]: https://github.com/devopsinside/burn-o-meter/releases/tag/v0.3.1
[0.3.0]: https://github.com/devopsinside/burn-o-meter/releases/tag/v0.3.0
[0.2.0]: https://github.com/devopsinside/burn-o-meter/releases/tag/v0.2.0
[0.1.0]: https://github.com/devopsinside/burn-o-meter/releases/tag/v0.1.0
