# Tests and CLI characterization

Use Python 3.11 or later, the standard library, and local Git. Tests use authored
synthetic inputs and recorded GitHub responses; they never need network access.

```sh
python -m unittest discover -s tests
```

## Temporary roots and Git isolation

Every test that creates repositories or invokes Git uses
`support.IsolatedTemporaryDirectory`. It sets `GIT_CEILING_DIRECTORIES` to its
own temporary root and its parent, retaining any outer test ceilings. The parent
ceiling matters when a Git probe starts at the temporary root itself. Register
`cleanup` with `TestCase.addCleanup`, or use the helper as a context manager.
Nested roots must be cleaned up in reverse creation order. Local repositories
explicitly initialized within these roots remain visible. Copy install fixture
repositories into these roots before scanning them; scanning the checked-in
fixture directory would measure the enclosing checkout instead.

The mocked GitHub REST adapter tests additionally use
`support.isolate_github_destinations`. Its destination checker inspects `.git`
files directly instead of invoking Git, so ceilings alone cannot isolate it.
The helper passes synthetic ancestry bounded by the active test root through
the real checker, retaining all `.git` checks inside that root and rejecting
destinations outside active roots. It does not patch `Path.exists`, rename or
hide checkout metadata. A separate test proves the unpatched real checker still
rejects a destination inside the enclosing checkout. These adapter tests measure
recorded REST behavior; the characterization corpus uses recorded outcomes and
never replaces a product boundary checker.

Run the entire suite with temporary directories inside the checkout:

```sh
python tests/run_suite.py --temp-root local/test-tmp
```

This mode sets `TMPDIR`, `TEMP`, `TMP` and Python's cached temporary directory.
It leaves the checkout's `.git` untouched. Equivalently, set the environment
before starting Python. PowerShell:

```powershell
New-Item -ItemType Directory -Force local/test-tmp | Out-Null
$env:TEMP = Join-Path $PWD 'local/test-tmp'
$env:TMP = $env:TEMP
$env:TMPDIR = $env:TEMP
python -m unittest discover -s tests
```

WSL or POSIX, from the checkout:

```sh
mkdir -p local/test-tmp
TMPDIR="$PWD/local/test-tmp" TEMP="$PWD/local/test-tmp" TMP="$PWD/local/test-tmp" \
  python3 -m unittest discover -s tests
```

POSIX permission tests probe the selected temporary filesystem before asserting
mode preservation. They skip on a filesystem that cannot represent those modes,
including WSL Windows-drive mounts without Linux metadata support. They still
run on a filesystem that supports the modes. Windows retains its existing
platform and symlink capability skips.

## Characterization corpus

`characterization/manifest.json` declares each case's real CLI arguments, input
setup, command and expected exit status. The runner calls `sumbi.cli.main` in
process, capturing argparse exits as well as returned statuses. A coverage test
checks the manifest against the subcommands advertised by `sumbi --help` and
requires successful and stderr-producing error cases for every command. Adding
a new command requires adding its cases; help alone does not count as success.

Cases cover native and opt-in event collection, text/file-JSON and stdout-JSON
routing, local review, recorded GitHub delivery and comparison, native and event
local verification, every install fixture's dry-run and JSON plan, and each
fixture's apply/revert lifecycle. The fully configured install fixture has an
empty plan: its attempted revert pins the missing-backup diagnostic too.
Root help and the missing-command error have the argparse contract below;
version output remains byte-pinned.
The merged measurement commands add catalog pre-registration (both outcome
sources, late registration and refusal/error paths), verifier-never-passed
signals, actual apply-time exposure, named installation interventions, native
Claude worker verification, and freshness-based in-progress workers.

Inputs are private copies of `tests/fixtures` and `tests/install/fixtures`.
Native local-verification streams in `characterization/fixtures` are newly
authored versions of the existing local-verification test shapes. No script in
a fixture is executed. Event comparisons replay the existing open-event stream
with a second day's timestamps and distinct event/session IDs for the after arm.
Fixture markers become temporary repository locations. These repositories have
one fixed synthetic origin, so keyed project pseudonyms stay identical across
temporary locations and operating systems. Recorded deliver/compare operands
remain synthetic paths. All cases use the manifest's fixed synthetic salt.

