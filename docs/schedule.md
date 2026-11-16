# Local schedule

`sumbi schedule` runs only the jobs that are due right now, then exits. An
external scheduler calls it once a minute; it is not a resident process.

```
python -m sumbi schedule --config jobs.toml --state .schedule
```

Keep the job table and the state directory outside the repository. The command
reads the table with the standard library `tomllib` and needs no dependency.

```toml
[[job]]
name = "example-live"
command = ["python", "-m", "example", "collect"]
every_sec = 300
timeout_sec = 120
cwd = "."

[[job]]
name = "example-daily"
command = ["python", "-m", "example", "report"]
at_utc = "07:30"
timeout_sec = 3600
```

Each `[[job]]` needs a bounded `name`, a `command` array of strings that runs
without a shell, and exactly one of:

- `every_sec`: run when the time since the last start is at least this many
  seconds, with a 30-second tolerance for a one-minute external tick. It must be
  at least 60; a smaller value is a configuration error and no job runs.
- `at_utc`: run once a day at `HH:MM` UTC after that time has passed.

`timeout_sec` defaults to 3600. When it passes, the command's whole process
group receives `SIGTERM`, then `SIGKILL` after up to five seconds, so children
the command started do not survive the timeout. `cwd` is optional.

State under `--state`:

- `<name>.lock`: a nonblocking `flock` held while the job runs. A job whose lock
  is held is skipped as `busy` rather than started twice.
- `<name>.json`: the last start time in epoch seconds, written atomically before
  the command runs.
- `runs.jsonl`: one line per job per call with `name`, `start`, `end`, `rc` and
  `status` (`ok`, `failed`, `timeout`, `busy` or `skipped`). Command output is
  inherited and never recorded.

Exit codes: `0` when every job that ran returned zero, `1` when any job that ran
failed or timed out, and `2` when the configuration is invalid, in which case no
job runs and one line goes to standard error.
