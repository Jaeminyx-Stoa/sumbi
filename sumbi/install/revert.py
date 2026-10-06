"""Hash-guarded file and block reversal with an append-only audit."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import stat

from .blocks import remove_blocks, validate_blocks
from .files import _checked, _write
from .errors import InstallError
from .files import checked_relative, read_bytes, safe_path
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
            blocks = validate_blocks(entry.get("blocks", []))
            if blocks:
                applied = (original or b"") + b"".join(
                    block["separator"].encode("ascii") + block["bytes"].encode("utf-8") for block in blocks)
                if digest(applied) != entry["applied_hash"]:
                    raise ValueError
            targets.append({**entry, "original": original, "blocks": blocks})
        if not targets:
            raise ValueError
        return targets
    except (TypeError, KeyError, ValueError, UnicodeError):
        raise InstallError("Backup manifest is unsupported or invalid; no files reverted.") from None


def _history(root: Path) -> tuple[bytes, int, list[dict]]:
    ledger = read_bytes(root, LEDGER)
    path = _checked(root, LEDGER, ledger)
    mode = stat.S_IMODE(path.stat().st_mode) if ledger is not None else 0o600
    try:
        if not ledger or not ledger.endswith(b"\n"):
            raise ValueError
        history = [json.loads(line, object_pairs_hook=_object) for line in ledger.splitlines()]
        if any(not isinstance(record, dict) for record in history):
            raise ValueError
    except (TypeError, ValueError, UnicodeError):
        raise InstallError("Intervention ledger is invalid; no files reverted.") from None
    return ledger, mode, history


def _normalize(results: list[dict]) -> list[dict]:
    return [{**entry,
             "status": "already-reverted" if entry["status"] in {"restored", "blocks-removed"} else entry["status"],
             **({"blocks": [{**block, "status": "already-reverted"}
                            if block["status"] == "removed" else block for block in entry["blocks"]]}
                if "blocks" in entry else {})} for entry in results]


def _block_results(entry: dict, status: str, reason: str | None = None) -> dict:
    return {"blocks": [{"id": block["id"], "status": status,
                        **({"reason": reason} if reason else {})} for block in entry["blocks"]]} if entry["blocks"] else {}


def _restore_entry(root: Path, entry: dict, finished: set[str], written: list) -> dict:
    path = entry["path"]
    current = read_bytes(root, path)
    target = _checked(root, path, current)
    mode = stat.S_IMODE(target.stat().st_mode) if current is not None else entry["mode"]
    if digest(current) == entry["applied_hash"]:
        after = entry["original"]
        if after is None:
            _checked(root, path, current).unlink()
        else:
            _write(root, path, current, after, entry["mode"])
        written.append((path, current, after, mode))
        return {"path": path, "status": "restored", **_block_results(entry, "removed")}
    if current is None or entry["absent"] or not entry["blocks"]:
        return {"path": path, "status": "refused", "reason": "content-changed",
                **_block_results(entry, "refused", "content-changed")}
    after, blocks = remove_blocks(current, entry["blocks"], finished)
    if after != current:
        _write(root, path, current, after, mode)
        written.append((path, current, after, mode))
    refused = any(block["status"] == "refused" for block in blocks)
    return {"path": path, "status": "refused" if refused else "blocks-removed", "blocks": blocks,
            **({"reason": "block-content-changed"} if refused else {})}


def _restore_targets(root: Path, targets: list[dict], prior: list[dict], written: list) -> list[dict]:
    files = [entry for record in prior for entry in record.get("files", []) if isinstance(entry, dict)]
    finished = {entry["path"] for entry in files
                if entry.get("status") in {"restored", "blocks-removed", "already-reverted"}}
    results = []
    for entry in targets:
        path = entry["path"]
        if path in finished:
            results.append({"path": path, "status": "already-reverted", **_block_results(entry, "already-reverted")})
            continue
        blocks = {block["id"] for item in files if item.get("path") == path
                  for block in item.get("blocks", []) if isinstance(block, dict)
                  and block.get("status") in {"removed", "already-reverted"}}
        try:
            results.append(_restore_entry(root, entry, blocks, written))
        except (OSError, InstallError):
            results.append({"path": path, "status": "refused", "reason": "unsafe-or-unavailable",
                            **_block_results(entry, "refused", "unsafe-or-unavailable")})
    return results


def _rollback(root: Path, written: list) -> bool:
    failed = False
    for path, before, after, mode in reversed(written):
        try:
            _write(root, path, after, before, mode)
        except (OSError, InstallError):
            failed = True
    return failed


def revert_install(repository: Path | str, backup_id: str) -> dict:
    """Restore unchanged files or remove unchanged blocks from edited existing files."""
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
    written = []
    try:
        targets = _manifest(root, backup_id)
        ledger, mode, history = _history(root)
        prior = [record for record in history if record.get("action") == "revert" and record.get("backup_id") == backup_id]
        results = _restore_targets(root, targets, prior, written)
        if _normalize(results) != (_normalize(prior[-1].get("files", [])) if prior else None):
            record = {"action": "revert", "backup_id": backup_id, "files": results,
                      "utc_time": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")}
            _write(root, LEDGER, ledger, ledger + (json.dumps(record, sort_keys=True) + "\n").encode(), mode)
        return {"backup_id": backup_id, "files": results,
                "refused": sum(entry["status"] == "refused" for entry in results)}
    except BaseException as error:
        failed = _rollback(root, written)
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            raise
        if failed:
            raise InstallError("Revert failed; recovery needs owner attention in .sumbi/backups.") from None
        if isinstance(error, InstallError):
            raise
        raise InstallError("Revert failed; files restored to pre-revert bytes.") from None
    finally:
        safe_path(root, ".sumbi/install.lock").unlink()
