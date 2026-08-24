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


def test_doctor_security_lists_every_guarantee(
    burn_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["doctor", "--security"]) == 0
    out = capsys.readouterr().out
    for gid in ("G1", "G2", "G3", "G4", "G5", "G6", "G7"):
        assert gid in out, f"{gid} missing from security report"
    assert "models.dev" in out, "the single egress point must be disclosed"
    assert "No telemetry" in out


def test_doctor_output_does_not_leak_home_path(
    burn_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """G6: reports are safe to paste into a bug report."""
    main(["doctor"])
    out = capsys.readouterr().out
    assert str(Path.home()) not in out, "absolute home path leaked into doctor output"


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
    out = capsys.readouterr().out.replace("\n", " ")
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
