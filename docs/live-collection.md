# Local live collection

The snapshot v1 row and account contracts remain unchanged. `summarize_requests`
and `collect_inventory` remain available for complete supplied inputs.
`RequestAccumulator` supplies the same native request normalization to full and
incremental collection. No runtime dependency or background service is needed.
Native discovery and incremental collection sit in the existing measurement
layer; the top-level live API shares that layer for compatibility. Locks and
atomic JSON storage live in core, with no deferred imports or dependency cycles.

## Incremental JSONL sources

Pass explicitly resolved sources and an owned local cache to the reader:

```python
from sumbi.measure.live_incremental import IncrementalReader, collect_incremental
from sumbi.live import write_snapshot

with IncrementalReader(cache_path) as reader:
    data = collect_incremental(inventory, reader, now)
write_snapshot(data, output_directory)
```

`inventory` uses the existing `windows` list: engine, lane, optional account and
session ID, and `source_paths`. Inline records/messages also remain supported.
Sources are read-only. Native source discovery is a separate adapter, so the
reader does not search a machine for logs.

The cache key includes source path, engine and caller session ID. Each entry
keeps device/inode, size, modification/change times, committed newline offset,
normalized request IDs/times/counters and numeric quota fields. It never stores
messages, tool arguments or native log bodies. Baseline and old request IDs
remain available after their requests leave the two hourly windows, so late
stream updates retain the first request time and cumulative counter boundaries.
The current implementation retains these normalized observations for the entire
session; cache size follows request count rather than raw log size.

Bootstrap streams with explicit limits: 512 MB, two million lines per source
scan, and four MB per line. Reaching a byte/row limit leaves the source unknown
and resumes on the next call. Partial final lines are deferred; unchanged partial
files incur zero additional bytes, and the incomplete line is reread only when
new bytes arrive. Coverage reports bytes actually returned by source reads,
new rows, partial lines, bootstrap limits/count and cache recovery.

Unchanged metadata skips source reads. Observed truncation, replacement,
metadata-changing same-size rewrites, engine or session changes recalculate the
state. As with any metadata-based append reader, an in-place rewrite that
preserves all inspected metadata, or truncation followed by regrowth past the
old size between observations, cannot be distinguished from an append without
rereading existing source bytes.

A nonblocking `flock` protects cache load/read/save. Another collector fails with
`CollectorBusy`; malformed cache schemas trigger a new bootstrap. Saving uses a
flushed temporary file and atomic replacement. A caller publishing a collection
should hold a separate collection lock across discovery, collection and publish.

## Native and account adapters

`live_inventory` accepts a tmux target, optional explicit socket, process root,
SQLite path and injectable command runner. It reads window engine/lane/sid/cfg
and pane PID. Codex sources come from actual open rollout FDs below that pane;
multiple candidate FDs remain unknown. Claude sources use only the exact sid in
its cfg's projects directory. `SourceProbe` confirms native format and caches
only metadata and an engine label. Unsupported formats remain unknown.

`sqlite_session` uses `mode=ro` and an exact session ID, then passes decoded
assistant messages to the shared normalizer. A synthetic WAL regression verifies
uncheckpointed rows are visible. Missing/mismatching IDs and DB errors remain
unknown; it never selects a different recent session or uses immutable mode.
SQLite session rows are queried on each collection. Materialized payload bytes
are distinct from physical database IO, which is not measured by this adapter.

`claude_accounts` reads the existing five-hour and seven-day quota shapes;
`codex_limits` extracts primary/secondary rollout quota windows. Account identity
is `(engine, account)`. Percent, duration and reset must all be present for an
`ok` sample; missing fields remain null/unknown, and expired or old samples are
stale. `cached_claude_accounts` persists only normalized quota fields and skips
unchanged source bytes while recalculating freshness. No authentication or
network calls are made. Installation-specific account mapping belongs to the
local caller, not the public module.

## Publication

`write_snapshot` writes a complete `.generation-*` directory and switches a
single `current` symlink under a publication lock. `snapshot.json`, `LATEST.md`
and `fortress-line.txt` are relative convenience links through that pointer.
JSON and Markdown include observation time and coverage. Markdown shows compact
Korean column titles and explicitly marks engines with zero observed windows.
The fortress output is one line ending in exactly one newline. Missing values
produce `unknown` in fleet totals instead of silently contributing zero.

For a consistent multi-file read, call `read_snapshot_bundle(directory)` or
resolve `current` once and open all files within that generation. Separate opens
of convenience links can straddle a subsequent generation switch. Failed
publication leaves the previous complete generation available. Old generations
are retained so readers holding a generation path remain valid.

The local runner, real source paths, evidence, quota caches and scheduler drafts
belong in ignored local storage. Nothing here installs a schedule or sends a
message.
