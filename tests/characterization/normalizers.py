"""Exact replacements for generated locations, clock fields and artifact IDs.

No normalizer parses, sorts, rounds or rewrites measurements or pseudonyms.
Callers explicitly register each generated value from its documented field.
"""

import json


class Normalizer:
    def __init__(self, root):
        self.replacements = {}
        for value in (str(root), root.as_posix()):
            self.add(value, "<TEMP>")
            self.add(json.dumps(value)[1:-1], "<TEMP>")

    def add(self, value, placeholder):
        if not isinstance(value, str) or not value:
            raise ValueError("A variable field must be a nonempty string")
        existing = self.replacements.get(value)
        if existing is not None and existing != placeholder:
            raise ValueError("Variable fields have conflicting replacements")
        self.replacements[value] = placeholder

    def starts(self, report):
        window = report["observed_starts"]["window"]
        for bound in ("since", "until"):
            self.add(window[bound], "<START_WINDOW_" + bound.upper() + ">")

    def baseline(self, relative, report):
        identity = relative.rsplit("/", 1)[1].removesuffix(".json")
        self.add(identity, "__BASELINE_ID__")
        for bound in ("since", "until"):
            self.add(report["window"][bound], "<BASELINE_WINDOW_" + bound.upper() + ">")

    def ledger(self, rows):
        for row in rows:
            action = row.get("action")
            if action == "revert":
                self.add(row["utc_time"], "<REVERT_TIME>")
                self.add(row["backup_id"], "__BACKUP_ID__")
            else:
                self.add(row["utc_time"], "<APPLY_TIME>")

    def text(self, value):
        # Longest first prevents replacing a root prefix before its escaped form.
        for original in sorted(self.replacements, key=len, reverse=True):
            value = value.replace(original, self.replacements[original])
        return value
