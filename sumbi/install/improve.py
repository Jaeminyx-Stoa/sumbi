"""Evidence-linked, owner-reviewed local improvements; never execute payloads."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import stat

from sumbi import SCHEMA_VERSION
from sumbi.core.json import unique_object
from sumbi.core.time import Window
from .apply import apply_transaction, _protect_local_artifacts, _save_backup
from .errors import InstallError
from .exclusions import GitIgnore, excluded_by, load_excludes
from .files import MAX_BYTES, _checked, read_bytes, safe_path, checked_relative
from .inventory import inventory
from .planner import Change, digest
from .revert import LEDGER

LABEL = re.compile(r"[a-z][a-z0-9-]{0,63}")
HASH = re.compile(r"[a-f0-9]{64}")
MAX_TARGETS = 64
METRICS = {
    "/summary/sessions": "sessions",
    "/summary/counts/tool_errors": "tool-errors",
    "/summary/counts/api_errors": "api-errors",
    "/summary/counts/compactions": "compactions",
    "/summary/tokens/total": "reported-tokens",
    "/summary/tokens/new_input": "new-input-tokens",
    "/summary/tokens/cache_write": "cache-write-tokens",
    "/summary/tokens/cache_read": "cache-read-tokens",
    "/summary/tokens/output": "output-tokens",
    "/summary/tokens/reasoning_output": "reasoning-output-subset-tokens",
    "/summary/time/active_seconds/value": "estimated-active-seconds",
    "/summary/time/wall_span_seconds/value": "summed-wall-seconds",
    "/coverage/unattributed_share": "unattributed-share",
}
SUFFIXES = {".py", ".js", ".ts", ".tsx", ".jsx", ".json", ".toml", ".yaml",
    ".yml", ".md", ".txt", ".ini", ".cfg", ".sh"}
PRIVATE_PARTS = {".git", ".sumbi", ".ssh", ".aws", ".azure", ".gnupg", "sessions",
    "logs", "transcripts", "secrets", "credentials", ".env", "node_modules", ".venv"}


def _number(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _label(value):
    return isinstance(value, str) and LABEL.fullmatch(value)


def _hash(value):
    return isinstance(value, str) and HASH.fullmatch(value)


def _fields(value, names):
    if not isinstance(value, dict) or set(value) != set(names.split()):
        raise ValueError


def _private_text(value):
    return isinstance(value, str) and 0 < len(value.strip()) <= 16384 and "\x00" not in value


def _display_text(text: str) -> None:
    """Refuse controls that could disguise local diff contents or target names."""
    for index, character in enumerate(text):
        code = ord(character)
        if (code < 32 and character not in "\t\n\r" or 127 <= code <= 159
            or 0x202A <= code <= 0x202E or 0x2066 <= code <= 0x2069
            or character == "\r" and text[index + 1:index + 2] != "\n"):
            raise ValueError


def _load(path: Path, limit: int):
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > limit:
            raise ValueError
        data = path.read_bytes()
        if len(data) > limit:
            raise ValueError
        value = json.loads(data, object_pairs_hook=unique_object)
        return data, value
    except (OSError, UnicodeError, ValueError, TypeError, RecursionError):
        raise InstallError("Expected a bounded, unique-key local JSON object.") from None


def _lookup(report, pointer):
    value = report
    for part in pointer.split("/")[1:]:
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def observations(path: Path) -> tuple[bytes, dict]:
    """Project a collect report to finite numeric observations, never causal claims."""
    data, report = _load(Path(path), 16 * MAX_BYTES)
    try:
        if not isinstance(report, dict) or report.get("schema_version") != SCHEMA_VERSION:
            raise ValueError
        window = report["window"]
        _fields(window, "since until bounds")
        if window["bounds"] != "[since,until)":
            raise ValueError
        Window(datetime.fromisoformat(window["since"].replace("Z", "+00:00")),
            datetime.fromisoformat(window["until"].replace("Z", "+00:00")))
        if not _number(_lookup(report, "/summary/sessions")):
            raise ValueError
        findings = []
        for pointer, label in METRICS.items():
            value = _lookup(report, pointer)
            if value is None:
                continue
            if not _number(value) or label == "unattributed-share" and value > 1:
                raise ValueError
            findings.append({"id": label, "pointer": pointer, "value": value,
                "kind": "observation"})
        return data, {"version": 1, "evidence_sha256": digest(data),
            "findings": findings, "causality": "not-established",
            "outcome_verdict": "not-available-from-collect"}
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
        raise InstallError(
            "Evidence must be a supported collect report with numeric metrics.") from None


@dataclass(frozen=True)
class Improvement:
    root: Path = field(repr=False)
    evidence: Path = field(repr=False)
    bundle: Path = field(repr=False)
    bundle_sha256: str
    summary: dict
    changes: tuple[Change, ...] = field(repr=False)


def _target(root: Path, name: str, report: dict):
    if not isinstance(name, str) or len(name) > 240:
        raise ValueError
    _display_text(name)
    parts = checked_relative(name).parts
    lower = [part.casefold() for part in parts]
    if (any(part in PRIVATE_PARTS or part.startswith(".env.") for part in lower)
        or any(part in {".claude", ".codex"} for part in lower)
            and any(part in {"projects", "sessions"} for part in lower)
        or Path(name).suffix.casefold() not in SUFFIXES
        or any("secret" in part or "credential" in part or "private-key" in part
            for part in lower)
        or excluded_by(name, load_excludes(root))
        or any(name == path or name.startswith(path + "/")
            for path in report["versioning"]["nested_repositories"]["paths"])):
        raise ValueError
    path = safe_path(root, name)
    if GitIgnore(root).check(name):
        raise ValueError
    return path


def _validate_bundle(raw, evidence_data, measured):
    _fields(raw, "version intervention_id evidence_sha256 evidence_refs diagnosis prediction "
        "verification review changes")
    if type(raw["version"]) is not int or raw["version"] != 1:
        raise ValueError
    if not _label(raw["intervention_id"]) or raw["evidence_sha256"] != digest(evidence_data):
        raise ValueError
    refs = raw["evidence_refs"]
    available = {row["pointer"]: row["value"] for row in measured["findings"]}
    if not isinstance(refs, list) or not 1 <= len(refs) <= len(METRICS):
        raise ValueError
    seen = set()
    for ref in refs:
        _fields(ref, "pointer value")
        pointer = ref["pointer"]
        if (not isinstance(pointer, str) or pointer not in available or pointer in seen
            or not _number(ref["value"]) or ref["value"] != available[pointer]):
            raise ValueError
        seen.add(pointer)
    diagnosis = raw["diagnosis"]
    _fields(diagnosis, "id hypothesis rationale")
    if not _label(diagnosis["id"]) or not all(_private_text(diagnosis[k])
        for k in ("hypothesis", "rationale")):
        raise ValueError
    prediction = raw["prediction"]
    _fields(prediction, "metric direction rough_size_percent")
    if (prediction["metric"] not in {"tokens_per_success", "time_per_success", "success_rate"}
        or prediction["direction"] not in {"increase", "decrease"}
        or not _number(prediction["rough_size_percent"])):
        raise ValueError
    verification = raw["verification"]
    _fields(verification, "id status evidence_sha256 declaration")
    if (not _label(verification["id"]) or not _private_text(verification["declaration"])
        or not (verification["status"] == "passed" and _hash(verification["evidence_sha256"])
            or verification["status"] == "pending" and verification["evidence_sha256"] is None)):
        raise ValueError
    review = raw["review"]
    _fields(review, "author_family reviewer_family status safety_gates_preserved")
    if (not _label(review["author_family"])
        or not (review["status"] == "approved" and _label(review["reviewer_family"])
            and review["author_family"] != review["reviewer_family"]
            and review["safety_gates_preserved"] is True
            or review["status"] == "pending" and review["reviewer_family"] is None
            and review["safety_gates_preserved"] is None)):
        raise ValueError
    entries = raw["changes"]
    if not isinstance(entries, list) or not 1 <= len(entries) <= MAX_TARGETS:
        raise ValueError


def _changes(root, entries, report, evidence, bundle):
    changes, targets, hashes = [], set(), []
    for entry in entries:
        _fields(entry, "path before_sha256 after_sha256 after_text")
        name = entry["path"]
        _target(root, name, report)
        if name.casefold() in targets:
            raise ValueError
        targets.add(name.casefold())
        before = read_bytes(root, name)
        _checked(root, name, before)
        if before is not None:
            _display_text(before.decode("utf-8"))
        if (entry["before_sha256"] is not None and not _hash(entry["before_sha256"])):
            raise ValueError
        if entry["before_sha256"] != digest(before):
            raise InstallError("Improvement target changed; rebuild and review the bundle.")
        text = entry["after_text"]
        if not isinstance(text, str) or not text:
            raise ValueError
        _display_text(text)
        after = text.encode("utf-8")
        if (len(after) > MAX_BYTES or not _hash(entry["after_sha256"])
            or digest(after) != entry["after_sha256"]):
            raise ValueError
        if after == before:
            raise InstallError("Improvement bundle contains no-change targets.")
        if safe_path(root, name).resolve() in {Path(evidence).resolve(), Path(bundle).resolve()}:
            raise ValueError
        changes.append(Change(name, before, after))
        hashes.append({"before_sha256": digest(before), "after_sha256": digest(after)})
    return changes, hashes


def build_improvement(repository: Path | str, evidence: Path, bundle: Path) -> Improvement:
    """Validate reviewed hypotheses and exact bounded source/config/document edits."""
    root = Path(repository).resolve()
    if not root.is_dir():
        raise InstallError("Improve requires an existing workspace directory.")
    evidence_data, measured = observations(evidence)
    bundle_data, raw = _load(Path(bundle), MAX_BYTES)
    try:
        _validate_bundle(raw, evidence_data, measured)
        report = inventory(root)
        changes, hashes = _changes(root, raw["changes"], report, evidence, bundle)
        summary = {**measured, "intervention_id": raw["intervention_id"],
            "bundle_sha256": digest(bundle_data), "diagnosis_id": raw["diagnosis"]["id"],
            "evidence_refs": raw["evidence_refs"], "prediction": raw["prediction"],
            "verification": {k: raw["verification"][k]
                for k in ("id", "status", "evidence_sha256")},
            "review": raw["review"], "targets": hashes, "change_count": len(changes),
            "review_basis": "owner-local-attestation"}
        return Improvement(root, Path(evidence), Path(bundle), digest(bundle_data), summary,
            tuple(changes))
    except (ValueError, TypeError, KeyError, UnicodeError, OverflowError):
        raise InstallError(
            "Improvement bundle must match the reviewed local change schema.") from None


def apply_improvement(plan: Improvement, reviewed_sha256: str) -> dict:
    """Acknowledge exact review bytes; re-read evidence and targets under the lock."""
    if not _hash(reviewed_sha256) or reviewed_sha256 != plan.bundle_sha256:
        raise InstallError("Apply requires the exact reviewed bundle SHA-256.")
    if (plan.summary["review"]["status"] != "approved"
        or plan.summary["verification"]["status"] != "passed"):
        raise InstallError(
            "Apply requires approved independent review and passed verification attestations.")

    def prepare(originals, modes):
        fresh = build_improvement(plan.root, plan.evidence, plan.bundle)
        if fresh != plan:
            raise InstallError("Evidence, review bundle, or targets changed; review again.")
        ledger = read_bytes(plan.root, LEDGER)
        _checked(plan.root, LEDGER, ledger)
        try:
            if ledger is not None and ledger and not ledger.endswith(b"\n"):
                raise ValueError
            history = [json.loads(line, object_pairs_hook=unique_object)
                for line in (ledger or b"").splitlines()]
            if any(not isinstance(record, dict) for record in history):
                raise ValueError
        except (ValueError, UnicodeError):
            raise InstallError("Intervention ledger is invalid; no improvement applied.") from None
        identity = plan.summary["intervention_id"]
        if any(row.get("intervention_id", row.get("practice_id")) == identity for row in history):
            raise InstallError("Intervention ID already exists; use a new reviewed intervention.")
        for change in plan.changes:
            path = _checked(plan.root, change.path, change.before)
            originals[change.path] = change.before
            modes[change.path] = (stat.S_IMODE(path.stat().st_mode)
                if change.before is not None else 0o644)
        originals[LEDGER] = ledger
        ledger_path = safe_path(plan.root, LEDGER)
        modes[LEDGER] = stat.S_IMODE(ledger_path.stat().st_mode) if ledger is not None else 0o600
        _protect_local_artifacts(plan.root)
        now = datetime.now(timezone.utc)
        backup_relative = ".sumbi/backups/" + now.strftime("%Y%m%dT%H%M%S.%fZ")
        safe_path(plan.root, backup_relative).mkdir(parents=True, mode=0o700, exist_ok=False)
        record = {"action": "improve", "practice_id": "reviewed-local-change",
            **plan.summary, "utc_time": now.isoformat().replace("+00:00", "Z")}
        applied = {c.path: c.after for c in plan.changes}
        manifest = {"version": 1, "status": "prepared", "records": [record],
            "files": [{"path": name, "before_hash": digest(data), "mode": modes[name],
                "applied_hash": digest(applied.get(name)), "absent": data is None}
                for name, data in originals.items()]}
        _save_backup(plan.root, backup_relative, originals, manifest)
        return (LEDGER, ledger, {"status": "supplied collect evidence"},
            backup_relative, [record], manifest)

    return apply_transaction(plan.root, list(plan.changes), prepare)
