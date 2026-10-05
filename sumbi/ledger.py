"""Strict dispatch ledger parsing; free-text fields never leave this module."""

import csv
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import re

from sumbi.deliver_evidence import branch
from sumbi.model import timestamp

COLUMNS = ("id", "dispatched_at", "acceptance", "repos", "prs", "branches",
           "state_override", "accepted_by_human", "notes")
LABEL = r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}"
REPO = r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}/[A-Za-z0-9][A-Za-z0-9_.-]{0,99}"
PR = REPO + r"#[1-9][0-9]{0,9}"
PR_ENTRY = PR + r"(?::(?:constituent|retry|followup))?"


def utc(value, context):
    result = timestamp(value)
    if result is None or not isinstance(value, str) or not value.endswith(("Z", "+00:00")):
        raise ValueError(context + ": expected UTC ISO timestamp")
    return result


@dataclass(frozen=True)
class Deliverable:
    id: str
    dispatched_at: datetime
    repos: tuple[str, ...]
    prs: tuple[str, ...]
    branches: tuple[str, ...]
    abandoned: bool = False
    abandoned_at: datetime | None = None
    human: str = ""
    pr_roles: tuple[str, ...] = ()
    task_type: str | None = None

    def role(self, identity: str) -> str:
        return self.pr_roles[self.prs.index(identity)] if self.pr_roles else "constituent"


def read_ledger(path: Path) -> list[Deliverable]:
    rows = []
    ids, prs = set(), set()
    try:
        with path.open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream, strict=True)
            if reader.fieldnames not in (list(COLUMNS), [*COLUMNS, "task_type"]):
                raise ValueError("Ledger header must match the nine columns, optionally followed by task_type")
            for number, raw in enumerate(reader, 2):
                prefix = f"Ledger row {number}"
                if None in raw or any(v is None for v in raw.values()):
                    raise ValueError(prefix + ": field count must match the header")
                if any(len(v) > 4096 or any(ord(c) < 32 for c in v) for v in raw.values()):
                    raise ValueError(prefix + ": fields must be single-line and at most 4096 characters")
                if any(v != v.strip() for v in raw.values()):
                    raise ValueError(prefix + ": surrounding whitespace is not allowed")
                identity = raw["id"]
                if not re.fullmatch(LABEL, identity) or identity in ids:
                    raise ValueError(prefix + ": id must be a unique bounded identifier")
                ids.add(identity)
                dispatched = utc(raw["dispatched_at"], prefix + " dispatched_at")
                if not raw["acceptance"] or len(raw["acceptance"]) > 512:
                    raise ValueError(prefix + ": acceptance must contain 1 to 512 characters")

                def items(key, pattern=None):
                    values = tuple(raw[key].split(";")) if raw[key] else ()
                    if len(set(values)) != len(values) or any(not v or (pattern and not re.fullmatch(pattern, v)) for v in values):
                        raise ValueError(prefix + ": invalid or repeated " + key + " entry")
                    return values

                repos = tuple(v.lower() for v in items("repos", REPO))
                if not repos or len(set(repos)) != len(repos):
                    raise ValueError(prefix + ": repos must contain unique repository IDs")
                entries = items("prs", PR_ENTRY)
                pulls = tuple(v.split(":", 1)[0].lower() for v in entries)
                roles = tuple(v.split(":", 1)[1] if ":" in v else "constituent" for v in entries)
                if len(set(pulls)) != len(pulls) or any(v.rsplit("#", 1)[0] not in repos for v in pulls) or prs.intersection(pulls):
                    raise ValueError(prefix + ": PR must belong to repos and only one deliverable")
                prs.update(pulls)
                branches = items("branches")
                if any(branch(v) is None or ".." in v or "//" in v or v.endswith(("/", "."))
                       or any(p.startswith(".") or p.endswith(".lock") for p in v.split("/")) for v in branches):
                    raise ValueError(prefix + ": branches must be bounded literal branch names")
                override = raw["state_override"]
                abandoned = override == "abandoned" or override.startswith("abandoned@")
                if override and not abandoned:
                    raise ValueError(prefix + ": state_override only permits abandoned or abandoned@UTC")
                ended = utc(override[10:], prefix + " abandonment") if override.startswith("abandoned@") else None
                if ended and ended < dispatched:
                    raise ValueError(prefix + ": abandonment precedes dispatch")
                human = raw["accepted_by_human"]
                if human not in ("", "y", "n"):
                    raise ValueError(prefix + ": accepted_by_human must be blank, y or n")
                task_type = raw.get("task_type") or None
                if task_type is not None and not re.fullmatch(LABEL, task_type):
                    raise ValueError(prefix + ": task_type must be a bounded label")
                rows.append(Deliverable(identity, dispatched, repos, pulls, branches, abandoned, ended, human, roles, task_type))
    except (OSError, UnicodeError, csv.Error):
        raise ValueError("Ledger could not be read as strict UTF-8 CSV") from None
    return rows
