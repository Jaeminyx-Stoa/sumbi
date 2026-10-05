# sumbi

> **sumbi** (Korean *숨비*) — the breath a Korean *haenyeo* diver lets out when she surfaces from a long dive. She catches her breath, then dives again, a little better.
>
> sumbi gives coding agents room to catch their breath.

sumbi is a self-improving harness for coding agents. Seed a sensible harness in any repository, watch the friction and waste in how your agents work, and keep only the changes that verifiably raise the task success rate while cutting the time and tokens each success costs.

**Status:** M1a and M1b provide offline harness installation and read-only local
session measurement for Claude Code and Codex through one `sumbi` command.
Install records a repository-scoped 14-day baseline before applying practices.
M1d adds a deliverable ledger, recorded or read-only live GitHub pull-request
outcomes, PR roles and token cost per success. Checks at merge use an explicit
basis: historical requirements, current policy, or all visible results when no
checks are required. Unreadable policy remains unknown. M2 adds `sumbi compare`:
pre-registered dispatch cohorts, score intervals, deterministic cost bootstraps,
confounder flags and verdict proposals. Propagation remains planned.
Local verification additionally measures fixed dispatched-worker units using
declared script exit evidence; its weaker acceptance and worker-only cost scope
are explicit.

## Quickstart

```sh
pip install git+https://github.com/Jaeminyx-Stoa/sumbi
```

The repository is private for now. Installation needs GitHub repository access
until it is public.

In the repository you want to seed:

```sh
sumbi install              # Dry run: inspect the plan without applying it
sumbi install --apply      # Apply additive practices and record a baseline
sumbi collect --since 2030-01-01T00:00Z --until 2030-01-08T00:00Z
sumbi deliver --ledger deliverables.csv --outcomes recorded-outcomes \
  --since 2030-01-01T00:00Z --until 2030-01-08T00:00Z
sumbi compare --registration registration.json --ledger deliverables.csv \
  --outcomes recorded-outcomes
sumbi deliver --outcome-source local-verify --repository . --verify scripts/check.sh \
  --since 2030-01-01T00:00Z --until 2030-01-08T00:00Z \
  --scan-until 2030-01-09T00:00Z
```

Replace the example UTC bounds with your measurement window. Collection prints
a summary and writes `out/collect.json`. Install keeps its baseline and backups
local through `.sumbi/.gitignore`; `.sumbi/interventions.jsonl` remains committable.
Use `--home DIR` on measurement subcommands to select a local agent-log home.
Comparison windows, margin, sample size and follow-up days come from the
[documented pre-registration schema](docs/MEASUREMENT.md#registration-json-schema).
Use `SUMBI_SALT` or `--salt-file FILE` for keyed pseudonyms before sharing reports;
without either, output explicitly identifies keys as unsalted.
For local verification, pre-register `outcome_source: local-verify` and use
`sumbi compare --registration local-registration.json --repository .` with the
same bare verification declaration. It measures worker costs, with dispatch
overhead separate, and cannot certify human acceptance or later reverts. See
[local verification limits](docs/MEASUREMENT.md#local-verification-fixed-worker-session-outcomes).

`sumbi --help` lists the subcommands, `sumbi --version` prints the version, and
`python -m sumbi` runs the same CLI from a checkout. See
[measurement behavior and options](docs/MEASUREMENT.md) and
[install behavior and safety](sumbi/install/README.md).

## What it will do

- **Install (`sumbi install`).**
  - Takes stock of the harness a repository already has (instructions, skills, hooks, CI gates) and finds the gaps.
  - Applies proven practices from a curated catalog that distills Superpowers, gstack, gajae code and two in-house harness labs.
  - Discovering new candidates on GitHub only produces proposals for review; nothing is installed from the internet.
- **Measure (`sumbi collect`).** Read agent session logs (Claude Code and Codex first) and outcomes (CI runs, pull requests, reverts). It emits counts, durations, labels and IDs — never transcripts.
- **Judge.**
  - Count success per deliverable by outside signals.
  - Compare rounds on cost per success: time, and tokens split into new input, cache writes, cache reads and output.
  - Flag confounders, and decline to judge when the data is incomplete.
- **Propagate.** Deliver short notices to every agent through standard hooks and `AGENTS.md`, scoped by repository.
- **Seed a lab.** A starter kit for the harness lab itself, including a retro skill in the Agent Skills format.

## Principles

1. Success uses a declared machine outcome source: external acceptance per deliverable or the explicitly weaker local check per dispatched worker. Agent completion prose does not establish success.
2. Cost per success includes failures, retries and waiting.
3. Every harness change is an intervention: write the prediction first, then compare like with like.
4. No verdict ever weakens an approval, a review or a safety gate.
5. Change little at a time. Prefer devices (scripts, checks, ordering) over new rules, and keep always-loaded instructions short.

The design is in [docs/DESIGN.md](docs/DESIGN.md).

## License

[MIT](LICENSE). The license does not cover the sumbi name or logo.

The install catalog distills practices from Superpowers, gstack and gajae code (all MIT); the catalog credits them.
