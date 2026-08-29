# Adding an agent

## Check what "output tokens" means before you trust it

Every source so far has disagreed about reasoning, and the difference is a silent
percentage on every bill:

| Source | Reasoning tokens | Verified against |
|---|---|---|
| Codex | **inside** `output_tokens` | 174/174 blocks with non-zero reasoning satisfy `input + output == total` |
| Claude Code | no reasoning field; `output_tokens_details.thinking_tokens` is a **breakdown within** output | 2,444/2,444 blocks, detail never exceeds output |
| OpenCode | **excluded** from `tokens.output`, but billed at the output rate | its own `cost` matched ours only after adding reasoning |
| Kimi Code | **no reasoning field at all** — `usage` carries only input, output and the two cache counts | 6/6 turns satisfy `inputOther + output == token_counting.tokens` |

`TokenCounts.reasoning` is display-only and is *not* added to `.total`. An adapter
whose source excludes reasoning must therefore fold it into `output` itself. Getting
this wrong is invisible: the totals still look plausible, and the error scales with
how much the model thinks.

The way to settle it is arithmetic on real data, not documentation — find blocks
where reasoning is non-zero and see which identity holds.


The adapter contract is deliberately thin, because breadth is the point. An
adapter declares where its data lives and turns records into `UsageEvent`s;
everything else — deduplication, pricing, cache-TTL accounting, storage,
analytics, the menu bar — it gets for free.

```python
class MyAgentAdapter:
    name, display_name, implemented = "myagent", "My Agent", True
    ENV_VAR, DEFAULT_ROOTS = "MYAGENT_HOME", (Path.home() / ".myagent",)

    def sources(self):                    # where the logs are
        return [LogSource(root=r / "sessions", glob="*.jsonl", env_var=self.ENV_VAR)
                for r in resolve_roots(self.ENV_VAR, self.DEFAULT_ROOTS)]

    def parse(self, path, root, offset=0, project_mode="basename"):
        ...                               # records -> UsageEvent, via the allowlist
```

Two things the framework handles that are easy to get wrong alone: agents
relocate their data with an environment variable and write to more than one
default location, so `resolve_roots` covers both — checking one path and
reporting zero for the other is worse than not supporting the tool at all. And
`safety.pluck_*` makes message content structurally unreachable, so a new
adapter cannot leak prompts even by accident.

Full guide, including the invariants that matter, in
[CONTRIBUTING.md](../CONTRIBUTING.md).

### Other models

Claude Code and Codex can both be pointed at another provider, so a transcript
may name a model neither vendor made. Rates ship for **287 models** including
DeepSeek, Kimi, GLM, MiniMax and Qwen, so that usage is priced rather than
landing in the unpriced bucket — no new adapter needed.

Two cases deliberately show no dollar figure at all:

- **Self-hosted inference** (Ollama, LM Studio, llama.cpp) has no per-token
  price. You paid for hardware and electricity, not tokens.
- **Plan-included models** publish a rate of zero because usage is covered by a
  subscription. Zero is not free.

Both are labelled `not_metered`, which is distinct from `unpriced`: one means
there is no rate, the other means we do not know it. Neither ever shows `$0.00`.