Each `golden/<case>/` stores `stdout.txt`, `stderr.txt`, `exit-code.txt`, every
written CLI JSON file, and a separate `stdout.json` for stdout-JSON cases. Local
review captures only authored synthetic fixture text. Apply/revert captures both
stages' complete directory/file listings, installed/restored file contents,
original backup bytes, baseline JSON and the append-only interventions ledger.
Private recovery manifests are listed in the trees, but their contents are not
CLI output and include filesystem-specific permission modes. Existing apply and
revert unit tests exercise those manifests and permissions separately. File
trees do not contain filesystem timestamps, owners or other host metadata.
The test-authored tree listings sort case-folded relative path components on
both platforms. This fixes the test's own ordering; CLI ordering is untouched.
Captured `.gitignore` contents use a `.gitignore.txt` storage filename so they
cannot accidentally hide sibling golden files from Git. Tree listings retain
the actual `.gitignore` name. Golden attributes preserve LF and allow the CLI's
existing trailing whitespace rather than trimming it.

The runner blocks sockets, DNS, urllib requests and subprocesses other than its
allow-listed local Git operations. Source fixture files and product files are
never changed. Golden mismatches show a unified diff; a missing or extra output
artifact fails too. The suite runs the corpus again in the same process and twice
in fresh Python processes, comparing every artifact to the first run. Running
that suite on Windows and WSL checks both platforms against the same goldens.

## Argparse output contract

Argparse help and usage formatting belongs to the Python interpreter and can
change between supported versions. Help stdout and usage-error stderr (exit 2)
are not byte-compared with captured goldens, including in the regeneration
command's check mode. Their exit status, nonempty expected stream and empty
opposite stream are checked, along with the parser's command name, every option
and every subcommand name. Help must include all option aliases; usage must
include each option's first spelling, as argparse usage omits aliases. Usage
checks select the parser that emits the error: the register subparser for missing
registration arguments, and the root parser for root-level validation errors.

All sumbi-formatted output and the complete artifact set remain byte-pinned.
Same-process and fresh-process determinism still compare every artifact,
including argparse output, byte-for-byte within the running Python version.
Existing argparse goldens remain capture records, not cross-version contracts.

## Normalization boundary

`characterization/normalizers.py` performs exact string replacements registered
from specific generated fields. It does not reserialize JSON or sort output.

- The case's absolute temporary root, including its JSON-escaped representation,
  becomes `<TEMP>`.
- Install `observed_starts.window.since/until` become named start-window tokens.
- Generated baseline filenames use `__BASELINE_ID__`; baseline JSON's wall-clock
  `window.since/until` become named baseline-window tokens.
- The generated backup ID becomes `__BACKUP_ID__` in paths and revert output.
- Intervention `utc_time` fields become `<APPLY_TIME>` or `<REVERT_TIME>`.
- Newly published registration `registered_at` becomes `<REGISTERED_AT>`;
  its planned application and dispatch window timestamps remain unchanged.

Logged measurement timestamps, counts, durations, labels, IDs, pseudonyms,
ordering, hashes, floating-point values and all other numbers remain unchanged.
Fixture copies use authored LF text. Reading generated text files translates the
OS newline convention to LF; CLI strings captured in process already use LF.
This text transport convention does not alter product serialization or source
files. Never add a normalizer to conceal a product change or nondeterminism.

## Regeneration rule

**Regenerate only in a PR whose purpose is an intended output change, and say so
in the PR.** This R0 PR establishes the initial corpus; later refactor phases must
pass its existing goldens byte-for-byte. Tests never write or regenerate goldens.
The explicit regeneration command first runs the corpus twice and refuses to
write if the two runs differ:

```sh
python tests/characterization/regenerate.py --write --temp-root local/test-tmp
```

Omit `--write` to check against existing goldens with unified diffs. `--dump PATH`
writes a private determinism snapshot, without touching any golden files; the
fresh-process tests use this mode. Keep snapshots under ignored `local/`.
Review all golden changes and explain the intentional behavior change in the PR.
When rebasing R0 onto merged output changes, list the changed golden files and
the merged PR responsible for each. An unexplained change must stop the work.
