## What and why

<!-- What changes, and what problem it solves. If a number changes, say which
     and by how much. -->

## Checklist

- [ ] `ruff check src tests` and `ruff format --check src tests` pass
- [ ] `pytest` passes
- [ ] New behaviour has a test

If this touches parsing, pricing, or anything that produces a figure:

- [ ] No number is invented — unknown stays `NULL`/`—`, never `0.0`
- [ ] Cost bases are not summed together
- [ ] Anything derived rather than reported is labelled `est`
- [ ] Fixtures include a malformed record and a duplicate

If this touches file reading or storage:

- [ ] No glob widened; no content field added to an allowlist in `safety.py`
- [ ] No new network call outside `pricing/catalog.py`
- [ ] Error messages carry `path:line`, never file content
- [ ] `pytest tests/test_security.py` passes

## Verification

<!-- What you ran, and what it showed. Reconciling against a second source
     (raw logs, a provider's own totals) is the strongest evidence. -->
