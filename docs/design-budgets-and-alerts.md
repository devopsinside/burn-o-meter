# Design: budgets and alerts

Status: **proposed**, measured against real data on 2026-09-23. Not built.

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

**Settled: the menu bar app speaks; the background agent stays silent.**

The agent exists to scan and is silent by design, and giving it a voice would
mean building everything the app already has. The app already scans every minute,
already holds the snapshot, and is the thing the user has chosen to keep running.
It posts through `UNUserNotificationCenter`, which puts the permission prompt,
Focus modes and Do Not Disturb in the user's hands rather than ours.

"Never say it twice" needs one small piece of state: for each window, the
thresholds already announced. A window is identified by its reset time, which the
engine already records, so a new window starts with a clean slate by construction
and no clearing logic is needed.

## 3. Not crying wolf — measured

This was the open question, and it was answered with 30 days of this author's own
Claude plan-usage history rather than by argument: 1,240 samples, 23 completed
five-hour windows, **5 of which ran out** (reached 100%).

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

The second finding is why this is still a proposal. A warning that arrives a
quarter of an hour before the limit is useful — enough to finish a thought and
commit — but it is not the early warning the phrase "budget alert" suggests, and
the feature should not be described as one.

Caveat: 23 windows and 5 positives is a small sample from one user. It is enough to
rule out the projection rule's supposed advantage, which the sampling cadence
explains mechanically; it is not enough to tune a threshold to the percent.

## What a first version would be

- One alert: the 5-hour window reaching 90%, on a subscription. Opt-in.
- Stated honestly in the UI as short notice, with the reason.
- The weekly window and API-key dollar budgets follow once the first has been lived
  with, because each adds a way to be noisy.

## Open — the author's call, not an engineering one

1. **Opt-in or on by default?** On by default reaches the people it would help, but
   a notification permission prompt at first launch, for a feature they did not ask
   for, is a poor first impression.
2. **Is a ~15-minute warning worth a notification at all,** or would the same fact
   be better as a change in the menu bar itself — the percentage turning to the
   warning colour at 90%, which costs no permission and cannot interrupt?
