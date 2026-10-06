# Architecture

sumbi translates local agent logs into observations, folds them into sessions,
measures costs and declared outcomes, and proposes comparisons for review.
The public reports contain counts, durations, labels and IDs; raw text and
execution operands stay local. See [DESIGN.md](DESIGN.md) for product policy.

## Layers

[The layer test](../tests/test_layers.py) checks every import in the AST,
including relative and nested imports. Imports may point to the same or a lower
layer; cycles and deferred internal imports are rejected.

| Level | Packages | Contract |
| --- | --- | --- |
| 0 | `core`, `catalog` | Values, privacy, paths, time, accounting primitives; bundled text practices |
| 1 | `events` | Immutable observations and log translation |
| 2 | `sessions` | Shared event fold and stable session accounting |
| 3 | `measure` | Project attribution and allow-listed collection reports |
| 4 | `outcomes`, `install` | Declared machine outcomes; offline inventory and guarded installation |
| 5 | `judge` | Pre-registration, exposure, statistics and ordered verdict proposals |
| 6 | `cli` | Argument validation, dispatch and output routing |

`sumbi.__main__` and `sumbi.install.__main__` are CLI entry-point exceptions.
Outcome sources never import the judge; install never imports it either.
CLI modules compose the lower layers and own user-facing diagnostics.

## Observations and sessions

`events/schema.py` defines frozen `Record` envelopes with an agent/session
identity, timestamp, deduplication identity, ordering hints and an event tuple.
The eight [open v1 event types](EVENTS.md) describe starts, ends, context,
tokens, tools, executions and edits. Native adapters also expose snapshot,
request, pairing, metadata and diagnostic observations. Translators receive
only a log home and coverage counters; vendor dictionaries never enter the
session builder.

`sessions/builder.py` owns the shared fold. Named rules select maximal output
snapshots, derive cumulative usage deltas, pair executions, resolve context at
execution start, and validate immutable dispatch identity. Open event IDs are
checked globally: exact repeats are idempotent, while conflicts cannot supply
favorable evidence. Native streams retain their own deduplication and ordering.
The resulting `Session` holds accounting, attribution and local machine evidence;
its public projection omits commands, edits and private review text.

## Outcomes and comparisons

`outcomes/units.py` defines fixed `Unit` inputs and the `OutcomeSource` protocol.
GitHub joins session costs to a dispatch ledger and outside PR evidence; local
verification selects dispatched workers and checks declared script executions
after their last known edit. Their acceptance and cost scopes remain distinct.

`judge/compare.py` owns one engine: dispatch arms, exposure exclusions, metadata
mixes, statistics and ordered verdicts. A source supplies measurement, coverage
reasons, gates at the candidate/retained/coverage stages, and an ordered public
schema projection. Source order keys preserve bootstrap order, and source
settings preserve each metadata counting unit. Comparison entry points in
`judge/compare_github.py` and `judge/compare_local.py` select these implementations.

## Install scanners

`install/inventory/` shares one `InventoryContext` with cached, bounded readers,
warnings and explicit section data. The package entry point scans traversal,
versioning, instructions, imports, capabilities, enforcement and conventions
in that order, then reads conditional rules and computes costs. Scanners do not
import sibling scanners; assembly preserves report and diagnostic order.

The planner creates additive managed blocks from the bundled catalog. Placement
uses observed session starts and versioned load rules. Apply and revert own
locking, checked publication, backups and rollback; baseline collection runs
before target writes. [Install boundaries](../sumbi/install/README.md) document
the safety and detection limits.

## Characterization and extension points

The [R0 corpus](../tests/README.md#characterization-corpus) runs real CLI arguments
against authored fixtures and recorded outcomes. It compares the complete
artifact set with reviewed goldens, then repeats in the same process and fresh
processes. Fixed synthetic identities and narrowly declared dynamic-field
normalizers keep Windows and WSL comparable. Interpreter-owned argparse output
has a structural contract; all sumbi-formatted output remains byte-pinned.
Refactoring must pass the existing corpus without regenerating it.

For a new agent adapter, add a translator under `events/adapters/`, register it
and its log root in `events/registry.py`, and test frozen observations plus the
shared session fold. Accounting and outcome decisions belong in the builder
and outcome layers, rather than the translator.

For a new outcome source, implement `OutcomeSource` under `outcomes/` and return
fixed `Unit` records with explicit completeness and ordering. Add its comparison
entry point, registration validation and CLI selection, then cover its gates,
public projection and CLI artifacts. Keep shared statistical and verdict policy
in the engine. Tests mirror the package layers; cross-layer CLI cases and the
characterization corpus exercise their composition.
