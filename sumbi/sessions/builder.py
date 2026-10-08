"""Fold normalized observations into the stable Session surface and derive feature evidence.
Named rules retain snapshot, cumulative usage, execution pairing, context, and dispatch semantics.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable

from sumbi.core.paths import execution_cwd
from sumbi.core.privacy import pseudonym
from sumbi.core.records import Coverage
from sumbi.core.time import Window
from sumbi.core.values import label
from sumbi.events import schema as e
from sumbi.events.authorship import result_refs
from sumbi.events.references import branch, branch_query, tool_refs
from sumbi.events.tool_paths import resolve_path, tool_evidence
from sumbi.sessions.session import Session


@dataclass
class _State:
    session: Session
    starts: list = field(default_factory=list)
    contexts: list = field(default_factory=list)
    metadata: dict = field(default_factory=dict)
    native_metadata: list = field(default_factory=list)
    maximal: dict = field(default_factory=dict)
    cumulative: list = field(default_factory=list)
    launches: dict = field(default_factory=dict)
    pending_cwds: dict = field(default_factory=dict)
    tool_names: dict = field(default_factory=dict)
    first_cwd_at: datetime | None = None
    invalid: bool = False
    token_invalid: bool = False
    explicit: bool = False
    native_start_seen: bool = False
    worker_links: bool = False


def own_context_at_start(history, started_at, own_start):
    """Only unambiguous own-session context in force at execution start counts."""
    if started_at is None or any(at is None for at, _ in history):
        return None
    eligible = [(at, cwd) for at, cwd in history if at <= started_at
        and (own_start is None or at >= own_start)]
    if not eligible:
        return None
    latest = max(at for at, _ in eligible)
    values = {cwd for at, cwd in eligible if at == latest}
    return next(iter(values)) if len(values) == 1 else None


def _metadata(state, metadata, when, window, coverage):
    session = state.session
    if metadata.selection == "context":
        state.native_metadata.append((when, metadata.model, metadata.effort))
        return
    targets = (("model", "models", "model"), ("effort", "efforts", "effort"),
        ("cli_version", "versions", "version"))
    for name, target, category in targets:
        if name not in metadata.supplied:
            continue
        value = e.thaw(getattr(metadata, name))
        if metadata.selection == "explicit":
            if when is None:
                continue
            if value is not None and not isinstance(value, str):
                coverage.unknown("invalid_context_metadata")
                state.invalid = True
                value = None
            state.metadata.setdefault(name, []).append((when, value))
        elif value and (name == "cli_version" or when is not None and when < window.until):
            getattr(session, target).add(label(value, category))


def _start(state, event, when, order, window, coverage, links):
    session = state.session
    if event.provenance != "sumbi-events-v1" and not state.native_start_seen:
        session.dispatch_kind = event.dispatch_kind
        state.native_start_seen = True
    if event.provenance == "first-observed-cwd":
        if when is not None and (session.start_at is None or when < session.start_at):
            session.start_at = when
        if when is not None and event.cwd and (state.first_cwd_at is None
            or when < state.first_cwd_at):
            state.first_cwd_at = when
            session.start_cwd, session.start_evidence = event.cwd, event.provenance
        session.cwd(event.cwd, when)
    elif event.provenance == "session-header":
        session.start_evidence = event.provenance
        if session.start_at is None or when is not None and when < session.start_at:
            session.start_at, session.start_cwd = when, event.cwd
        session.is_worker |= event.role == "worker"
        if event.parent_session_id is not None:
            session.parent_raw_id = event.parent_session_id
    else:
        state.starts.append((when, event))
        _context(state, e.Context(event.cwd, metadata=event.metadata), when, order, window,
            coverage, links)


def _context(state, event, when, order, window, coverage, links):
    session = state.session
    if event.cwd_supplied or not state.explicit:
        session.cwd(event.cwd, when)
        if event.attribution:
            session.context(when, order, event.cwd)
            if links and not state.explicit and when:
                session.deliverable_events.append((when, order, "context",
                    (branch(e.thaw(event.branch)), event.cwd)))
    if event.cwd_supplied and (event.execution_context or state.explicit):
        # Open cwd validation belongs to its translator. Normalize only the
        # conflict comparison; retain the original cwd on the Session surface.
        cwd = (None if event.cwd_valid is False
            else execution_cwd(event.cwd)) if state.explicit else event.cwd
        state.contexts.append((when, cwd))
    _metadata(state, event.metadata, when, window, coverage)


def _tool_evidence(state, evidence, when, order, links, *, apply=True, fallback=None):
    session = state.session
    at = evidence.at if evidence.own_time else when
    paths, cwd, refs = [], None, set()
    for item in evidence.inputs:
        arguments = e.thaw(item.arguments)
        workdir, operands = tool_evidence(item.name, arguments)
        cwd = workdir or cwd
        paths.extend(operands)
        if links:
            identity = _source_identity(item, evidence, fallback)
            if (state.worker_links and at and item.tool_call_id
                and item.name == "CommandExecution" and isinstance(arguments, dict)):
                session.deliverable_events.append((at, order, "execution_refs",
                    (item.tool_call_id, result_refs(arguments.get("aggregated_output",
                        arguments.get("output"))))))
            own_refs = tool_refs(item.name, arguments, worker=state.worker_links)
            operation = next((v for k, v in own_refs if k == "branch_operation"), None)
            state.tool_names[identity] = item.name, branch_query(item.name, arguments), operation
            refs.update(own_refs)
    if links:
        for item in evidence.outputs:
            output = e.thaw(item.output)
            if item.text_blocks and isinstance(output, list):
                output = "\n".join(b.get("text", "") for b in output
                    if isinstance(b, dict) and b.get("type") == "text")
            identity = _source_identity(item, evidence, fallback)
            name, query, operation = state.tool_names.get(identity, (None, False, None))
            if state.worker_links and at and item.tool_call_id:
                session.deliverable_events.append((at, order, "execution_refs",
                    (item.tool_call_id, result_refs(output))))
            refs.update(tool_refs(name, output, output=True, query=query,
                worker=state.worker_links, operation=operation))
    if apply and evidence.apply:
        session.tool_paths(at, order, cwd or (evidence.cwd if paths else None), paths)
        if links and at and (refs or evidence.include_empty_refs):
            session.deliverable_events.append((at, order, "refs", refs))
    return cwd, paths, refs


def _source_identity(item, evidence, fallback):
    if item.source_identity is not None:
        return e.thaw(item.source_identity.value)
    return (item.tool_call_id or fallback) if evidence.fallback_identity else item.tool_call_id


def maximal_output_snapshot(state, event, when, order, links):
    """Streaming repeats input/cache; greatest output wins, then latest time."""
    cwd, paths, refs = _tool_evidence(state, event.evidence, when, order, links, apply=False)
    identity = event.message_id or str(order[1])
    rank = event.tokens.output, when
    previous = state.maximal.get(identity)
    if previous is None or rank > previous[0]:
        state.maximal[identity] = (rank, event.tokens.as_dict(), order, event.cwd or cwd,
            paths, cwd, branch(e.thaw(event.evidence.branch)), refs)


def paired_execution(state, event, identity, when, fallback):
    """Pair launches without repairing contradictory start/cwd evidence."""
    session = state.session
    pairing_identity = e.thaw(
        event.source_identity.value) if event.source_identity is not None else (
            identity if event.tool_call_id is not None or event.suffix else fallback)
    if event.phase == "start":
        state.launches[pairing_identity] = event
        return
    launch = state.launches.get(pairing_identity)
    if event.require_pair and launch is None:
        return
    command, code = e.thaw(event.command), event.exit_code
    started, cwd = event.started_at, e.thaw(event.cwd)
    pending = False
    if event.pairing == "launch":
        started = launch.started_at if launch else None
        cwd = e.thaw(launch.cwd) if launch else None
        if not event.command_supplied or event.require_pair:
            command = e.thaw(launch.command) if launch else None
        if launch and launch.deferred:
            code = None
        pending = launch is None or not launch.cwd_supplied
    elif event.pairing == "interval":
        own_start = launch.started_at if launch else None
        explicit = launch is not None and launch.cwd_supplied
        start_conflict = launch is not None and (own_start is None or started is not None
            and started != own_start)
        cwd_conflict = explicit and event.cwd_supplied and (
            execution_cwd(e.thaw(launch.cwd)) != execution_cwd(cwd))
        started = None if start_conflict else started or own_start
        paired_cwd = explicit and own_start is not None and not start_conflict
        cwd = None if start_conflict or cwd_conflict else (
            cwd if event.cwd_supplied else e.thaw(launch.cwd) if paired_cwd else None)
        pending = start_conflict or cwd_conflict or not event.cwd_supplied and not paired_cwd
        if cwd_conflict:
            started_for_cwd = None
        else:
            started_for_cwd = started
    if state.explicit and command in (None, "", []):
        session.local_evidence_gaps["command_content_unknown"] += 1
    error = None if event.deferred or launch and launch.deferred else event.error
    session.execution(identity, when, command, code, started_at=started, cwd=cwd, error=error)
    if event.infer_cwd:
        if pending:
            state.pending_cwds[identity] = (started_for_cwd if event.pairing == "interval"
                else started)
        else:
            state.pending_cwds.pop(identity, None)


def _diagnostic(state, event, coverage):
    if event.counter == "unknown":
        coverage.unknown(e.thaw(event.category))
    else:
        coverage.invalid_token_records += 1
    if state is not None:
        state.invalid |= event.evidence_gap
        state.token_invalid |= event.token_gap
        if event.invalid_start:
            state.starts.append(None)


def _fold(state, event, record, order, fallback, source_fallback, window, coverage, review, links):
    session, when = state.session, record.timestamp
    identity = str(getattr(event, "tool_call_id", None) or getattr(event, "identity", None)
        or fallback) + (getattr(event, "suffix", "") if not (
            getattr(event, "tool_call_id", None)
            or getattr(event, "identity", None)) else "")
    if isinstance(event, e.SessionStart):
        _start(state, event, when, order, window, coverage, links)
    elif isinstance(event, e.Context):
        _context(state, event, when, order, window, coverage, links)
    elif isinstance(event, e.Metadata):
        _metadata(state, event, when, window, coverage)
    elif isinstance(event, e.TokenUsage):
        if event.selection == "maximal_output":
            if event.message_id is None:
                event = e.TokenUsage(event.tokens, event.cwd, event.selection, fallback,
                    event.evidence)
            maximal_output_snapshot(state, event, when, order, links)
        elif event.selection == "cumulative_including_cache":
            values = event.tokens.as_dict()
            values["input_total"] = event.input_total
            state.cumulative.append((when, order, values))
        else:
            values = event.tokens.as_dict()
            session.usage(when, order, values, cwd=event.cwd)
            if window.contains(when):
                session.add_tokens(values)
    elif isinstance(event, e.ToolEvidence):
        _tool_evidence(state, event, when, order, links, fallback=source_fallback)
    elif isinstance(event, (e.ToolStart, e.ToolEnd)):
        at = event.at if event.own_time else when
        start = isinstance(event, e.ToolStart)
        if start or event.count:
            session.count("tool_calls" if start else "tool_results", identity, at, window)
            if not start and event.error is True:
                session.count("tool_errors", identity, at, window)
        if start or event.interval:
            session.interval("tool", identity, at if start else None, None if start else at)
    elif isinstance(event, e.CommandExecution):
        paired_execution(state, event, identity, when, source_fallback)
    elif isinstance(event, e.FileEdit):
        session.edit(identity, when)
        cwd = execution_cwd(event.cwd) if event.cwd is not None else own_context_at_start(
            state.contexts, when, session.start_at)
        session.edit_targets[identity] = tuple(
            resolve_path(target, cwd) for target in event.targets)
    elif isinstance(event, e.Counter):
        session.count(event.kind, identity, when, window)
    elif isinstance(event, e.Request):
        session.interval("request", identity,
            event.start or (when if event.endpoint == "start" else None),
            event.end or (when if event.endpoint == "end" else None))
    elif isinstance(event, e.SessionEnd):
        if when is not None and when < window.until:
            session.completed_at = max(when, session.completed_at
                or when) if event.selection == "latest" else when
    elif isinstance(event, e.Resume):
        if window.contains(when):
            session.completed_at = None
    elif isinstance(event, e.LocalText):
        session.local_text(e.thaw(event.content), when, window, review)
    elif isinstance(event, e.Diagnostic):
        _diagnostic(state, event, coverage)


def cumulative_usage_deltas(state, window, coverage):
    """Sort cumulative snapshots, rebaseline resets, and exclude reasoning twice."""
    keys = ("input_total", "cache_read", "output", "reasoning_output")
    previous = dict.fromkeys(keys, 0)
    session = state.session
    for when, order, total in sorted(state.cumulative, key=lambda entry: (entry[0], entry[1])):
        reset = any(total[k] is not None and previous[k] is not None and total[k] < previous[k]
            for k in keys)
        baseline = dict.fromkeys(keys, 0) if reset else previous
        delta = {k: total[k] - baseline[k] if total[k] is not None and baseline[k] is not None
            else None for k in keys}
        if reset and window.contains(when):
            session.counts["counter_resets"] += 1
        fresh = delta.pop("input_total") - delta["cache_read"]
        if fresh < 0:
            if window.contains(when):
                coverage.invalid_token_records += 1
        elif fresh or any(value for value in delta.values() if value is not None):
            values = {**delta, "new_input": fresh}
            session.usage(when, order, values)
            if window.contains(when):
                session.add_tokens(values)
        previous = total


def immutable_dispatch(state, coverage):
    """An open session's dispatch is usable only when all starts agree."""
    signatures = state.starts
    session = state.session
    if signatures and all(s is not None
        and _dispatch_signature(s) == _dispatch_signature(signatures[0])
        for s in signatures):
        when, event = signatures[0]
        session.start_at, session.start_cwd = when, event.cwd
        session.start_evidence = event.provenance
        session.emitter_agent, session.parent_raw_id = event.agent, event.parent_session_id
        session.is_worker = event.role == "worker"
        session.dispatch_kind = "subagent" if session.is_worker else "interactive"
    elif signatures:
        coverage.unknown("conflicting_session_start")
        state.invalid = True


