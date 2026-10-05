# Authored M1d fixtures

All ledgers, API response objects and logs here are synthetic. No network service
or actual session was used. Regeneration uses `write_fixture` in
`tests/test_deliver.py`; the shipped-file test also runs the checked-in fixtures.

```sh
python -m sumbi deliver --ledger tests/fixtures/deliver/deliverables.csv \
  --outcomes tests/fixtures/deliver/outcomes --home tests/fixtures/deliver/home \
  --since 2030-01-01T00:00Z --until 2030-01-10T00:00Z \
  --project sample --match-path '/synthetic/sample*' --json out/deliver-example.json
```

Six deliverables dispatch on January 1. The observation ends exclusively February
1; required checks are recorded at merge on each exact head. D1 merges January 2.
Dfail is abandoned January 2 without a PR. Drev merges January 2 and is reverted
January 3. Dimm merges January 28, so its 7-day window is open. Dsplit's two PRs
both merge January 2. Dfollow merges January 2, receives a fixing PR created
January 4 and merged January 5; the fix's own window has closed.

Hand truth: success 3, failed 2, in_progress 0, immature 1. First-pass successes
are D1 and Dsplit (2/6); eventual successes add Dfollow (3/6). The Wilson 95%
interval for 3/6 is [0.18761630648, 0.81238369352]. For 2/6 it is approximately
[0.096771, 0.700007]. Tests separately check 1/2, 0/1 and 1/1 intervals.

Every assistant message has 10 new input, 2 cache write, 3 cache read and 5 output
tokens: 20 total. Reasoning output is unreported, not zero. The six deliverables
each have one message. A seventh session has one event for D1 and one for Dsplit,
giving 8 allocated messages in the period: 160 tokens. Three other sessions have
one ambiguous project event (20 unallocated), one event without any evidence
(20 unassigned), and one excluded project event (20 other). Period total is 220.
January 11 adds another Dfollow message after the period: observed lifetime total
is 240, of which 180 is linked and 60 is separately unlinked/excluded.

Operational project spend is 160 + 20 = 180; final successful merges in the period
are D1, Dsplit and Dfollow: 180/3 = 60 tokens per success. The linked operational
view is 160/3 = 53.333333. All six dispatch in the period, so the cohort includes
180 linked lifetime tokens (failed work included): 180/3 = 60. Cohort token kinds
are 90 new input, 18 cache write, 27 cache read and 45 output; dividing each by 3
gives 30, 6, 9 and 15. Unknown reasoning remains null. No unlinked token is assigned
to a deliverable or spread into cohort costs.

Elapsed: D1, Dfail, Drev and Dsplit are 86,400 seconds from dispatch to merge or
abandonment. Dimm is 2,332,800 seconds to its immature merge. Dfollow is 345,600
seconds to the final repair merge. Parallel session time is not human waiting.
