# Installing burn-o-meter

**Most people want one command**, which does the CLI, the app, the login item and
background scanning together:

```bash
git clone https://github.com/devopsinside/burn-o-meter
cd burn-o-meter && ./install.sh
```

Safe to re-run. `./uninstall.sh` reverses all of it (`--purge` also deletes your
data). `./install.sh --help` lists the flags: `--cli-only`, `--no-login`,
`--no-agent`.

This page covers the cases that need more than that.

Requires **Python 3.11+**. macOS 14+ for the menu bar app.

> **`brew`, `pipx` and `uv` install the command line tool only.** No app is created
> and nothing appears in your menu bar until you build one — see
> [the macOS menu bar app](#the-macos-menu-bar-app) below. `./install.sh` does both.

### The CLI

Not on PyPI yet. Any of these puts `burn-o-meter` on your PATH, so it works from
any directory.

**Homebrew:**

```bash
brew tap devopsinside/burn-o-meter https://github.com/devopsinside/burn-o-meter
brew install devopsinside/burn-o-meter/burn-o-meter
```

Since Homebrew 6.0, a tap outside `homebrew/core` is untrusted until you say
otherwise, and `brew tap` alone does not grant that — installing by the full
`user/tap/formula` name trusts this one formula and nothing else. If Homebrew
still asks, `brew trust --formula devopsinside/burn-o-meter/burn-o-meter`
grants exactly that much. Prefer it over `brew trust devopsinside/burn-o-meter`,
which would trust everything this tap ever ships.

Homebrew builds the formula from source, so it needs Apple's Command Line Tools
(`xcode-select --install`). If you would rather not install those, use pipx or uv
below — nothing here needs a compiler; the requirement is Homebrew's, not
burn-o-meter's.

**pipx or uv**, straight from the repository:

```bash
pipx install git+https://github.com/devopsinside/burn-o-meter
# or
uv tool install git+https://github.com/devopsinside/burn-o-meter
```

**No developer tools on the machine?** A `git+https://` URL needs git, and on a
fresh Mac git arrives with Apple's Command Line Tools — so all three commands
above want a toolchain burn-o-meter itself never uses. Installing the release
archive directly needs none of it:

```bash
pipx install https://github.com/devopsinside/burn-o-meter/releases/download/v0.3.4/burn_o_meter-0.3.4.tar.gz
# or
uv tool install https://github.com/devopsinside/burn-o-meter/releases/download/v0.3.4/burn_o_meter-0.3.4.tar.gz
```

That URL pins a version; check [releases](https://github.com/devopsinside/burn-o-meter/releases)
for the newest.

Then, from anywhere:

```bash
burn-o-meter scan      # read your logs (first run takes ~150ms)
burn-o-meter today     # what today cost, and where your limits stand
```

<details>
<summary>Installing from a clone instead (for hacking on it)</summary>

```bash
git clone https://github.com/devopsinside/burn-o-meter
cd burn-o-meter
python3 -m venv .venv && .venv/bin/pip install -e .
.venv/bin/burn-o-meter --version
```

A virtualenv only adds its commands to your PATH while it is **activated**, so
after this, a bare `burn-o-meter` in a new terminal gives you
`command not found`. Pick one:

```bash
source .venv/bin/activate     # for this shell only
pipx install --editable .     # on your PATH everywhere, still tracks your edits
```

</details>

### Try it without installing anything

One command installs it in a throwaway environment, runs it against your real
logs, checks the results, and removes every trace:

```bash
git clone https://github.com/devopsinside/burn-o-meter
cd burn-o-meter && scripts/smoke-test.sh
```

It is safe to run even if you already use burn-o-meter — it works in its own
temporary data directory and never touches your database, config or background
agent. Add `--from-git` to install from the public repository instead of the
checkout, which is what a first-time user gets.

Alongside "does it run", it checks the things worth checking: that a second scan
adds nothing, that the database is owner-only, and that nothing it wrote contains
a credential-shaped string or a path from your home directory.

### The macOS menu bar app

No prebuilt release yet — build it in one command:

```bash
macos/make-app.sh --install          # builds, then installs to /Applications
open /Applications/burn-o-meter.app
```

`--install` matters more than it looks: macOS refuses to register a login item for
a bundle outside `/Applications`, so an app left in the build directory can never
start itself. It seems fine until the first reboot, at which point the icon is
simply gone and Spotlight is the only way to get it back. Leave the flag off only
if you are testing a build you do not intend to keep.

**Building it yourself is the smooth path.** macOS trusts an app you compiled on
your own machine. It does not trust an app downloaded from the internet unless
the developer has paid Apple for a certificate and had the app checked by them —
burn-o-meter has not, so a downloaded copy gets blocked on first open and you
have to allow it under **System Settings → Privacy & Security**. The one-line
build above skips all of that.

Look for `🔥 23% · ~$60` near your clock. If you cannot see it, your menu bar is
probably full — macOS silently hides status items when there is no room, which is
common on MacBooks with a notch.

**Left-click** opens the popover. **Right-click** (or the gear button inside)
opens options: launch at login, background-scanning status, scan now, reveal the
data folder, quit.

To have it start with your Mac:

```bash
macos/make-app.sh --install
open /Applications/burn-o-meter.app     # then gear menu → Launch at Login
```

Or without opening the app at all:

```bash
/Applications/burn-o-meter.app/Contents/MacOS/burn-o-meter --enable-login-item
```

**The app must be in `/Applications` for this.** macOS will not register a login
item for a bundle running from a build directory — it reports the app as not
found, and clicking the toggle cannot fix it. The menu says so rather than
failing quietly.

Registration goes through `SMAppService`, so macOS owns the login item: you can
revoke it in **System Settings → General → Login Items**, and the app cannot
re-enable it behind your back.

### Keep it up to date automatically

```bash
burn-o-meter agent install          # scans every 60s, low priority
burn-o-meter agent status
```

This installs a launchd agent at
`~/Library/LaunchAgents/com.burn-o-meter.scan.plist`. It is not a resident
daemon — launchd re-runs a short scan on an interval, so there is no process to
leak memory and a failed run simply retries. The app also scans when you open the
popover, so what you see is current.

## Uninstall

`./uninstall.sh` does all of this in one step and keeps your data unless you pass
`--purge`. The manual sequence, for reference:


Complete removal, in order:

```bash
# 1. stop and remove background scanning
burn-o-meter agent uninstall
brew services stop burn-o-meter    # only if you started it this way instead

# 2. quit the menu bar app (gear menu → Quit, or its power button)
#    Turn OFF "Launch at Login" first, or use System Settings → Login Items.
pkill -f burn-o-meter.app
# turn off Launch at Login first, so macOS is not left with a dangling login item
/Applications/burn-o-meter.app/Contents/MacOS/burn-o-meter --disable-login-item 2>/dev/null
rm -rf macos/build/burn-o-meter.app /Applications/burn-o-meter.app

# 3. delete all stored data — the database, UI payload, config
rm -rf ~/.burn-o-meter

# 4. remove the CLI, whichever way you installed it
brew uninstall burn-o-meter && brew untap devopsinside/burn-o-meter
pipx uninstall burn-o-meter        # or: uv tool uninstall burn-o-meter
```

That is everything. burn-o-meter writes to exactly two places —
`~/.burn-o-meter/` and the launchd plist — and touches nothing else. It never
writes to your agents' directories, so removing it leaves your Claude Code and
Codex logs untouched.

To verify nothing remains:

```bash
ls ~/.burn-o-meter 2>/dev/null            # should not exist
ls ~/Library/LaunchAgents | grep burn      # should print nothing
# and check System Settings → General → Login Items for a leftover entry
```
