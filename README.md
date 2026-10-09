# sumbi

> **sumbi** (Korean *숨비*) — the breath a Korean *haenyeo* diver lets out when she surfaces from a long dive. She catches her breath, then dives again, a little better.
>
> sumbi gives coding agents room to catch their breath.

sumbi helps you improve the harness around coding agents. Inspect and seed a
repository's instructions, measure session costs and outcomes, and propose
changes for review using pre-registered comparisons.

**Status:** alpha software. Offline harness installation and read-only local
session measurement for Claude Code and Codex through one `sumbi` command.
Install records a repository-scoped 14-day baseline before applying practices.
A deliverable ledger joins recorded or read-only live GitHub pull-request
outcomes, PR roles and token cost per success. Checks at merge use an explicit
basis: historical requirements, current policy, or all visible results when no
checks are required. Unreadable policy remains unknown. `sumbi compare` provides
pre-registered dispatch cohorts, score intervals, deterministic cost bootstraps,
confounder flags and verdict proposals. Native session adapters support Claude
Code and Codex; pull-request outcome measurement supports GitHub. Automatic
propagation and candidate discovery remain planned. No comparison applies a
change automatically.
Local verification additionally measures fixed dispatched-worker units using
declared script exit evidence; its weaker acceptance and worker-only cost scope
are explicit.
Worker GitHub outcomes additionally join dispatched subagents and non-interactive
launcher sessions to their own PR or branch evidence, without a deliverable
ledger. Repeated `--repo owner/name` options scope remote repositories, including
workers in sibling worktrees; dispatcher overhead remains separately reported.
Any coding agent or wrapper can opt into L1 session measurement and L2 local
worker outcomes by emitting [sumbi-events JSONL v1](https://github.com/Jaeminyx-Stoa/sumbi/blob/main/docs/EVENTS.md). This does
not imply a proprietary native integration or instruction-loading support for
every agent. Select `--agents sumbi-events` and use a private `--home` for its
logs; the native adapter defaults remain unchanged.

## Quickstart

```sh
pip install git+https://github.com/Jaeminyx-Stoa/sumbi
```

Until the owner publishes the repository, installation requires repository
access. Package-index installation is a separate release step; this quickstart
uses the source repository.

In the repository you want to seed:

```sh
sumbi install              # Dry run: inspect the plan without applying it
sumbi install --apply      # Apply additive practices and record a baseline
sumbi collect --since 2030-01-01T00:00Z --until 2030-01-08T00:00Z
sumbi improve --evidence out/collect.json  # Observations; no causal verdict
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
local through `.sumbi/.gitignore`. The intervention ledger is outside those
ignores and contains relative target paths; review it before committing or
sharing it.
Use `--home DIR` on measurement subcommands to select a local agent-log home.
Comparison windows, margin, sample size and follow-up days come from the
[documented pre-registration schema](https://github.com/Jaeminyx-Stoa/sumbi/blob/main/docs/MEASUREMENT.md#registration-json-schema).
Use `SUMBI_SALT` or `--salt-file FILE` for keyed pseudonyms before sharing reports;
without either, output explicitly identifies keys as unsalted.
For local verification, pre-register `outcome_source: local-verify` and use
`sumbi compare --registration local-registration.json --repository . --verify scripts/check.sh` with the
same bare verification declaration. It measures worker costs, with dispatch
overhead separate, and cannot certify human acceptance or later reverts. See
[local verification limits](https://github.com/Jaeminyx-Stoa/sumbi/blob/main/docs/MEASUREMENT.md#local-verification-fixed-worker-session-outcomes).

`sumbi --help` lists the subcommands, `sumbi --version` prints the version, and
`python -m sumbi` runs the same CLI from a checkout. See
[measurement behavior and options](https://github.com/Jaeminyx-Stoa/sumbi/blob/main/docs/MEASUREMENT.md) and
[install behavior and safety](https://github.com/Jaeminyx-Stoa/sumbi/blob/main/sumbi/install/README.md).

## Implemented scope

- **Install (`sumbi install`).**
  - Takes stock of the harness a repository already has (instructions, skills, hooks, CI gates) and finds the gaps.
  - Applies additive practices from a curated catalog that distills Superpowers, gstack, gajae code and two in-house harness labs.
  - Installation uses the bundled catalog and makes no network request.
- **Measure (`sumbi collect`, `sumbi deliver`).** Read native Claude Code and Codex logs or opt-in agent-neutral events, plus GitHub pull-request outcomes. The open format supports local verification outcomes without assigning GitHub ownership. Reports contain counts, durations, labels and IDs; raw text is restricted to an explicitly requested local review file.
- **Improve (`sumbi improve`).** Turn collect observations into an owner-authored, evidence-linked proposal for a bounded set of source/configuration/document changes. Review-bound application reuses install backups and rollback; [local review and comparison workflow](docs/IMPROVE.md).
- **Compare (`sumbi compare`).**
  - Count success per dispatched deliverable using recorded outcome evidence.
  - Compare rounds on cost per success: time, and tokens split into new input, cache writes, cache reads and output.
  - Flag confounders, and decline to judge when the data is incomplete.

Propagation through hooks, a harness-lab starter kit, and discovery of new
catalog candidates are roadmap work. Discovery is designed to create review
proposals rather than install untrusted code.

## Principles

1. Success uses a declared machine outcome source: external acceptance per deliverable or the explicitly weaker local check per dispatched worker. Agent completion prose does not establish success.
2. Cost per success includes failures, retries and waiting.
3. Every harness change is an intervention: write the prediction first, then compare like with like.
4. No verdict ever weakens an approval, a review or a safety gate.
5. Change little at a time. Prefer devices (scripts, checks, ordering) over new rules, and keep always-loaded instructions short.

The design is in [docs/DESIGN.md](https://github.com/Jaeminyx-Stoa/sumbi/blob/main/docs/DESIGN.md).
The implementation layers and extension points are in [the architecture guide](docs/ARCHITECTURE.md).
Release gates and owner-controlled publication steps are in
[the release guide](https://github.com/Jaeminyx-Stoa/sumbi/blob/main/docs/RELEASE.md).

## License

[MIT](https://github.com/Jaeminyx-Stoa/sumbi/blob/main/LICENSE). The license does not cover the sumbi name or logo.

The install catalog distills practices from Superpowers, gstack and gajae code (all MIT); the catalog credits them.
