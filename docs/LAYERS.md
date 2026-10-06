# Package layers

Imports point toward earlier layers:

```text
core <- events <- sessions <- measure <- outcomes/install <- judge <- cli
```

`core` owns bounded values, UTC windows, JSONL coverage, path normalization,
privacy, strict JSON objects, and shared descriptive statistics. `catalog` is
bundled data with standard-library loaders and is available to the upper layers.
Version and schema constants remain in `sumbi/__init__.py`.

`events` owns log adapters, their registry, tool paths, and machine references.
Callers supply the unchanged `Session` constructor through `session_factory`;
adapters do not import the session layer. The shared token-kind tuple lives in
core and is exposed by `sessions/session.py` for session consumers.

`sessions` retains session accounting and execution evidence. `measure` owns
attribution and collection reports. GitHub outcomes and local verification keep
their separate implementations under `outcomes`. Wilson intervals and the
descriptive cost estimate live in core because outcome reports use them;
`judge/stats.py` exposes those primitives alongside comparison statistics.

`install/files.py` owns bounded reads, safe paths, Markdown code filtering, and
checked file publication. Inventory, exclusions, conventions, baseline, apply,
and revert import these shared helpers directly. The baseline hook calls
measurement directly and retains the existing public status strings, including
the legacy entry-point label in its fallback status record.

`judge` owns registration, intervention exposure, and the two comparison
implementations. `cli` owns argument parsing and command execution. The console
entry point remains `sumbi.cli:main`; `sumbi/__main__.py` and
`sumbi/install/__main__.py` are thin command delegates and are the only entry
exceptions to the layer order. Old internal module paths have no shims.

`tests/test_layers.py` parses all package modules, including nested imports,
resolves relative imports, and rejects later-layer edges, unknown internal paths,
deferred imports, and cycles. Behaviour and output remain covered by the existing
R0 corpus; refactors must not regenerate its goldens.
