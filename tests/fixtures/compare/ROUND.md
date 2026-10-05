# Synthetic round: hand calculations

All ledger, outcome and log data in this directory was authored for testing.
No real session, response, repository, host or developer path is included.
The `Z:/synthetic/...` directories in logs are deliberately nonexistent fixture
paths. `tests/test_compare.py` copies this round into a temporary directory and
applies the explicitly described variants below. Expected numbers are literal
test constants, never computed by the production functions to create an oracle.

## Base round

Registration precedes application at 2030-01-08 00:00 UTC. The two seven-day
dispatch windows are Jan 1-8 and Jan 8-15, with seven follow-up days. Outcomes
are observed through Feb 10. There are 20 before deliverables, B01-B20, and 20
after deliverables, A01-A20. In each arm 01-16 succeed and 17-20 are abandoned.
One synthetic Codex session links to each deliverable by its ID. All sessions
have model `synthetic-model`, effort `high`, CLI `1.0`; tasks are `feature`.
All merged PRs have historical green checks. No confounders or excluded rows.

Each before row spends 100 tokens and takes 100 elapsed seconds. Each after
row spends 40 tokens and takes 40 seconds, **including abandoned rows**.
Codex input includes cache reads, so new input = input - cache reads.

| Quantity | Before numerator / denominator | After numerator / denominator |
| --- | --- | --- |
| Success rate | 16 / 20 = 0.8 | 16 / 20 = 0.8 |
| New input per success | (20 * (80-20)) / 16 = 75 | (20 * (32-8)) / 16 = 30 |
| Cache reads per success | (20 * 20) / 16 = 25 | (20 * 8) / 16 = 10 |
| Output per success | (20 * 20) / 16 = 25 | (20 * 8) / 16 = 10 |
| Reasoning subset per success | (20 * 5) / 16 = 6.25 | (20 * 2) / 16 = 2.5 |
| Total tokens per success | 2000 / 16 = 125 | 800 / 16 = 50 |
| Elapsed seconds per success | 2000 / 16 = 125 | 800 / 16 = 50 |

Cache writes are **not reported**, not zero. Reasoning is already in output and
is not added to the total. Failure spend is 400 before and 160 after; dropping
failures would wrongly produce totals of 1600 and 640.

Wilson uses z = 1.959963984540054. For 16/20, center is
`(0.8 + z^2/40)/(1 + z^2/20)` and radius is
`z*sqrt(0.8*0.2/20 + z^2/1600)/(1+z^2/20)`.
Both arms' 95% limits are **[0.5839825677481064, 0.919342337420202]**.
The after-minus-before point estimate is zero. Newcombe's hybrid score radius
here is `sqrt((0.8-0.5839825677481064)^2 +
(0.919342337420202-0.8)^2) = 0.24679166221252044`.
Its 95% interval is **[-0.24679166221252044, 0.24679166221252044]**.

The registered margin is intentionally generous **50 percentage points** to
exercise decisions on a small synthetic round. It is not a recommended margin.
`ceil(2*0.8*0.2*(1.959963984540054+0.8416212335729143)^2/0.5^2)`
is **11** needed per arm. Registered size is **20**, actual is **20 / 20**.
With a 5-point margin, the diagnostic needed size is **1005** per arm instead.

## Deterministic percentile calculation

Use `random.Random(1729)`, 1000 resamples; each replicate draws 20 before
indices using `randrange(20)`, then 20 after indices. IDs sort 01-20, and a
drawn index below 16 is successful. Let bootstrap success counts be `Sb, Sa`.
Because per-row spend is constant, the total-token and time ratios reduce to
`(800/Sa)/(2000/Sb) = 0.4*Sb/Sa`. The actual point estimate is **0.4**.
All 1000 draws have at least one success in each arm.

At `(1000-1)*.025 = 24.975` and `(1000-1)*.975 = 974.025`, the neighbouring
ordered bootstrap ratios (zero-based positions) are:

