"""Half-open UTC measurement windows."""

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Window:
    since: datetime
    until: datetime

    def __post_init__(self):
        if (self.since.tzinfo is None or self.until.tzinfo is None
            or self.since.utcoffset().total_seconds() != 0
            or self.until.utcoffset().total_seconds() != 0 or self.since >= self.until):
            raise ValueError("Window bounds must be UTC and since must precede until")

    def contains(self, when: datetime | None) -> bool:
        return when is not None and self.since <= when < self.until

    def overlap(self, start: datetime, end: datetime) -> float:
        return max(0.0, (min(end, self.until) - max(start, self.since)).total_seconds())
