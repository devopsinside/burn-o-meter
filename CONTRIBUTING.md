# Contributing

Thanks for looking. This project has one unusual property that shapes everything
below: **it reads people's conversation transcripts.** That makes some ordinary
conveniences unacceptable here, so please read the two short sections marked
**non-negotiable** before opening a PR.

## Getting set up

```bash
git clone https://github.com/devopsinside/burn-o-meter
cd burn-o-meter
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest                 # the full suite, under a second
```

The macOS app is a separate Swift package with no third-party dependencies:

```bash
swift build --package-path macos/burn-o-meter -c release
macos/make-app.sh                # produces macos/build/burn-o-meter.app
```

`macos/build/burn-o-meter.app/Contents/MacOS/burn-o-meter --dump` prints what the UI
would show, as JSON, without launching a window. It must agree with
`burn-o-meter today --json`; if it does not, the menu bar is quietly showing
different numbers from the CLI.

`scripts/smoke-test.sh` is the end-to-end check: it installs into a throwaway
environment, scans your real logs, verifies idempotence and file permissions,
and removes everything. Run it before opening a PR that touches parsing,
storage or the CLI — the unit tests use fixtures, and this is the only thing
that exercises the whole path against real data.

`./install.sh` sets up a working install end to end if you want to run what you are
changing; `./uninstall.sh --purge` puts the machine back. Both are shellchecked in
CI alongside the Python.

Releases have a forced order — GitHub releases are immutable, so assets attach only
at creation and a consumed tag can never be reused. [RELEASING.md](RELEASING.md)
records the sequence and why each step is where it is.

## Non-negotiable: never invent a number

The entire point of this tool is that its figures are defensible. Every one of
these rules exists because breaking it produces a plausible number that is wrong,
which is worse than no number:

- **Unknown price → `NULL`, never `0.0`.** "Free" and "we don't know" are
  different facts. Unpriced models keep their token counts and render `—`.
- **Never sum across cost bases.** A subscription figure is what tokens *would*
  have cost; adding it to a real API charge produces a meaningless total. There is
  deliberately no combined total anywhere, including in `--json`.
- **Absence is not zero.** An empty quota block, an unreadable field, a missing
  sample — none of these are "0%". Skip them.
- **No estimate without a defensible basis.** We show no "percent of limit" for
  Claude's five-hour window derived from usage, because Anthropic publishes no
  token limit to divide by. Comparing against the user's own history is a claim we
  can support; a percentage would be invention.
- **Label what you cannot verify.** `exact` means the provider reported it; `est`
  means we derived it. Readings carry their age.

If a change makes a number easier to display but harder to defend, it will not be
merged.

## Non-negotiable: the security guarantees

`SECURITY.md` documents seven guarantees (G1–G7). They are enforced by tests, not
by convention, and CI runs those tests on every pull request. In particular:

- **Never read message content.** Extraction goes through the allowlists in
  `safety.py` using the `pluck_*` helpers, which return scalars only. Do not copy
  a parsed dict. Do not add a content field to an allowlist.
- **Never widen a glob.** `~/.codex/auth.json` and `~/.claude/sessions/*.key` sit
  inside directories we scan. The globs name exactly what they need.
- **No network calls** outside `pricing/catalog.py`. The test suite blocks sockets
  and a test greps the package for networking imports.
- **No live/authenticated quota lookup, ever.** Anthropic's Consumer Terms
  prohibit automated access to the Services without an API key and prohibit using
  subscription OAuth tokens in other tools. A PR adding this will be declined —
  the cost lands on users' own accounts.
- **Errors carry `path:line`, never content.** Line one of a transcript can be a
  secret.

Test fixtures contain planted `CANARY-` strings; CI greps the produced database,
JSON output and logs for them. If your change makes a canary appear, the content
firewall has regressed.

## Adding a provider

One module plus a registry entry. Nothing else changes.

1. Create `src/burnometer/adapters/<provider>.py` implementing the `Adapter`
   protocol from `adapters/base.py`.
2. Register it, and add it to the import in `base.get_adapters()`.
3. Normalise tokens to the invariants on `TokenCounts` — uncached input only,
   reasoning as a display-only subset of output, cache writes split by TTL.
   Providers disagree about all three, and getting it wrong is the single most
   common source of wrong totals.
4. Add hand-built fixtures under `tests/fixtures/<provider>/` with `CANARY-`
   strings in every content-bearing field, including at least one malformed
   record and one duplicate.
5. Set `implemented = True` only when `parse()` actually works. Until then
   `doctor` reports the provider as detected but not yet readable, which is
   honest; claiming support you do not have is not.

If the format offers any self-check — Codex's running total lets us verify our
own deltas — implement it and surface the result. It turns a silent misreading
into a visible failure.

## Pricing changes

`pricing/snapshot.json` is generated, not hand-edited — regenerate it with
`burn-o-meter pricing refresh`. Corrections go in `pricing/overlay.toml`, and every
entry needs a `source` URL and a `verified` date, because `doctor --pricing`
shows them and a stale correction should be visible rather than silently trusted.

## Naming

The project is **burn-o-meter**. Use that everywhere — prose, headings, commit
messages, and commands in documentation. `burnometer` is installed as a second
console script so nobody is forced to type hyphens, but it is not the documented
form.

The single exception is the Python module, `burnometer`, because Python does not
permit hyphens in module names. It is internal and only contributors see it.

Do not add a `burn` alias. It is a generic word likely to shadow something on a
user's PATH, and taking a common command name is not ours to do.

## Style

`ruff check` and `ruff format` are enforced in CI. Beyond that: explain *why* in
comments, not *what*. Most of the non-obvious code here exists because of a real
measurement — say which one.

## Keeping the roadmap honest

[ROADMAP.md](ROADMAP.md) is updated when things ship, not written once. A stale
line there is a bug — it is the file people read to decide whether to wait for
something or build it themselves.

When you land an agent adapter: move it from "Next" to "Shipped", and add what
you learned about its format to the entry for the next one. When you *rule
something out*, put it in "Not building" with the reason and a link. That
section exists so a question does not have to be re-answered, and so the
judgement is visible rather than implied.

## Repository settings

Some settings cannot live in a file. `scripts/setup-repo.sh` applies them and is
safe to re-run:

```bash
gh auth login          # once
scripts/setup-repo.sh
```

Branch protection, private vulnerability reporting and CodeQL all require the
repository to be public (or a paid plan). The script applies what the current
plan allows and reports the rest rather than failing, so running it again after
going public finishes the job.

## Reporting a vulnerability

Use GitHub's private vulnerability reporting (Security → Report a vulnerability).
Please do not open a public issue. See `SECURITY.md`.

## A note on the name

The project is **burn-o-meter** everywhere a user can see it: the repository, the
commands, the app bundle, `~/.burn-o-meter`, and the launchd label. The one place
the hyphen cannot appear is the Python module (`import burnometer`) — Python does
not allow hyphens in module names. Only contributors ever see that form.
