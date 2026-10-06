"""Immutable sumbi-events v1 observations and optional native evidence.

The eight public event types share their meaning with docs/EVENTS.md. Native
logs can additionally report snapshots, request intervals and bounded counters.
These are observations, not accounting or outcome decisions. Nothing here
depends on Session, a measurement window, or a vendor record shape.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, TypeAlias


@dataclass(frozen=True)
class Object:
    """Immutable local JSON object, used only by recognized tool consumers."""

    items: tuple[tuple[str, "Value"], ...]


Value: TypeAlias = str | int | float | bool | None | Object | tuple["Value", ...]


def freeze(value) -> Value:
    if isinstance(value, dict):
        return Object(tuple((key, freeze(item)) for key, item in value.items()))
    if isinstance(value, list):
        return tuple(freeze(item) for item in value)
    return value


def thaw(value: Value):
    if isinstance(value, Object):
        return {key: thaw(item) for key, item in value.items}
    if isinstance(value, tuple):
        return [thaw(item) for item in value]
    return value


@dataclass(frozen=True)
class Identity:
    """Record digest or immutable event ID; scope determines conflict handling."""

    digest: bytes
    event_id: str | None = None
    scope: Literal["record", "event_id"] = "record"


@dataclass(frozen=True)
class Metadata:
    model: Value = None
    effort: Value = None
    cli_version: Value = None
    supplied: tuple[str, ...] = ()
    selection: Literal["observed", "context", "explicit"] = "observed"


@dataclass(frozen=True)
class SessionStart:
    cwd: str | None
    parent_session_id: str | None = None
    role: Literal["worker", "orchestrator"] = "orchestrator"
    agent: str | None = None
    provenance: Literal["first-observed-cwd", "session-header", "sumbi-events-v1"] = "sumbi-events-v1"
    metadata: Metadata = Metadata()


@dataclass(frozen=True)
class Context:
    cwd: str | None = None
    cwd_supplied: bool = True
    branch: Value = None
    metadata: Metadata = Metadata()
    attribution: bool = True
    execution_context: bool = False


@dataclass(frozen=True)
class Tokens:
    new_input: int | None = None
    cache_write: int | None = None
    cache_read: int | None = None
    output: int | None = None
    reasoning_output: int | None = None

    def as_dict(self):
        return vars(self).copy()


@dataclass(frozen=True)
class ToolInput:
    tool_call_id: str | None
    name: str | None
    arguments: Value = None


@dataclass(frozen=True)
class ToolOutput:
    tool_call_id: str | None
    output: Value = None
    text_blocks: bool = False


@dataclass(frozen=True)
class ToolEvidence:
    """Local tool observations; the builder derives paths and references."""

    inputs: tuple[ToolInput, ...] = ()
    outputs: tuple[ToolOutput, ...] = ()
    cwd: str | None = None
    branch: Value = None
    at: datetime | None = None
    own_time: bool = False
    apply: bool = True
    include_empty_refs: bool = False


@dataclass(frozen=True)
class TokenUsage:
    tokens: Tokens
    cwd: str | None = None
    selection: Literal["delta", "maximal_output", "cumulative_including_cache"] = "delta"
    message_id: str | None = None
    evidence: ToolEvidence = ToolEvidence()
    # Some native snapshots report total input including cache reads. Retain
    # that observation to detect resets without changing new_input's v1 meaning.
    input_total: int | None = None


@dataclass(frozen=True)
class ToolStart:
    tool_call_id: str | None
    suffix: str = ""
    at: datetime | None = None
    own_time: bool = False


@dataclass(frozen=True)
class ToolEnd:
    tool_call_id: str | None
    error: bool | None = None
    suffix: str = ""
    at: datetime | None = None
    own_time: bool = False
    count: bool = True
    interval: bool = True


@dataclass(frozen=True)
class CommandExecution:
    """Completed execution or its observed launch, with explicit pairing rules."""

    tool_call_id: str | None
    command: Value = None
    exit_code: int | None = None
    started_at: datetime | None = None
    cwd: Value = None
    cwd_supplied: bool = True
    phase: Literal["start", "end", "complete"] = "complete"
    pairing: Literal["explicit", "launch", "interval"] = "explicit"
    command_supplied: bool = True
    suffix: str = ""
    deferred: bool = False
    require_pair: bool = False
    infer_cwd: bool = False


@dataclass(frozen=True)
class FileEdit:
    identity: str | None = None
    suffix: str = ""


@dataclass(frozen=True)
class Counter:
    kind: Literal["compactions", "api_errors", "user_input_requests"]
    identity: str | None = None
    suffix: str = ""


@dataclass(frozen=True)
class Request:
    identity: str | None
    start: datetime | None = None
    end: datetime | None = None
    endpoint: Literal["start", "end"] | None = None


@dataclass(frozen=True)
class SessionEnd:
    selection: Literal["latest", "observed"] = "latest"


@dataclass(frozen=True)
class Resume:
    pass


@dataclass(frozen=True)
class LocalText:
    """Private review input; never part of public output."""

    content: Value


@dataclass(frozen=True)
class Diagnostic:
    category: Value
    counter: Literal["unknown", "invalid_token_records"] = "unknown"
    evidence_gap: bool = False
    token_gap: bool = False
    invalid_start: bool = False


Event: TypeAlias = (SessionStart | Context | TokenUsage | ToolStart | ToolEnd |
                    CommandExecution | FileEdit | Counter | Request | SessionEnd |
                    Resume | LocalText | Metadata | ToolEvidence | Diagnostic)


@dataclass(frozen=True)
class Record:
    """Ordered envelope shared by all translators.

    Multiple observations from one native record retain one deduplication
    identity and one ordering position. Open records contain their v1 event.
    Diagnostics are deferred until the builder resolves identity conflicts.
    """

    agent: str
    session_id: str | None
    timestamp: datetime | None
    identity: Identity
    events: tuple[Event, ...] = ()
    timestamp_supplied: bool = True
    observe_time: bool = True
    ordinal: int | None = None
    fallback_id: str | None = None
    parent_session_id: str | None = None
    worker: bool = False
    before_dedup: tuple[Metadata, ...] = ()
    usage_record: bool = False
