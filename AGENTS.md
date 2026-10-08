# AGENTS.md — working on sumbi

The design is in [docs/DESIGN.md](docs/DESIGN.md). Read the parts your task touches; this file only holds the rules that always apply.

- **Purpose and lane.**
  - sumbi audits how coding agents work and improves the harness: instructions, checks, devices, automations and briefs. The aim is higher goal achievement and fewer tokens per goal.
  - When you study another project's run, your output is limited to three things: a harness diagnosis, harness changes, and measurement.
  - That project's product plans, product decisions and product briefs belong to its own agent and owner. Do not make them, and do not ask about them.

- **This repository will be public.** Never commit any of these:
  - real session logs or transcripts, customer data, secrets
  - internal company, project or host names
  - absolute paths from a developer's machine

  Tests use synthetic fixtures only. If you compare against real logs, keep the data and the results out of the repository and put only summary numbers in your report.
- **Python 3.11+, standard library only.** No runtime dependencies. Run the tests with `python -m unittest discover -s tests`.
- **Output is counts, durations, labels and IDs.** Anything a person typed (prompts, messages, command arguments, test names, folder names) stays out of committed output.
- **Branch → pull request → review → merge.** Do not merge your own pull request. A reviewer from a different model family reviews it, and the maintainer merges.
- **End your report** with `Harness friction: <one line>` when these rules, the tools or the checks slowed you down. Write `Harness friction: none` otherwise.
