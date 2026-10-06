"""Session accounting and machine execution evidence."""

from __future__ import annotations
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from sumbi.core.time import Window
from sumbi.core.values import TOKEN_KINDS
from sumbi.core.privacy import pseudonym
from sumbi.events.tool_paths import resolve_path


EVIDENCE_TYPES = ("cwd", "tool_path", "previous_event", "unassigned")


COUNT_KINDS = ("compactions", "tool_calls", "tool_results", "tool_errors", "api_errors",
    "user_input_requests", "counter_resets")


@dataclass
class CommandExecution:
    """Agent-neutral machine execution evidence. Commands stay local only."""

    agent: str
    session: str
    at: datetime
    command: str | list[str] | None
    exit_code: int | None
    started_at: datetime | None = None
    cwd: str | None = None


@dataclass
class Session:
    agent: str
    raw_id: str
    parent_raw_id: str | None = None
    times: set[datetime] = field(default_factory=set)
    cwd_events: list[tuple[datetime | None, str]] = field(default_factory=list)
    models: set[str] = field(default_factory=set)
    efforts: set[str] = field(default_factory=set)
    versions: set[str] = field(default_factory=set)
    tokens: dict[str, int | None] = field(default_factory=lambda: dict.fromkeys(TOKEN_KINDS))
    counts: Counter = field(default_factory=Counter)
    seen: set[bytes] = field(default_factory=set)
    starts: dict[tuple[str, str], datetime] = field(default_factory=dict)
    ends: dict[tuple[str, str], datetime] = field(default_factory=dict)
    intervals: dict[tuple[str, str], tuple[datetime, datetime]] = field(default_factory=dict)
    counted: set[tuple[str, str]] = field(default_factory=set)
    review: set[str] = field(default_factory=set)
    attribution_events: list[tuple] = field(default_factory=list)
    # Bounded branch/PR evidence, enabled only by the deliver command.
    deliverable_events: list[tuple] = field(default_factory=list)
    # These fields are deliberately absent from as_dict/public collect output.
    start_at: datetime | None = None
    start_cwd: str | None = None
    start_evidence: str | None = None
    is_worker: bool = False
    commands: dict[str, CommandExecution] = field(default_factory=dict)
    edits: dict[str, datetime] = field(default_factory=dict)
    completed_at: datetime | None = None
    local_evidence_gaps: Counter = field(default_factory=Counter)
    # Open-format emitter identities stay local; IDs use the adapter namespace.
    emitter_agent: str | None = None
    token_evidence_incomplete: bool = False
    metadata_incomplete: dict[str, bool] = field(default_factory=dict)

    def public_agent(self):
        return "sumbi-events:" + pseudonym("agent", self.emitter_agent
            or "unknown") if self.agent == "sumbi-events" else self.agent

    def execution(self, identity, when, command, code, *, started_at=None, cwd=None):
        if when is not None:
            self.commands[str(identity)] = CommandExecution(
                self.agent, self.raw_id, when, command, code, started_at, cwd)
        else:
            self.local_evidence_gaps["command_timestamp_missing"] += 1

    def edit(self, identity, when):
        if when is not None:
            self.edits[str(identity)] = when
        else:
            self.local_evidence_gaps["edit_timestamp_missing"] += 1

    def id(self):
        return pseudonym("session", self.agent + ":" + self.raw_id)

    def cwd(self, value: object, when: datetime | None):
        if isinstance(value, str) and value:
            self.cwd_events.append((when, value))

    def add_tokens(self, values: dict[str, int | None]):
        for key, value in values.items():
            if value is not None:
                self.tokens[key] = (self.tokens[key] or 0) + value

    def context(self, when, order, cwd, *, reset=True):
        if when is not None:
            self.attribution_events.append((when, order, "context", (cwd, reset)))

    def tool_paths(self, when, order, cwd, paths):
        if when is not None and (cwd or paths):
            self.attribution_events.append((when, order, "tool", (cwd, paths)))

    def usage(self, when, order, values, *, cwd=None, paths=()):
        if when is not None:
            self.attribution_events.append((when, order, "usage", (values, cwd, paths)))

    def event_allocations(self, window, attributor, idle_minutes):

        context_cwd = None
        command_cwd = None
        turn_paths = set()
        previous = None
        previous_when = None
        for when, order, kind, data in sorted(self.attribution_events, key=lambda e: (e[0], e[1])):
            if when >= window.until:
                break
            if kind == "context":
                raw_cwd, reset = data
                context_cwd = resolve_path(raw_cwd)
                if self.agent == "sumbi-events" and context_cwd is None:
                    previous, previous_when = None, None
                if reset:
                    command_cwd, turn_paths = None, set()
                continue
            if kind == "tool":
                cwd, paths = data
                if cwd:
                    command_cwd = resolve_path(cwd, context_cwd)
                turn_paths.update(p for raw in paths
                    if (p := resolve_path(raw, command_cwd or context_cwd)))
                continue
            values, own_cwd, own_paths = data
            # A Claude line's cwd belongs to that message; Codex context is in force
            # at the closing cumulative snapshot, even across a directory change.
            cwd = resolve_path(own_cwd, context_cwd) if own_cwd else None
            if self.agent == "codex":
                cwd = command_cwd or context_cwd
            elif self.agent == "sumbi-events":
                cwd = cwd or context_cwd
            if cwd:
                link = attributor.event_link(cwd)
                evidence = "cwd"
            else:
                paths = turn_paths | {p for raw in own_paths
                    if (p := resolve_path(raw, context_cwd))}
                if paths:
                    links = [attributor.event_link(p) for p in sorted(paths)]
                    signatures = {(x["bucket"], x["project_key"], x["rule"]) for x in links}
                    link = links[0] if len(signatures) == 1 else {
                        "bucket": "unassigned", "project_key": None, "rule": None,
                        "evidence": "conflicting_tool_paths"}
                    evidence = "tool_path"
                elif (previous is not None and previous["bucket"] != "unassigned"
                    and previous_when is not None
                    and (when - previous_when).total_seconds() < idle_minutes * 60):
                    link, evidence = previous, "previous_event"
                else:
                    link = {"bucket": "unassigned", "project_key": None, "rule": None,
                        "evidence": "missing_evidence"}
                    evidence = "unassigned"
            previous, previous_when = link, when
            if not window.contains(when):
                continue
            yield when, order, values, link, evidence, cwd

    def allocations(self, window, attributor, idle_minutes):
        rows = {}
        for when, order, values, link, evidence, cwd in self.event_allocations(window,
            attributor, idle_minutes):
            identity = link["bucket"], link["project_key"], link["rule"]
            row = rows.setdefault(identity,
                {**link, "events": 0, "tokens": dict.fromkeys(TOKEN_KINDS),
                    "evidence_counts": dict.fromkeys(EVIDENCE_TYPES, 0),
                    "evidence_tokens": dict.fromkeys(EVIDENCE_TYPES, 0)})
            row["events"] += 1
            if self.agent == "sumbi-events":
                missing = row.setdefault("not_reported_events", dict.fromkeys(TOKEN_KINDS, 0))
                for kind in TOKEN_KINDS:
                    missing[kind] += values.get(kind) is None
            row["evidence_counts"][evidence] += 1
            total = sum(values.get(k) or 0 for k in TOKEN_KINDS if k != "reasoning_output")
            row["evidence_tokens"][evidence] += total
            for key, value in values.items():
                if value is not None:
                    row["tokens"][key] = (row["tokens"][key] or 0) + value
        for row in rows.values():
            row["tokens"]["total"] = sum(row["tokens"][k] or 0 for k in TOKEN_KINDS
                if k != "reasoning_output")
            if self.agent == "sumbi-events":
                missing = row.pop("not_reported_events")
                row["tokens"]["not_reported_events"] = missing
                row["tokens"]["evidence_incomplete"] = self.token_evidence_incomplete
                row["tokens"]["complete_total"] = (row["tokens"]["total"]
                    if not self.token_evidence_incomplete
                    and not any(missing[k] for k in TOKEN_KINDS[:4]) else None)
            total = row["tokens"]["total"]
            row["fallback_share"] = (row["evidence_tokens"]["previous_event"] / total if total
                else 0.0)
        return sorted(rows.values(),
            key=lambda r: (r["bucket"], r["project_key"] or "", r["rule"] or ""))

    def count(self, kind: str, key: object, when: datetime | None, window: Window):
        identity = (kind, str(key))
        if window.contains(when) and identity not in self.counted:
            self.counts[kind] += 1
            self.counted.add(identity)

    def interval(self, kind: str, key: object, start: datetime | None, end: datetime | None):
        identity = (kind, str(key))
        if start is not None:
            self.starts[identity] = min(start, self.starts.get(identity, start))
        if end is not None:
            self.ends[identity] = max(end, self.ends.get(identity, end))
        if identity in self.starts and identity in self.ends:
            begin, finish = self.starts[identity], self.ends[identity]
            if finish >= begin:
                self.intervals[identity] = begin, finish

    def local_text(self, value: object, when: datetime | None, window: Window, enabled: bool):
        if not enabled or not window.contains(when):
            return
        if isinstance(value, str):
            for line in value.splitlines():
                if line.lstrip().startswith("Harness friction:"):
                    self.review.add(line.strip())
        elif isinstance(value, list):
            for item in value:
                self.local_text(item, when, window, enabled)
        elif isinstance(value, dict):
            for key in ("text", "content"):
                self.local_text(value.get(key), when, window, enabled)

    def in_window(self, window: Window):
        return any(window.contains(t) for t in self.times) or any(
            window.overlap(a, b) > 0 for a, b in self.intervals.values())

    def project_paths(self, window: Window) -> set[str]:
        paths = {value for when, value in self.cwd_events if window.contains(when)}
        prior = [(when, value) for when, value in self.cwd_events if when and when < window.since]
        # A changed cwd on the first in-window event supersedes the previous context.
        first = min((t for t in self.times if window.contains(t)), default=window.until)
        if prior and not any(when == first for when, _ in self.cwd_events):
            last = max(t for t, _ in prior)
            paths.update(value for t, value in prior if t == last)
        if not paths:
            paths.update(value for when, value in self.cwd_events if when is None)
        return paths

    def as_dict(self, window: Window, attributor, idle_minutes: float):
        times = sorted(self.times)
        span = window.overlap(times[0], times[-1]) if times else 0.0
        thresholds = sorted({2.0, 5.0, 10.0, idle_minutes})
        active = {format(n, "g"): round(sum(window.overlap(a, b) for a, b in zip(times, times[1:])
            if (b - a).total_seconds() <= n * 60), 6)
            for n in thresholds}
        durations = {}
        for kind in ("tool", "request"):
            intervals = [(a, b) for (k, _), (a, b) in self.intervals.items() if k == kind]
            durations[kind] = {"measurement": "observed", "seconds": round(sum(
                window.overlap(a, b) for a, b in intervals), 6),
                "paired_intervals": sum(window.overlap(a, b) > 0
                    or (a == b and window.contains(a)) for a, b in intervals),
                "unpaired_starts": sum(key[0] == kind and key not in self.intervals
                    and window.contains(t)
                    for key, t in self.starts.items()),
                "unpaired_ends": sum(key[0] == kind and key not in self.intervals
                    and window.contains(t)
                    for key, t in self.ends.items())}
        total = sum(self.tokens[k] or 0 for k in TOKEN_KINDS if k != "reasoning_output")
        allocations = self.allocations(window, attributor, idle_minutes)
        result = {"id": self.id(),
            "parent_id": pseudonym("session", self.agent + ":" + self.parent_raw_id)
            if self.parent_raw_id else None, "agent": self.public_agent(),
            "project": attributor.session_link(self.project_paths(window)),
            "allocations": allocations,
            "spend_allocation": "events",
            "models": sorted(self.models), "efforts": sorted(self.efforts),
            "cli_versions": sorted(self.versions),
            "tokens": {**self.tokens, "total": total, "measurement": "observed",
                "total_basis": "reported_components_excluding_reasoning_subset"},
            "counts": {k: self.counts[k] for k in COUNT_KINDS},
            "time": {"wall_span": {"measurement": "observed", "seconds": round(span, 6)},
                "active": {"measurement": "estimated", "idle_minutes": idle_minutes,
                    "seconds": active[format(idle_minutes, "g")],
                    "sensitivity_seconds": active}, "durations": durations}}
        if self.agent == "sumbi-events":
            events = [values for at, _, kind, (values, *_) in self.attribution_events
                if kind == "usage" and window.contains(at)]
            missing = {kind: sum(values.get(kind) is None for values in events)
                for kind in TOKEN_KINDS}
            result["tokens"]["not_reported_events"] = missing
            result["tokens"]["evidence_incomplete"] = self.token_evidence_incomplete
            result["tokens"]["complete_total"] = (total if events
                and not self.token_evidence_incomplete
                and not any(missing[k] for k in TOKEN_KINDS[:4]) else None)
        return result