def _dispatch_signature(entry):
    if entry is None:
        return None
    when, event = entry
    return (when, event.agent, event.cwd, event.parent_session_id, event.role,
        *(e.thaw(getattr(event.metadata, key)) for key in ("model", "effort", "cli_version")))


def _explicit_metadata(state, window, coverage):
    session = state.session
    for name, target, category in (("model", session.models, "model"),
        ("effort", session.efforts, "effort"), ("cli_version", session.versions, "version")):
        entries = [(at, value) for at, value in state.metadata.get(name, []) if at < window.until]
        prior = [(at, value) for at, value in entries if at < window.since]
        relevant = [(at, value) for at, value in entries if window.contains(at)]
        if prior:
            last = max(at for at, _ in prior)
            relevant.extend((at, value) for at, value in prior if at == last)
        grouped = {}
        for at, value in entries:
            grouped.setdefault(at, set()).add(value)
        if any(len(values) > 1 for values in grouped.values()):
            coverage.unknown("conflicting_context_metadata")
            state.invalid = True
        session.metadata_incomplete[name] = any(value is None for _, value in relevant) and any(
            value is not None for _, value in relevant)
        target.update(pseudonym(category, value) for _, value in relevant if value is not None)


def _finish(state, window, coverage, links):
    session = state.session
    for identity, started in state.pending_cwds.items():
        if (execution := session.commands.get(identity)) is not None:
            execution.cwd = own_context_at_start(state.contexts, started, session.start_at)
    eligible = [(at, model, effort) for at, model, effort in state.native_metadata if at
        and at < window.until]
    prior = [entry for entry in eligible if entry[0] < window.since]
    relevant = [entry for entry in eligible if window.contains(entry[0])]
    if prior:
        relevant.append(max(prior, key=lambda entry: entry[0]))
    for _, model, effort in relevant:
        if model:
            session.models.add(label(e.thaw(model), "model"))
        if effort:
            session.efforts.add(label(e.thaw(effort), "effort"))
    for snapshot in state.maximal.values():
        (_, when), values, order, cwd, paths, command_cwd, own_branch, refs = snapshot
        session.tool_paths(when, order, command_cwd or (cwd if paths else None), paths)
        session.usage(when, order, values, cwd=cwd, paths=paths)
        if links:
            session.deliverable_events.append((when, order, "own", (own_branch, refs)))
        if window.contains(when):
            session.add_tokens(values)
    cumulative_usage_deltas(state, window, coverage)
    if state.explicit:
        session.token_evidence_incomplete = state.token_invalid
        cwds = {}
        for at, value in state.contexts:
            cwds.setdefault(at, set()).add(value)
        if any(len(values) > 1 for values in cwds.values()):
            coverage.unknown("conflicting_context_metadata")
            state.invalid = True
            session.attribution_events = [(at, order, kind,
                (None, True) if kind == "context" and len(cwds.get(at, ())) > 1 else data)
                for at, order, kind, data in session.attribution_events]
        immutable_dispatch(state, coverage)
        _explicit_metadata(state, window, coverage)
        if state.invalid:
            session.local_evidence_gaps["open_event_evidence_incomplete"] += 1


