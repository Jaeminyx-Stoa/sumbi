# One improvement cycle

This page fixes the order of one round: measure a window before a change,
change one thing, measure an equally long window after it, and compare. It only
uses commands that exist today; option details stay in
[MEASUREMENT.md](MEASUREMENT.md) and [IMPROVE.md](IMPROVE.md). All names and
paths below are synthetic.

## Order

1. **Fix two windows of equal length.** Pick a before window and an after window
   of the same length (for example 7 days each) that are adjacent around the
   planned application time. Windows are `[since, until)` in UTC. Do not extend
   or shorten either window after seeing the numbers.
2. **Collect both windows with the same options.** Use the same `--home`,
   `--agents`, `--idle-minutes` and project rules for both, and name the target
   repository so its rows are not only a hash (see below).

   ```sh
   sumbi collect --since 2030-01-01T00:00Z --until 2030-01-08T00:00Z \
     --project sample --match-path '*/sample' --json ../sumbi-out/before.json
   sumbi collect --since 2030-01-08T00:00Z --until 2030-01-15T00:00Z \
     --project sample --match-path '*/sample' --json ../sumbi-out/after.json
   ```

   Run the after collection only once the after window has closed.
3. **Compare with the same metrics.** Read the two reports side by side on the
   same fields (sessions, errors, compactions, tokens by kind, active time).
   `sumbi improve --guide --evidence ../sumbi-out/before.json` prints the
   observations for one report; it makes no causal claim.
4. **Choose one intervention from the before evidence.** Keep a round to one to
   three coupled changes. Before applying, write the prediction first: metric,
   direction and rough size. `sumbi register` copies a catalog prediction; a
   custom prediction uses the [registration schema](MEASUREMENT.md#registration-json-schema).
5. **Apply.** Either a pull request in the repository that owns the harness
   file, or a reviewed `sumbi improve --bundle ... --apply --reviewed-sha256 ...`
   (see [IMPROVE.md](IMPROVE.md)). Apply does not claim improvement.
6. **Measure again.** Collect the after window as above. With a declared
   success source, run `sumbi compare` using the pre-registration; the
   comparison proposes a verdict and flags confounders. Then pick the next
   intervention, or revert.

Write the prediction before the after window starts; a prediction written after
seeing the numbers is not a prediction.

## Name the target repository

`sumbi collect` and the other measurement commands attribute spend per event.
Without rules, inferred projects are grouped automatically and appear only as
pseudonymous keys (`project_key`), so you must guess which key is your
repository. Name it up front:

- `--project NAME` starts a named rule. `NAME` is a label you choose and it
  appears in reports, so pick a public-safe one.
- Each following `--match-path GLOB` or `--match-origin GLOB` belongs to that
  rule. Patterns are shell-style globs and `*` matches across directory
  separators. Give several to one rule by repeating them.
- Repeat `--project NAME2 --match-...` for another rule.
- `--match-origin` matches the normalized Git origin (host/repository, no
  credentials or `.git`) and takes priority: when any origin rule is configured,
  a known origin that matches none is `other` and cannot fall back to a path.
  `--match-path` alone also covers working folders that no longer exist or have
  no usable origin.

Forgetting this usually means collecting again, which can be slow (below).

## Confounders

A before/after difference is an observation, not an effect. Write down, for
both windows, anything else that changed:

- **Workload.** Number and size of tasks, a release rush, a quiet week.
- **Model mix.** Which models and which agent products ran, and any model
  switch inside a window.
- **Account and session switches.** Switching accounts, machines or log homes
  moves sessions between logs; resumed sessions split or merge. Check that both
  windows read the same `--home` and that coverage (files scanned, unreadable
  files) is comparable.
- **Other changes.** Instruction edits, hooks or tooling changed by someone else
  during either window.

`sumbi compare` flags the confounders it can see; it cannot see the rest. List
the others next to the result.

## Success denominator

Cost alone cannot say a change helped: cheaper sessions that deliver less are
not an improvement. A cost-per-success or success-rate claim needs a declared
machine outcome source (see the next section). **If you have none, report the
cost side only** (tokens, time, errors) and make no causal or improvement
claim, not even a hedged one. Agent completion prose is not an outcome.

## Own progress numbers as a denominator

A host repository may track its own progress count, for example the number of
results accepted by an outside party. Today sumbi offers three sources. None
of them reads an arbitrary external counter, and none invents a new format.

| Source | Fits when | Does not prove |
| --- | --- | --- |
| **Deliver ledger** (`sumbi deliver --ledger`, template [deliverables.template.csv](deliverables.template.csv)) with GitHub outcomes (`--outcomes github` live, or a recorded-outcomes directory) | The accepted thing is a GitHub pull request. Each row is one dispatched deliverable, fixed at dispatch, listing its PRs. Success needs a merge with green checks at merge and a complete observation window. Human acceptance can be required with `accepted_by_human`. | Anything that is not a pull request: a count kept in another system, an external acceptance outside GitHub. Quality beyond merged and checked. Rows are written by hand, so a row made after the fact does not prove it was fixed at dispatch. |
| **Recorded outcomes** (`--outcomes DIR`) | You want the same ledger judged offline from saved GitHub responses, repeatably. | It is the same GitHub PR evidence, only saved. It does not add a new kind of outcome and is only as complete as the recording (`pulls_complete`, `commits_complete`). |
| **Local verification** (`--outcome-source local-verify`, with scripts declared in `.sumbi/config.toml` or `--verify`; events emitted as [sumbi-events L2](EVENTS.md) `command_execution` or read from native logs) | There is no PR flow, and a bare, argument-free script can exit 0 exactly when the worker's unit is acceptable. Units are dispatched workers fixed from session start. | Human acceptance, later reverts, that tests actually ran, or the progress number itself: the script exit code is the only evidence, the event producer is not authenticated, and cost covers workers only with dispatcher overhead reported separately. |

Practical reading:

- If the progress number is "pull requests accepted by the outside party" and
  they are GitHub PRs, use the ledger. This is the strongest source.
- If it is anything else, sumbi cannot count it. The closest option is a
  repository script that checks the acceptance condition and exits 0 or
  nonzero, declared as the verifier, while accepting that this is the weaker
  local outcome per worker. Do not emit a custom counter in L2 events: the
  format carries execution exit codes, not named metrics.
- Otherwise keep the progress number outside sumbi, and report it next to the
  cost comparison without computing a cost per success from it.

## Time and where files live

- Collection scans whole log files, including events before and after the
  window (for baselines and completed pairs); there is no modification-time
  filter. A large home with many or long logs can take a long time, so start it
  early and do not rerun for a missing flag. Narrow with `--agents` and a
  dedicated `--home` where you can.
- Keep reports, ledgers, recorded responses, bundles and salt files **outside**
  the repository being measured and outside version control (the examples write
  to a sibling `../sumbi-out/`). Reports hold only counts and labels, but
  ledgers, evidence and bundles can hold private text, and sumbi refuses to
  write into source log directories.
- Use `--salt-file FILE` (or `SUMBI_SALT`) consistently for both windows so
  pseudonymous keys match across reports.
