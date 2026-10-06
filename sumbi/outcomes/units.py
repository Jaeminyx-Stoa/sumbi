"""Source-neutral comparison inputs and the outcome-source plug-in contract."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

EXCLUDED_SHARE = 0.10
UNATTRIBUTED_SHARE = 0.05
MIX_DISTANCE = 0.20


def fraction(numerator, denominator):
    """Accounting fractions are observed censuses, without sampling intervals."""
    return {"numerator": numerator, "denominator": denominator,
            "value": numerator / denominator if denominator else None,
            "interval_95": None, "interval_method": "not_applicable_census"}


def flag(name, blocking, evidence):
    return {"name": name, "blocking": blocking, "evidence": evidence}


@dataclass(frozen=True)
class Unit:
    """Fixed dispatch evidence; public fields retain their source's wire order.

    order_key preserves the source's historical bootstrap order independently
    of the public ID. Metadata is already sanitized by the measurement layer.
    The three-key lookup lets existing statistical primitives consume units.
    """

    id: str
    dispatched_at: datetime
    state: str
    reason: str
    exposure_times: tuple[datetime, ...]
    session_metadata: tuple[dict, ...]
    tokens: dict
    tokens_complete: bool
    elapsed_seconds: float | None
    task_type: str | None
    public: dict = field(repr=False)
    order_key: str = field(repr=False)
    unlinked: bool = False
    excluded_state: str | None = None
    checks_basis: tuple[str, ...] = ()
    start_scope: str | None = None

    def __getitem__(self, key):
        if key not in ("state", "tokens", "elapsed_seconds"):
            raise KeyError(key)
        return getattr(self, key)


class OutcomeSource(Protocol):
    """Measurement and source-specific gates never depend on the judge layer.

    Gates run at candidates, retained, and coverage stages, in that order.
    public_fields adapts the shared results to the existing ordered schema.
    """

    name: str
    registration_error: str
    mix_kinds: tuple[str, ...]
    deduplicate_metadata: bool
    exclusion_flag: str
    exclusion_collection: str
    risky_exclusions: bool
    sort_exclusion_counts: bool
    task_breakdowns: bool
    finalized_elapsed_only: bool

    def measure(self, home, period, registration, windows, *, agents, salt) -> tuple[list[Unit], dict]: ...
    def source_gates(self, arms, *, candidates, report, stage) -> list[dict]: ...
    def coverage_reasons(self, arms, *, candidates, report, windows) -> list[str]: ...
    def public_fields(self, report, comparison) -> dict: ...