def _open_records(records, states, coverage):
    """Global event IDs: exact repeats are idempotent; conflicts discard both."""
    unique, conflicting = {}, set()
    for record in records:
        state = _state(states, record)
        event_id = record.identity.event_id
        if event_id is None:
            for event in record.events:
                if isinstance(event, e.Diagnostic):
                    _diagnostic(state, event, coverage)
            continue
        if event_id in unique:
            previous = unique[event_id]
            if record.identity.digest == previous.identity.digest and event_id not in conflicting:
                coverage.duplicate_events += 1
                continue
            coverage.unknown("conflicting_event_id")
            conflicting.add(event_id)
            for item in (previous, record):
                if (affected := _state(states, item)) is not None:
                    affected.invalid = True
                    affected.token_invalid |= item.usage_record
            continue
        unique[event_id] = record
    for order, (event_id, record) in enumerate(unique.items()):
        if event_id not in conflicting:
            yield order, record


def _state(states, record):
    if record.session_id is None:
        return None
    key = record.agent, record.session_id
    if key not in states:
        states[key] = _State(Session(record.agent, record.session_id, record.parent_session_id,
            is_worker=record.worker), explicit=record.identity.scope == "event_id")
    return states[key]


def build(records: Iterable[e.Record], window: Window, coverage: Coverage, *,
    local_review: bool = False, collect_links: bool = False,
    worker_links: bool = False) -> list[Session]:
    """Fold a translator stream; all accounting and feature decisions live here."""
    states = {}
    records = iter(records)
    first = next(records, None)
    if first is None:
        return []
    # Adapters each have one identity scope. A first record is retained without
    # materializing native streams; open identities require conflict preflight.
    source = _prepend(first, records)
    ordered = _open_records(source, states,
        coverage) if first.identity.scope == "event_id" else enumerate(source)
    for position, record in ordered:
        state = _state(states, record)
        if state is None:
            for event in record.events:
                if isinstance(event, e.Diagnostic):
                    _diagnostic(None, event, coverage)
            continue
        session = state.session
        state.worker_links = worker_links
        for metadata in record.before_dedup:
            _metadata(state, metadata, record.timestamp, window, coverage)
        if not state.explicit:
            if record.identity.digest in session.seen:
                coverage.duplicate_events += 1
                continue
            session.seen.add(record.identity.digest)
        if record.observe_time:
            if record.timestamp is not None:
                session.times.add(record.timestamp)
            elif record.timestamp_supplied:
                coverage.invalid_timestamps += 1
                state.invalid |= state.explicit
        sequence = len(session.seen)
        order = position if state.explicit else (record.ordinal if record.ordinal is not None
            else sequence, sequence)
        fallback = record.identity.event_id if state.explicit else record.fallback_id or str(
            sequence)
        source_fallback = (e.thaw(record.fallback_source_identity.value)
            if record.fallback_source_identity is not None else
            record.fallback_id if record.fallback_id is not None else sequence)
        for event in record.events:
            _fold(state, event, record, order, fallback, source_fallback, window, coverage,
                local_review, collect_links)
    for state in states.values():
        _finish(state, window, coverage, collect_links)
    return [state.session for state in states.values()]


def _prepend(first, records):
    yield first
    yield from records


def collect(adapter, home, window, coverage, *, local_review=False, collect_links=False,
    worker_links=False):
    """Collection seam for callers that need sessions rather than observations."""
    return build(adapter.collect(home, coverage), window, coverage,
        local_review=local_review, collect_links=collect_links, worker_links=worker_links)
