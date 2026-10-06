"""Hash-guarded, per-file reversal with an append-only intervention audit."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import stat

from .apply import _checked, _write
from .errors import InstallError
from .inventory import checked_relative, read_bytes, safe_path
from .planner import digest

BACKUP_ID = re.compile(r"[0-9]{8}T[0-9]{6}\.[0-9]{6}Z")
HASH = re.compile(r"[a-f0-9]{64}")
LEDGER = ".sumbi/interventions.jsonl"


def _object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate key")
        value[key] = item
    return value


def _manifest(root: Path, backup_id: str) -> list[dict]:
    base = ".sumbi/backups/" + backup_id
    data = read_bytes(root, base + "/manifest.json")
    try:
        manifest = json.loads(data, object_pairs_hook=_object)
        if type(manifest["version"]) is not int or manifest["version"] != 1 or manifest["status"] != "applied":
            raise ValueError
        files = manifest["files"]
        if not isinstance(files, list) or not files:
            raise ValueError
        seen, targets = set(), []
        for entry in files:
            path = entry["path"]
            if not isinstance(path, str) or path.casefold() in seen:
                raise ValueError
            seen.add(path.casefold())
            # Validate path grammar without following a target's links. A link
            # introduced after apply is refused per file during the operation.
            parts = checked_relative(path).parts
            if any(p.casefold() in {".git", ".sumbi"} for p in parts) and path != LEDGER:
                raise ValueError
            # Apply records S_IMODE, including supported POSIX special bits.
            # File-type bits and non-integer values remain invalid.
            if type(entry["absent"]) is not bool or type(entry["mode"]) is not int or not 0 <= entry["mode"] <= 0o7777:
                raise ValueError
            before = entry["before_hash"]
            if entry["absent"]:
                if before is not None:
                    raise ValueError
            elif not isinstance(before, str) or not HASH.fullmatch(before):
                raise ValueError
            if path == LEDGER:
                continue  # Ledger history is never replaced by a backup.
            if not isinstance(entry["applied_hash"], str) or not HASH.fullmatch(entry["applied_hash"]):
                raise ValueError
            original = None if entry["absent"] else read_bytes(root, base + "/files/" + path)
            if digest(original) != before:
                raise ValueError
            targets.append({**entry, "original": original})
        if not targets:
            raise ValueError
        return targets
    except (TypeError, KeyError, ValueError, UnicodeError):
        raise InstallError("Backup manifest is unsupported or invalid; no files reverted.") from None


def revert_install(repository: Path | str, backup_id: str) -> dict:
    """Restore only unchanged applied bytes; retain local backup protection."""
    if not isinstance(backup_id, str) or not BACKUP_ID.fullmatch(backup_id):
        raise InstallError("Backup ID must be an install timestamp ID.")
    root = Path(repository).resolve()
    if not root.is_dir():
        raise InstallError("Revert requires an existing workspace directory.")
    if not safe_path(root, ".sumbi").is_dir():
        raise InstallError("No local install metadata exists; no files reverted.")
    lock = safe_path(root, ".sumbi/install.lock")
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise InstallError("Another install is active or a recovery lock remains.") from None
    os.close(descriptor)
    written, results = [], []
    try:
        targets = _manifest(root, backup_id)
        ledger = read_bytes(root, LEDGER)
        ledger_path = _checked(root, LEDGER, ledger)
        ledger_mode = stat.S_IMODE(ledger_path.stat().st_mode) if ledger is not None else 0o600
        try:
            if not ledger or not ledger.endswith(b"\n"):
                raise ValueError
            history = [json.loads(line, object_pairs_hook=_object) for line in ledger.splitlines()]
            if any(not isinstance(r, dict) for r in history):
                raise ValueError
        except (TypeError, ValueError, UnicodeError):
            raise InstallError("Intervention ledger is invalid; no files reverted.") from None
        prior = [r for r in history if r.get("action") == "revert" and r.get("backup_id") == backup_id]
        finished = {f["path"] for r in prior for f in r.get("files", [])
                    if isinstance(f, dict) and f.get("status") in {"restored", "already-reverted"}}
        for entry in targets:
            path = entry["path"]
            if path in finished:
                results.append({"path": path, "status": "already-reverted"})
                continue
            try:
                current = read_bytes(root, path)
                _checked(root, path, current)
                if digest(current) != entry["applied_hash"]:
                    results.append({"path": path, "status": "refused", "reason": "content-changed"})
                    continue
                if entry["original"] is None:
                    _checked(root, path, current).unlink()
                else:
                    _write(root, path, current, entry["original"], entry["mode"])
                written.append((entry, current))
                results.append({"path": path, "status": "restored"})
            except (OSError, InstallError):
                results.append({"path": path, "status": "refused", "reason": "unsafe-or-unavailable"})
        # Successful files are sticky: another installation or owner edits must
        # never be undone by repeating an old revert. Identical retries add no
        # audit noise, but changed outcomes append a new record.
        normalized = [{**f, "status": "already-reverted"} if f["status"] == "restored" else f for f in results]
        prior_normalized = [{**f, "status": "already-reverted"} if f["status"] == "restored" else f
                            for f in prior[-1].get("files", [])] if prior else None
        if normalized != prior_normalized:
            record = {"action": "revert", "backup_id": backup_id, "files": results,
                      "utc_time": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")}
            _write(root, LEDGER, ledger, ledger + (json.dumps(record, sort_keys=True) + "\n").encode(), ledger_mode)
        return {"backup_id": backup_id, "files": results,
                "refused": sum(f["status"] == "refused" for f in results)}
    except BaseException as error:
        failed = False
        for entry, applied in reversed(written):
            try:
                _write(root, entry["path"], entry["original"], applied, entry["mode"])
            except (OSError, InstallError):
                failed = True
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            raise
        if failed:
            raise InstallError("Revert failed; recovery needs owner attention in .sumbi/backups.") from None
        if isinstance(error, InstallError):
            raise
        raise InstallError("Revert failed; files restored to applied bytes.") from None
    finally:
        safe_path(root, ".sumbi/install.lock").unlink()
