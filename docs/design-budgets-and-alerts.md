# Design: budgets and alerts

Status: **decided and built** (v0.6.3) — the menu bar reading turns the
nearly-exhausted colour at 90%. Measured against real data on 2026-09-23.

The roadmap names three questions that must be settled before this is built,
because getting them wrong makes the feature worse than not having it. This records
the answers, and the measurements behind them, so they are not re-derived.

## 1. What a threshold means on a subscription

**Settled: not dollars.** On an API key a dollar figure is real spend. On a
subscription it is a counterfactual — what the tokens *would* have cost — and
"you have spent $20" said to someone who cannot spend money is the fabrication this
project refuses everywhere else.

So the unit follows the cost basis the engine already resolves per provider:

| Basis | Threshold is expressed as | Example |
|---|---|---|
| `api_billed` | dollars, over a calendar period | "today crosses $20" |
| `api_equivalent` (subscription) | the rate-limit window's own percentage | "the 5-hour window reaches 90%" |
| `unpriced`, `not_metered` | no budget | — there is no amount to cross |

The rate-limit percentage is the one number on a subscription that is both exact
(Anthropic's and OpenAI's own figures, not derived) and consequential: it is what
stops work.

## 2. Notification without a daemon that talks

**Settled: no notification. The menu bar reading changes colour instead.**

A notification needs a permission prompt, can interrupt, and — per the measurement
below — would arrive only about a quarter of an hour before the limit anyway. A
colour on the number already in the menu bar needs no permission, cannot interrupt,
and is exactly as timely. It also removes the question this section began with: the
background agent never needs a voice, and "never say it twice" needs no state,
because a colour is not an event that can repeat.

The colour is the popover's own for that level, taken from the same function
(`Theme.quotaState`) with the threshold in one constant, so the menu bar and the
popover can never disagree about the same reading. Readings are classified as
displayed — rounded — so 89.6%, shown as "90%", is coloured as 90%.

## 3. Not crying wolf — measured

This was the open question, and it was answered with 30 days of real Claude
plan-usage history from one heavy user rather than by argument: 1,240 samples, 23
completed five-hour windows, **5 of which ran out** (reached 100%).

How often each rule would have fired, and how much warning it gave before the
window ran out:

| Rule | Caught | False alarms | Missed | Warning before running out |
|---|---:|---:|---:|---|
| fixed 50% | 5 | 11 | 0 | — (fires on 16 of 23 windows) |
| fixed 80% | 5 | 5 | 0 | median 15 min |
| **fixed 90%** | **5** | **3** | **0** | **median 15 min** |
| projected exhaustion before reset, from 50% | 5 | 5 | 0 | median 15 min, min 0 |
| projected exhaustion before reset, from 60% | 5 | 5 | 0 | median 15 min, min 0 |

Two findings, the second more important than the first:

- **A fixed 90% threshold is the best rule on this data** — every run-out caught,
  fewest false alarms. The projection rule, which seemed cleverer ("will I hit the
  limit before the reset?"), did no better.
- **No rule can give more than about 15 minutes of warning, and that is a property
  of the source, not of the rule.** The Claude desktop app writes a sample roughly
  every 15 minutes, so usage routinely jumps 72% → 90% → 100% between readings. The
  rate between two readings is not observable, which is also why projection could
  not help. Any alert on this source is short notice.

The second finding shaped what was built. A warning that arrives a quarter of an
hour before the limit is useful — enough to finish a thought and commit — but it is
not the early warning the phrase "budget alert" suggests, so it is a colour rather
than a notification, and it should not be described as an alert.

Caveat: 23 windows and 5 positives is a small sample from one user. It is enough to
rule out the projection rule's supposed advantage, which the sampling cadence
explains mechanically; it is not enough to tune a threshold to the percent.

## What was built

- The rate-limit reading in the menu bar turns the nearly-exhausted colour at 90% —
  the same colour the popover's row already used at that level.
- **On by default.** There is nothing to opt into: a colour needs no permission and
  cannot interrupt. *Menu Bar Shows → Colour the Limit at 90%* turns it off for
  anyone who wants a monochrome menu bar.
- Only the percentage is coloured, never the spend beside it; everything else keeps
  the button's own tint, including its inversion while the item is held down.

Not built, and deliberately: API-key dollar budgets and the weekly window. Each adds
a way to be noisy, and should follow only once this one has been lived with.
