# Security

`burn-o-meter` reads conversation transcripts. That makes it a security-sensitive
tool, and it is built as one. This document states the threat model, the
guarantees, and how each guarantee is enforced — so you can audit the claims
rather than take them on trust.

## Reporting a vulnerability

Please **do not** open a public issue.

Use GitHub's private vulnerability reporting:
**[Security → Report a vulnerability](https://github.com/devopsinside/burn-o-meter/security/advisories/new)**

Include what you found, how to reproduce it, and what an attacker gains. You will
get an acknowledgement within 72 hours and credit in the release notes unless you
prefer otherwise.

Particularly interested in: anything that gets message content into the database,
JSON output or a log; anything that opens a file the deny-list should have
stopped; and any network call outside `pricing refresh`.

## Threat model

| Asset | Where it lives | Risk if mishandled |
|---|---|---|
| Conversation transcripts | `~/.claude/projects/*/*.jsonl` | Full prompts and completions — pasted secrets, proprietary source, customer data |
| Codex rollouts | `~/.codex/sessions/**/rollout-*.jsonl` | The same, plus system prompts |
| OpenCode messages | the `part` table in `~/.local/share/opencode/opencode.db` | Conversation text, in the same file as the usage we read |
| Kimi Code prompts | `turn.prompt` and `context.append_message` in `wire.jsonl` | What you typed, verbatim, in the same file as the usage we read |
| **Credentials** | `~/.codex/auth.json`, `~/.gemini/oauth_creds.json`, `~/.claude/sessions/*.key`, `~/.local/share/opencode/auth.json`, the `account` / `credential` tables in `opencode.db`, `api_key` in `~/.kimi-code/config.toml` | Account takeover. **These sit inside directories we scan — two of them inside the very file we read.** |
| Project paths | `cwd` / `gitBranch` in the Claude and Codex logs, `session.directory` in OpenCode, `workDir` in Kimi's session index | Discloses account name, employer, client names, directory layout |
| Our database | `~/.burn-o-meter/burn.db` | An aggregate profile of when and how you work |

Adversaries considered:

- another local user or process reading our database;
- a malicious or compromised dependency exfiltrating during import;
- a future contributor widening a glob and sweeping in a credential file;
- the user themselves accidentally publishing a `--json` export, a screenshot,
  or a stack trace in a bug report.

Explicitly out of scope: an attacker who already has code execution as your
user. At that point they can read the transcripts directly and this tool is not
the weak link.

## Guarantees

### G1 — Prompts and completions are never read

Message content never enters the process beyond the JSON parse, and never
leaves it at all.

Adapters extract through a strict **allowlist** of field names
(`safety.USAGE_FIELDS`, `safety.METADATA_FIELDS`) using the `pluck_int` /
`pluck_str` / `pluck_float` helpers, which return **scalars only**. Handing
`pluck_str` a nested dict returns `None`, not a stringified payload. There is no
code path by which `message.content`, `payload.text` or `base_instructions`
reaches a variable, let alone the database.

`UsageEvent` is declared `slots=True` so no stray attribute can be attached to
smuggle content downstream.

*Enforced by:* `test_pluck_helpers_cannot_return_content`,
`test_content_keys_are_not_in_the_metadata_allowlist`, and a canary scan that
greps every byte of the resulting database, every `--json` output and the
captured log stream for strings planted in the fixtures. CI repeats the scan
against the artefacts built on a clean runner
(`.github/workflows/ci.yml`, job `security`).

### G2 — Credential files are never opened

Three independent layers, any one of which is sufficient:

1. **Narrow globs.** `~/.claude/projects/*/*.jsonl` and
   `~/.codex/sessions/*/*/*/rollout-*.jsonl` — never a recursive walk, which is
   what would reach `auth.json` and `*.key`.
2. **A deny-list** (`safety.is_credential_path`) checked immediately before
   every open: 12 filenames, 8 suffixes, and any path component under `.ssh`,
   `.gnupg`, `.aws`, `.kube`, `.docker`.
3. **Containment.** `safety.assert_within` fully resolves both the root and the
   candidate, so a file named `session.jsonl` that is really a symlink to
   `~/.codex/auth.json` is rejected — the deny-list alone would not catch it,
   because the name looks legitimate.

**Key-level, not just file-level.** Some tools keep a credential *beside* the
data. A VS Code-style `state.vscdb` holds an OAuth token, the account holder's
name and email, and application state in one key-value table — opening the file
is legitimate, reading one of its rows is not, and a filename deny-list cannot
tell the difference. This is not hypothetical for two shipped adapters:
`opencode.db` holds `account`, `control_account` and `credential` beside the
usage, so the OpenCode adapter names its columns and touches only `session` and
`message`; and Kimi Code keeps `api_key` for every configured provider in
`~/.kimi-code/config.toml`, which its glob is deliberately four levels deep to
avoid reaching. `safety.select_keys` is the control: an adapter states the
keys it needs and receives only those. Enumerating and filtering is explicitly
the wrong shape, because a key added upstream would then be read by default. An
adapter that asks for a credential-shaped key raises rather than being quietly
trimmed.

Additionally, `open_log_readonly` rejects anything that is not a regular file
*before* opening it. This is not only about devices: opening a FIFO blocks
indefinitely waiting for a writer, so a named pipe dropped into a scanned tree
would hang the scanner. That costs an attacker one `mkfifo`.

We never write to, rename or delete anything inside a provider directory.
Provider files are opened `O_RDONLY | O_NOFOLLOW | O_NONBLOCK`.

*Enforced by:* `test_credential_file_is_never_opened`,
`test_symlink_to_credentials_is_blocked`, `test_non_regular_file_refused`, and a
test fixture that plants real bait — `auth.json`, `oauth_creds.json`, a `.key`
file and a disguised symlink — in every adapter test run.

### G3 — No network by default

There is no telemetry, no analytics, no crash reporting and no remote logging.
Not opt-out — absent. Nothing here runs on a timer, at launch, or in the
background.

The codebase contains **exactly two** outbound requests, each only when you ask
for it:

| Trigger | Destination | Sends |
|---|---|---|
| `burn-o-meter pricing refresh` | `models.dev/api.json` | nothing — plain GET |
| menu bar → **Check for Updates…** | `api.github.com/…/releases/latest` | nothing — unauthenticated GET, no cookies |

Both are HTTPS with certificate verification on, no request body, no identifiers
and no cookies. A pricing snapshot ships with the package, so the first is
optional; the second only fires on a menu click.

**Why the update check is manual.** An automatic one would report your IP, your
version and your usage rhythm on a schedule you did not choose. That is telemetry
whatever it is called, so it does not exist.

**Upgrade Now** runs your own package manager — `brew upgrade`, `pipx upgrade` or
`uv tool upgrade`, chosen by resolving the recorded engine path to see who actually
installed it — and shows the command before running it. It upgrades only the
command line tool. The `.app` is unsigned, and self-replacement is precisely the
capability an unsigned binary should not have, so the result says the app is still
the old version and must be rebuilt from a checkout.

Run `burn-o-meter doctor --security` to print every egress point.

*Enforced by:* an **autouse** pytest fixture that raises on any socket connect,
so the entire suite runs offline and any accidental network call fails the
build; plus `test_no_telemetry_or_http_clients_imported`, which greps the
package for networking imports outside the single allowlisted module.

### G4 — Data is private on disk

- `~/.burn-o-meter/` is created `0700`; the database, its `-wal`/`-shm`
  sidecars and the config file are `0600`, set at creation so there is never a
  world-readable window.
- SQLite runs with `PRAGMA trusted_schema=OFF`.
- The menu bar app opens the database with a `mode=ro` URI — **read-only
  enforced by the driver**, so a UI bug cannot corrupt the store.
- All SQL is parameterised. There is one interpolation in the codebase (a
  `PRAGMA`, which SQLite cannot bind a parameter to); it takes an integer
  constant and is marked `sql-audited`.

*Enforced by:* `test_database_and_dir_permissions`,
`test_read_only_connection_rejects_writes`, `test_all_sql_is_parameterised`.

### G5 — Project paths are reducible

`cwd` is the only genuinely identifying string we store. It is worth storing —
per-project breakdowns are one of the most useful views — so it is made
controllable rather than dropped:

```toml
# ~/.burn-o-meter/config.toml
[privacy]
project_paths = "basename"   # "full" | "basename" (default) | "hash" | "none"
```

The default is `basename`, so `/Users/alice/clients/bigcorp/repo` is stored as
`repo` — the account name and the client never touch the database. `hash` gives
stable per-project grouping with no readable name, which is the right setting
before taking a screenshot or filing a bug.

Reduction happens **in the adapter, before the value reaches storage**, so
switching to `hash` leaves nothing recoverable rather than merely hidden.

*Enforced by:* `test_project_label_redaction_modes`,
`test_default_mode_drops_the_username`.

### G6 — Errors never carry content

A malformed line is reported as `path:line_number` and a length — never its
content. Line one of a transcript can be an API key.

`safety.redact()` describes a value without disclosing it
(`<redacted 31 chars>`). `safety.redact_path()` replaces your home directory
with `~`, so a pasted error does not leak a username. Adapters catch every
exception at their boundary and re-raise as `AdapterError(path, lineno, reason)`
with the original detached, so an unhandled traceback cannot print a fragment of
a transcript into a terminal or a GitHub issue.

*Enforced by:* `test_redact_never_discloses_content`,
`test_redact_path_hides_home`, `test_adapter_error_carries_location_not_content`.

### G7 — Minimal supply chain

One runtime dependency: `rich` (MIT, pure Python, no post-install hooks).
`orjson` is an optional extra. Adding a dependency requires justification in
review.

**No `curl | sh` install path will be published.** Piping a script straight from the
network into a shell asks you to execute code you have not seen, from a host that
could be compromised or impersonated, with your own privileges. `install.sh` exists
and does the whole setup in one command — but you clone the repository first, so the
script is on your disk and readable before it runs. That difference is the entire
point.

Releases are checksummed. **The macOS app is ad-hoc signed, not notarised** — that
needs a paid Apple Developer certificate, which this project does not have. It is
why the app is built from source rather than downloaded: macOS trusts a binary you
compiled yourself, and would put a downloaded one behind a Gatekeeper warning.

## Why there is no live quota lookup

Claude's utilisation figures come from a file the Claude desktop app writes to
local disk, sampled roughly every fifteen minutes. A live figure would require
calling claude.ai with the user's session credential, and **Anthropic's Consumer
Terms prohibit that**:

> Except when you are accessing our Services via an Anthropic API Key or where we
> otherwise explicitly permit it, to access the Services through automated or
> non-human means, whether through a bot, script, or otherwise.

Subscription credentials are called out specifically: OAuth tokens from Free, Pro
or Max accounts may not be used in any other product, tool or service. Anthropic
has enforced this against third-party tools, and the terms permit suspending an
account without notice.

The cost of getting this wrong falls on the user, not on us — their Claude
account, not our repository. So burn-o-meter does not do it, and will not accept
a contribution that does.

**Reading a file on the user's own disk is a different thing entirely.** It is
not access to Anthropic's Services: no credential, no network request, no
automation against a remote endpoint. That is why the passive source is fine and
the live one is not.

The consequence is a lag of up to about fifteen minutes on Claude's percentages.
Every reading therefore carries its own age, and the UI says so rather than
presenting an old number as current.

## Guardrails

Beyond the guarantees above, a few limits stop ordinary operation degrading into
a problem:

- **The database is bounded.** Quota readings are pruned after 90 days by
  default (`[retention]` in `config.toml`). Codex emits a reading per turn and
  Claude's desktop app writes one every 15 minutes, so they would otherwise grow
  without limit. Nothing pruned is unrecoverable — `scan --force` rebuilds
  everything from the provider logs.
- **One bad file fails that file, not the scan.** Every adapter error is caught
  per-file and only the exception *type* is kept, never its message.
- **Malformed input is skipped, never guessed.** An unreadable percentage is not
  0%; an absent price is not $0.00; a counter that goes backwards is a restart,
  not a negative delta.
- **The published cache-write multipliers are checked against the overlay.** The
  overlay exists because no public database records the 1-hour rate, which means
  nothing upstream would catch a typo in it. A test asserts every entry equals
  the model's input rate times the published multiplier.
- **Provider logs are opened read-only**, non-regular files are refused before
  opening (a FIFO would block the scanner indefinitely), and nothing is ever
  written into a provider's directory.
- **The background agent runs at low priority** (`Background` process type,
  `LowPriorityIO`, positive nice) and is scheduled rather than resident, so a
  wedged run is retried rather than left running.

## How these are enforced in CI

Every claim above is checked on each push and pull request
(`.github/workflows/ci.yml`):

| Job | What it proves |
|---|---|
| `engine` | Lint and the full suite on Python 3.11 / 3.12 / 3.13 |
| `security` | The G1–G7 suite; a canary scan of the database and JSON built on a clean runner; `pip-audit` on dependencies |
| `package` | The wheel installs into an empty environment and runs, and ships its pricing data |
| `app` | The macOS app builds and is a status-bar-only bundle |

The suite runs with an autouse fixture that raises on any outbound socket, so a
green `engine` job is itself evidence that the engine makes no network calls.
`.github/workflows/codeql.yml` runs CodeQL weekly and on every change.

## Verifying these claims yourself

```bash
burn-o-meter doctor --security     # guarantees, every egress point, file modes
pytest tests/test_security.py -v # the enforcing suite

stat -f '%Sp' ~/.burn-o-meter    # drwx------
stat -f '%Sp' ~/.burn-o-meter/*  # -rw-------

# Read the store yourself and look for anything resembling a conversation.
strings ~/.burn-o-meter/burn.db | less
strings ~/.burn-o-meter/burn.db | grep -c "$USER"   # expect 0

# Watch it run with no network at all.
pytest -q                        # sockets are blocked for the whole suite
```

If you find prompt text in any of those, that is a vulnerability — please report
it privately using the link at the top.
