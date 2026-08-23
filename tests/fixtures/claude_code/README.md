# Claude Code fixtures

Hand-written to the field shapes observed in real `~/.claude/projects/*/*.jsonl`
transcripts. Values are synthetic and the numbers are round, so tests can assert
exact totals rather than approximations.

Every content-bearing field carries a `CANARY-` string. `test_security.py` greps
the resulting database, JSON output and logs for `CANARY-`; a hit means the
content firewall (G1) has regressed.

Canary values are deliberately **not** key-shaped. An earlier revision used a
realistic `sk-ant-…` string; that trips GitHub secret scanning and makes a
reviewer stop to check whether a real key leaked. A fake key that looks real is
its own small hazard.
