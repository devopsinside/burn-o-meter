"""OpenCode adapter, tested against a database produced by real runs.

The fixture holds three genuine sessions covering every cost regime OpenCode can
report: a free model, a ChatGPT subscription, and an API key. That spread is what
makes the `cost` finding testable at all.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from burnometer.adapters.opencode import READABLE_TABLES, OpenCodeAdapter
from burnometer.pricing import load_catalog
from burnometer.pricing.calculator import compute_cost

FIXTURE = Path(__file__).parent / "fixtures" / "opencode"
DB = FIXTURE / "opencode.db"


@pytest.fixture
def parsed():
    return OpenCodeAdapter().parse(DB, FIXTURE)


def test_reasoning_is_folded_into_output_because_opencode_excludes_it(parsed):
    """The finding that makes this adapter different from the other two.

    Codex counts reasoning inside output and Claude Code has no separate field, so
    the habit from both would understate OpenCode. Every event must carry output
    that already includes its reasoning.
    """
    assert parsed.events
    for e in parsed.events:
        assert e.tokens.reasoning > 0, "the fixture should exercise reasoning"
        assert e.tokens.output > e.tokens.reasoning, (
            f"{e.model}: output must include reasoning, not exclude it"
        )


def test_our_price_matches_the_figure_opencode_computed_for_itself(parsed):
    """The cross-check that proves the mapping, not just its self-consistency.

    OpenCode recorded $0.00457125 for this session. Pricing its raw tokens with our
    own catalog must land on the same number. Before reasoning was folded into
    output this came out 1.87% low, which is how the discrepancy was found.
    """
    billed = next(e for e in parsed.events if e.tokens.input == 5927)
    price = load_catalog().get("gpt-5.4-mini")
    assert price, "gpt-5.4-mini must be in the catalog for this check to mean anything"

    ours, _ = compute_cost(billed.tokens, price)
    assert ours == pytest.approx(0.00457125, rel=1e-9), (
        "our price must equal what OpenCode computed for the same tokens"
    )


def test_messages_reconcile_with_the_sessions_own_totals(parsed):
    """A free integrity check, like Codex's running total.

    Summed message rows equal the session row, so a format change surfaces as a
    failed reconciliation instead of a quietly wrong figure.
    """
    assert parsed.integrity_checks > 0, "the check must actually run"
    assert parsed.integrity_failures == 0


def test_token_identity_holds(parsed):
    """input + output + reasoning + cache == total, verified 4/4 on real data.

    `input + output == total` held 0/4, which is how we know input excludes cache
    reads here rather than including them.
    """
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        rows = [json.loads(d) for (d,) in conn.execute("SELECT data FROM message")]
    finally:
        conn.close()

    checked = 0
    for d in rows:
        if d.get("role") != "assistant":
            continue
        t = d["tokens"]
        c = t.get("cache") or {}
        parts = (
            t["input"] + t["output"] + t["reasoning"] + (c.get("read") or 0) + (c.get("write") or 0)
        )
        assert parts == t["total"], "OpenCode's own identity must hold"
        assert t["input"] + t["output"] != t["total"] or t["reasoning"] == 0
        checked += 1
    assert checked == 4


def test_zero_cost_is_never_read_as_money_spent(parsed):
    """`cost = 0` is ambiguous and must not reach the ledger.

    The fixture contains a free model at 0.0 and a subscription session at 0.0 that
    genuinely spent tokens. Reading either as "$0.00 spent" is the failure this
    project exists to avoid, so the adapter emits tokens only and lets the pricing
    layer decide the basis.
    """
    for e in parsed.events:
        assert not hasattr(e, "cost_usd") or e.cost_usd is None, (
            "the adapter must not carry OpenCode's cost through as a billed figure"
        )
    # And the tokens are real even where cost was zero.
    free = next(e for e in parsed.events if "hy3-free" in e.model)
    assert free.tokens.total > 0


def test_model_slug_is_bare_so_the_catalog_can_price_it(parsed):
    """`Catalog.get` is exact-match by design and refuses to guess a rate.

    Prefixing the slug with the upstream provider reads better and prices nothing:
    the catalog holds `gpt-5.4-mini`, not `openai/gpt-5.4-mini`. This was caught by
    a real scan reporting every OpenCode model as unpriced.
    """
    from burnometer.pricing import load_catalog

    catalog = load_catalog()
    slugs = {e.model for e in parsed.events}
    assert "gpt-5.4-mini" in slugs, "the slug must be the bare model id"
    assert not any("/" in s for s in slugs), "no provider prefix"
    assert catalog.get("gpt-5.4-mini"), "and that slug must actually price"


def test_events_carry_a_datetime_not_a_string(parsed):
    """The store formats the timestamp, so it must receive a datetime.

    A string passes every check in the adapter and fails deep inside the writer,
    which is where a real scan first reported it as an unexplained AttributeError.
    """
    from datetime import datetime

    for e in parsed.events:
        assert isinstance(e.ts, datetime), f"ts must be datetime, got {type(e.ts).__name__}"
        assert e.ts.tzinfo is not None, "and it must be timezone-aware"


def test_adapter_never_touches_credential_or_content_tables():
    """Credentials live in the same database: account, control_account, credential.

    `part` holds conversation text. A filename deny-list cannot help when the secret
    is a column in the next table, so the source is checked for the only two table
    names it is allowed to name.
    """
    import re

    source = Path(__file__).parent.parent / "src" / "burnometer" / "adapters" / "opencode.py"
    text = source.read_text()

    # Only the tables actually named in a FROM clause matter; the module docstring
    # discusses the forbidden ones by name on purpose, to explain why they are.
    # Case-sensitive on purpose: the SQL is written in upper case, so this matches
    # queries without also matching Python's `from x import y` or the word in prose.
    queried = {m for m in re.findall(r"\bFROM\s+(\w+)", text)}
    assert queried <= {"session", "message"}, (
        f"adapter queries tables it must not touch: {sorted(queried - {'session', 'message'})}"
    )
    # Precise: a real wildcard query, not the phrase used in a comment explaining
    # why there isn't one.
    assert not re.search(r"SELECT\s+\*\s+FROM", text), "columns must be named"

    # The module declares which tables it may read; that declaration must match
    # what it actually queries, or it is decoration.
    assert queried == READABLE_TABLES, (
        f"declared {sorted(READABLE_TABLES)} but queries {sorted(queried)}"
    )


def test_the_fixture_is_actually_committed():
    """A fixture that exists only on the machine that made it is not a fixture.

    `.gitignore` carries `*.db` to keep the user's own database out of the repo,
    and it swallowed this file too — so these tests passed locally and failed on
    CI, which is the worst place to learn it. Checked here rather than left to CI,
    because the split can survive days before anyone notices.
    """
    import subprocess

    tracked = subprocess.run(
        ["git", "ls-files", "--error-unmatch", str(DB)],
        capture_output=True,
        cwd=DB.parent,
    )
    assert tracked.returncode == 0, (
        f"{DB.name} is not tracked by git — CI will not have it. "
        "Check .gitignore does not swallow test fixtures."
    )
