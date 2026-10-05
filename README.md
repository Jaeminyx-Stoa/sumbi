# sumbi

> **sumbi** (Korean *숨비*) — the breath a Korean *haenyeo* diver lets out when she surfaces from a long dive. She catches her breath, then dives again, a little better.
>
> sumbi gives coding agents room to catch their breath.

sumbi is a self-improving harness for coding agents. Seed a sensible harness in any repository, watch the friction and waste in how your agents work, and keep only the changes that verifiably raise the task success rate while cutting the time and tokens each success costs.

**Status:** M1a provides read-only local session measurement for Claude Code and
Codex. Outcomes, judging and propagation remain planned.

Run `python -m sumbi.cli collect --since 2030-01-01T00:00Z --until 2030-01-02T00:00Z`
from a checkout, or install the package to use `sumbi collect`. The command prints
a summary and writes `out/collect.json`. See [measurement behavior and options](docs/MEASUREMENT.md).

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

1. Success is judged by outside signals per deliverable, never by an agent's own report.
2. Cost per success includes failures, retries and waiting.
3. Every harness change is an intervention: write the prediction first, then compare like with like.
4. No verdict ever weakens an approval, a review or a safety gate.
5. Change little at a time. Prefer devices (scripts, checks, ordering) over new rules, and keep always-loaded instructions short.

The design is in [docs/DESIGN.md](docs/DESIGN.md).

## License

[MIT](LICENSE). The license does not cover the sumbi name or logo.

The install catalog distills practices from Superpowers, gstack and gajae code (all MIT); the catalog credits them.