| Rank | Sb | Sa | 0.4*Sb/Sa |
| --- | --- | --- | --- |
| 24 | 10 | 14 | 0.2857142857142857 |
| 25 | 13 | 18 | 0.2888888888888889 |
| 974 | 18 | 13 | 0.5538461538461539 |
| 975 | 17 | 12 | 0.5666666666666668 |

Interpolating gives **[0.28880952380952385, 0.5541666666666664]**.
Every reported token kind and time has the same ratio and limits, up to floating
roundoff. Both classify improved: point <= .90 and upper < 1. Arm cost-per-success
limits are **[105.26315789473684, 166.66666666666666]** before and
**[42.10526315789474, 66.66666666666667]** after, for total tokens and seconds.
These are resampling intervals, not an assertion that an observational round
randomized treatment or removed unregistered confounding.

## Required round-table cases

Each variant starts from the base round; variants are independent. The tests
assert the named proposal/reason and the numeric components where they change.

| Case | Explicit change | Hand result | Expected proposal |
| --- | --- | --- | --- |
| adopt | None | Non-inferior at margin .5; both ratios .4 with upper .554167 | adopt |
| reject (inferior) | All 20 after rows abandoned at 40 seconds; remove their ledger PR links; margin 10 points | Success 0/20 minus 16/20 = -.8; CI [-.919342337420202, -.5305100232026292]; upper < -.1 | reject |
| owner decides | All after merges/abandonments at 200 seconds; spend stays 40 each | Time 4000/16 = 250; ratio 2, CI [1.444047619047619, 2.770833333333332]; tokens remain improved | owner_decides |
| coverage | Set repository `commits_complete` false | Cannot establish outside follow-up coverage | withhold: incomplete_coverage |
| not preregistered | Register Jan 8 at 00:00:01 UTC | Registered after application and after-window start | withhold: not_preregistered |
| blocking confounder | Change all after models to `synthetic-next` | Dominant value changes; TV distance 1 | withhold: not_comparable, model_mix_shift |
| immature | Move A01 merge and its green check to Feb 9; observation still Feb 10 | Its seven-day maturity window ends Feb 16; after has 1 immature, 15 successes | withhold: immature_or_in_progress |
| insufficient sample | Register 21 per arm | Actual 20/20, below registered 21 | withhold: insufficient_sample |
| inconclusive | Margin 5 points | Equal rates' lower -.246792 <= -.05 and upper > -.05; diagnostic n 1005 | withhold: inconclusive_success |

For inferior success, after Wilson limits for 0/20 are approximately
`[0, 0.1611251580528194]`. The upper difference bound is
`-.8 + sqrt((0.1611251580528194-0)^2 +
(.8-.5839825677481064)^2) = -0.5305100232026292`.
After cost per success is undefined (800/0); the success rejection comes before
the cost gate and does not fabricate a finite cost or a ratio.

The opposite-cost variant multiplies the base **time** point and percentile
limits by five: `2 = 5*.4`, while tokens stay .4. This is a tradeoff, not adoption.

## Published interval checks and additional boundaries

[Newcombe (1998), Table II, method 10](https://doi.org/10.1002/(SICI)1097-0258(19980430)17:8%3C873::AID-SIM779%3E3.0.CO;2-I)
reports 56/70 minus 48/80 with limits **[.0524, .3339]**. The tests assert that
literal pair and all seven other contrasts in the table, at published precision.
Wilson 1/2 reproduces the existing **[.094531205734, .905468794266]** example.
The methods and assumptions are cited in [MEASUREMENT](../../../docs/MEASUREMENT.md#statistical-methods).

Other tests exercise each gate's precedence, exact decision thresholds, late
registration before a delayed after window, unequal windows, UTC validation,
duplicate JSON keys, mixed and contradicting sessions, unlinked counts at the
10% boundary, same/mixed checks bases, effort/CLI shifts, missing metadata,
runner event window ends, descriptive task mixes, unattributed 1000/3800 spend,
unreported bootstrap draws, output source protection, privacy canaries,
determinism, the optional ledger header and offline CLI JSON/text output.
