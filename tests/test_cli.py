"""CLI smoke tests — exit codes and the absence of leakage in output."""

from __future__ import annotations

from pathlib import Path

import pytest

from burnometer.cli import main


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert "burn-o-meter" in capsys.readouterr().out


def test_doctor_runs_with_no_database(burn_home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert "Providers" in out
    assert "no database yet" in out


def flat(text: str) -> str:
    """Collapse rich's line wrapping so assertions do not depend on width.

    The doctor table wraps to the terminal, so a string that fits on one line at
    80 columns is split at 60 and joined differently at 120. Tests asserting on
    prose have to compare against the unwrapped text or they only pass at
    whatever width CI happens to use.
    """
    return " ".join(text.split())


#: Rich's box-drawing characters, stripped when checking that a value is present
#: somewhere in a table - a folded cell puts a border between the halves of a URL.
_BORDERS = str.maketrans("", "", "┏┓┗┛┡┩├┤┬┴┼─━│┃╇╈╪╡╞")


def disclosed(text: str) -> str:
    """Text with wrapping and table borders removed.

    ``https://models.dev/api.json`` renders as ``https://models.d`` on one row
    and ``ev/api.json`` on the next in a narrow pane, with a cell border between
    them. Asserting a value is *present* has to survive that.
    """
    return "".join(text.translate(_BORDERS).split())


def squeezed(text: str) -> str:
    """Remove *all* whitespace - for checking that something never appears.

    A path wrapped mid-token reads as ``/Users/fired`` + ``up``, which no
    substring check for the whole path would ever match. Absence has to be
    asserted against text that cannot hide a token in a line break.
    """
    return "".join(text.split())


def test_doctor_security_lists_every_guarantee(
    burn_home: Path, capsys: pytest.CaptureFixture[str], wide_console: None
) -> None:
    assert main(["doctor", "--security"]) == 0
    capsys_out = capsys.readouterr().out
    out = flat(capsys_out)
    for gid in ("G1", "G2", "G3", "G4", "G5", "G6", "G7"):
        assert gid in out, f"{gid} missing from security report"
    assert "https://models.dev/api.json" in disclosed(capsys_out), (
        "the single egress point must be disclosed in full, not elided to fit"
    )
    assert "No telemetry" in out


def test_doctor_output_does_not_leak_home_path(
    burn_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """G6: reports are safe to paste into a bug report."""
    main(["doctor"])
    out = capsys.readouterr().out
    # Squeezed, not raw: the table wraps, and a home path broken across two lines
    # would slip past a plain substring check while still being fully disclosed.
    assert squeezed(str(Path.home())) not in squeezed(out), (
        "absolute home path leaked into doctor output"
    )


def test_no_subcommand_exits_nonzero() -> None:
    with pytest.raises(SystemExit) as exc:
        main([])
    assert exc.value.code != 0


def _seed(burn_home: Path) -> None:
    """Scan the bundled fixtures into a temp store."""
    from burnometer.adapters.base import LogSource
    from burnometer.adapters.claude_code import ClaudeCodeAdapter
    from burnometer.scan import scan
    from burnometer.store import Store

    fixtures = Path(__file__).parent / "fixtures" / "claude_code"

    class Scoped(ClaudeCodeAdapter):
        def sources(self):
            return [LogSource(root=fixtures, glob="*.jsonl")]

    with Store.open(burn_home / "burn.db") as store:
        scan(store, adapters=[Scoped()])


@pytest.mark.parametrize(
    "argv",
    [
        ["today"],
        ["models"],
        ["daily"],
        ["projects"],
        ["sessions"],
        ["blocks"],
        ["models", "--since", "30d"],
        ["daily", "--since", "7d", "--limit", "3"],
    ],
)
def test_report_commands_run(burn_home: Path, argv: list[str]) -> None:
    _seed(burn_home)
    assert main(argv) == 0


def test_report_commands_without_a_database_exit_nonzero(burn_home: Path) -> None:
    """Better a clear 'run scan first' than an empty table implying zero usage."""
    assert main(["models"]) == 1


def test_cost_basis_is_always_explained(
    burn_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A dollar figure must never appear without saying what kind of dollars."""
    _seed(burn_home)
    capsys.readouterr()
    assert main(["models"]) == 0
    out = capsys.readouterr().out
    assert "subscription" in out or "API key" in out or "unpriced" in out


def test_blocks_output_disclaims_the_missing_denominator(
    burn_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The one place a user might expect a percentage — so the absence must be
    explained rather than silently omitted."""
    _seed(burn_home)
    capsys.readouterr()
    assert main(["blocks"]) == 0
    # flat(), not a newline replace: rich pads wrapped prose to justify it, so
    # "percent of limit" broken across lines rejoins with two spaces.
    out = flat(capsys.readouterr().out)
    assert "percent of limit" in out
    assert "invented" in out


def test_bad_since_value_is_reported_clearly(burn_home: Path) -> None:
    _seed(burn_home)
    with pytest.raises(ValueError, match="cannot read"):
        main(["models", "--since", "whenever"])


def test_readme_pins_the_current_version():
    """The README has gone stale on three separate releases.

    It names the version in prose and pins it inside install URLs that a reader will
    copy verbatim - a stale URL there does not merely read as out of date, it
    installs the wrong build.
    """
    import re
    from pathlib import Path

    from burnometer import __version__

    root = Path(__file__).resolve().parent.parent
    readme = (root / "README.md").read_text()

    status = re.search(r"Status: alpha \(v([0-9]+\.[0-9]+\.[0-9]+)\)", readme)
    assert status, "README no longer states a status version"
    assert status.group(1) == __version__, (
        f"README says v{status.group(1)}, package is {__version__}"
    )

    # Every doc, not just the README: install URLs moved to docs/install.md once,
    # which left this test passing against a file that no longer contained any.
    docs = [readme] + [f.read_text() for f in sorted(root.glob("docs/*.md"))]
    names = ["README.md"] + [f.name for f in sorted(root.glob("docs/*.md"))]
    for name, text in zip(names, docs, strict=True):
        pinned = set(re.findall(r"burn_o_meter-([0-9]+\.[0-9]+\.[0-9]+)\.tar\.gz", text))
        assert pinned <= {__version__}, (
            f"{name} pins {sorted(pinned - {__version__})} in an install URL, "
            f"package is {__version__}"
        )
    # And the guard must actually be guarding something.
    assert any("burn_o_meter-" in t for t in docs), "no install URL found to check"


def test_homebrew_formula_matches_the_package_version():
    """The formula shipped v0.2.0 while the package was 0.3.0.

    That is not merely untidy: the bottles workflow builds from the formula, so it
    produced 0.2.0 bottles and attached them to the v0.3.0 release. A stale formula
    silently ships the wrong software.
    """
    import re
    from pathlib import Path

    from burnometer import __version__

    formula = Path(__file__).resolve().parent.parent / "Formula" / "burn-o-meter.rb"
    if not formula.exists():  # pragma: no cover - formula is optional in a checkout
        return
    text = formula.read_text()

    # The source is the tag archive (.../archive/refs/tags/vX.Y.Z.tar.gz) rather than
    # a release asset, so the version lives in the tag, not a filename.
    url = re.search(r'^\s*url "([^"]+)"', text, re.MULTILINE)
    assert url, "formula has no source url"
    assert f"/v{__version__}.tar.gz" in url.group(1) or f"-{__version__}.tar.gz" in url.group(1), (
        f"formula url {url.group(1)!r} does not point at version {__version__}"
    )

    # A bottle block, when present, must be for the same version.
    root = re.search(r'root_url "([^"]+)"', text)
    if root:
        assert __version__ in root.group(1), (
            f"bottle root_url {root.group(1)!r} is not for version {__version__}"
        )


def test_install_and_uninstall_scripts_are_present_and_executable():
    """These are the first and last thing a user runs.

    A README that promises `./install.sh` and a repository that does not ship it,
    or ships it without the executable bit, fails at the worst possible moment.
    """
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    for name in ("install.sh", "uninstall.sh"):
        script = root / name
        assert script.exists(), f"{name} is missing"
        assert script.stat().st_mode & 0o111, f"{name} is not executable"
        text = script.read_text()
        assert text.startswith("#!"), f"{name} has no shebang"
        assert "set -euo pipefail" in text, f"{name} does not fail fast"


def test_readme_documents_the_scripts_it_promises():
    """Every script the README tells someone to run must actually exist."""
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    readme = (root / "README.md").read_text()

    for match in re.findall(r"(?:^|\s)\./([a-z0-9_-]+\.sh)", readme):
        assert (root / match).exists(), f"README references ./{match}, which does not exist"

    # The distinction that caused real confusion: package managers install the CLI
    # only, and the menu bar app is a separate build.
    assert "command line tool only" in readme, (
        "README must say plainly that brew/pipx/uv do not install the menu bar app"
    )


def test_docs_do_not_contradict_each_other_on_the_login_item():
    """The troubleshooting table once blamed a full menu bar for a missing icon.

    That is the rarer cause. The common one is that nothing starts the app, because
    macOS will not register a login item for an app outside /Applications — and a
    wrong first answer sends people to reorder their menu bar instead of fixing it.
    """
    from pathlib import Path

    faq = (Path(__file__).resolve().parent.parent / "docs" / "faq.md").read_text()
    assert "--enable-login-item" in faq, "troubleshooting must mention the login item fix"

    reboot = faq.index("missing after a reboot")
    full_bar = faq.index("the menu bar being full")
    assert reboot < full_bar, "the login-item cause must be listed before the full-menu-bar one"


def test_every_markdown_link_resolves():
    """Broken links are the quiet way documentation stops being trustworthy.

    Checks relative file links and heading anchors, in-file and across files, using
    GitHub's slug rules.
    """
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent

    def slug(heading: str) -> str:
        s = re.sub(r"[^\w\s-]", "", heading.strip().lower())
        return re.sub(r"\s+", "-", s).strip("-")

    def headings(text: str) -> set[str]:
        return {slug(m) for m in re.findall(r"^#{1,6}\s+(.+)$", text, re.M)}

    problems = []
    for md in list(root.glob("*.md")) + list((root / "docs").glob("*.md")):
        text = md.read_text()
        own = headings(text)

        for anchor in re.findall(r"\]\(#([a-z0-9_-]+)\)", text):
            if anchor not in own:
                problems.append(f"{md.name}: #{anchor} matches no heading in the same file")

        for path in re.findall(r"\]\(([A-Za-z0-9_./-]+\.md)\)", text):
            if not (md.parent / path).exists():
                problems.append(f"{md.name}: {path} does not exist")

        for path, anchor in re.findall(r"\]\(([A-Za-z0-9_./-]+\.md)#([a-z0-9_-]+)\)", text):
            target = md.parent / path
            if not target.exists():
                problems.append(f"{md.name}: {path} does not exist")
            elif anchor not in headings(target.read_text()):
                problems.append(f"{md.name}: {path}#{anchor} matches no heading there")

    assert not problems, "broken documentation links:\n  " + "\n  ".join(problems)


def test_changelog_has_an_entry_for_the_current_version():
    """A release with no changelog entry is a release nobody can read.

    Checked here rather than remembered, because the version is bumped in five
    places and the changelog is the one with no other test holding it accountable.
    """
    import re
    from pathlib import Path

    from burnometer import __version__

    changelog = (Path(__file__).resolve().parent.parent / "CHANGELOG.md").read_text()
    assert f"## [{__version__}]" in changelog, f"CHANGELOG.md has no '## [{__version__}]' section"

    # And the link reference at the bottom must resolve to the matching tag.
    link = re.search(rf"^\[{re.escape(__version__)}\]:\s*(\S+)", changelog, re.M)
    assert link, f"no link reference for {__version__}"
    assert f"/v{__version__}" in link.group(1), (
        f"the {__version__} link points at {link.group(1)!r}"
    )


def test_doctor_billing_advice_actually_works(
    burn_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Following doctor's own instruction must change what doctor reports.

    ``doctor`` prints ``set billing.<provider> = "api"`` for anything it priced
    as API-equivalent by assumption. It resolved the provider with
    ``getattr(cfg.billing, provider)``, which returns the default for every
    provider the dataclass does not name — so the advice was a no-op and the
    label never changed.

    Deliberately exercised with **opencode**, not claude_code: the broken lookup
    worked fine for the two providers that have their own dataclass field, and a
    test using one of those passes with the bug fully present.
    """
    from burnometer.adapters.base import LogSource
    from burnometer.adapters.opencode import OpenCodeAdapter
    from burnometer.scan import scan
    from burnometer.store import Store

    fixtures = Path(__file__).parent / "fixtures" / "opencode"

    class Scoped(OpenCodeAdapter):
        def sources(self):
            return [LogSource(root=fixtures, glob="opencode.db")]

    burn_home.mkdir(parents=True, exist_ok=True)
    with Store.open(burn_home / "burn.db") as store:
        scan(store, adapters=[Scoped()])

    # flat(), because rich wraps the table to the terminal width - CI runs at 80
    # columns and this assertion passed only on a wide terminal when written.
    def doctor_output() -> str:
        capsys.readouterr()
        assert main(["doctor"]) == 0
        return flat(capsys.readouterr().out)

    assert 'set billing.opencode = "api"' in doctor_output()

    (burn_home / "config.toml").write_text('[billing]\nopencode = "api"\n')
    out = doctor_output()
    assert "opencode: real spend (set in config)" in out, out
