# sumbi

> **숨비 (sumbi)** — the breath a Korean *haenyeo* diver lets out when she surfaces from a long dive. She catches her breath, then dives again, a little better.
>
> 숨비 — 코딩 에이전트에게 숨 고를 틈을.

sumbi is a self-improving harness for coding agents. Seed a sensible harness in any repository, watch the friction and waste in how your agents work, and keep only the changes that verifiably raise the task success rate while cutting the time and tokens each success costs.

**Status:** early design. Nothing here is usable yet.

## What it will do

- **Measure.** Read agent session logs (Claude Code and Codex first) and outcomes (CI runs, pull requests, reverts). It emits counts, durations, labels and IDs — never transcripts.
- **Judge.**
  - Count success per deliverable by outside signals.
  - Compare rounds on cost per success: time, and tokens split into new input, cache writes, cache reads and output.
  - Flag confounders, and decline to judge when the data is incomplete.
- **Propagate.** Deliver short notices to every agent through standard hooks and `AGENTS.md`, scoped by repository.
- **Seed.** Starter kits for a harness lab and for product repositories:
  - a short `AGENTS.md` table of contents
  - brief and handoff templates
  - a retro skill in the Agent Skills format

## Principles

1. Success is judged by outside signals per deliverable, never by an agent's own report.
2. Cost per success includes failures, retries and waiting.
3. Every harness change is an intervention: write the prediction first, then compare like with like.
4. No verdict ever weakens an approval, a review or a safety gate.
5. Change little at a time. Prefer devices (scripts, checks, ordering) over new rules, and keep always-loaded instructions short.

The design is in [docs/DESIGN.md](docs/DESIGN.md).

## License

To be decided before the first public release.
