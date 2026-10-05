"""Locked, backed-up, additive updates with stale-plan checks and rollback."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import stat
import tempfile

from sumbi.catalog import VERSION, load_judgment_policy
from .baseline import baseline
from .errors import InstallError
from .inventory import read_bytes, safe_path
from .planner import Plan, build_plan, digest


def _checked(root: Path, relative: str, expected: bytes | None) -> Path:
    path = safe_path(root, relative)
    if path.exists() and (not path.is_file() or path.stat().st_nlink != 1):
        raise InstallError("Apply requires single-link regular files.")
    if read_bytes(root, relative) != expected:
        raise InstallError("Repository changed since planning; rebuild the plan.")
    return path


def _write(root: Path, relative: str, expected: bytes | None,
           data: bytes, mode: int) -> None:
    """Stage bytes in the destination directory, recheck, then replace."""
    path = _checked(root, relative, expected)
    path.parent.mkdir(parents=True, exist_ok=True)
    safe_path(root, relative)
    descriptor, temporary = tempfile.mkstemp(prefix=".sumbi-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        _checked(root, relative, expected)
        if expected is None:
            # A same-directory hard link publishes staged bytes exclusively.
            # Unlike replace, it cannot overwrite an unanticipated new file.
            os.link(temporary, path)
        else:
            os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


LOCAL_IGNORES = b"backups/\nbaseline/\ninstall.lock\n"


def _protect_local_artifacts(root: Path) -> None:
    """Keep privacy protection even if apply later fails and leaves backups."""
    relative = ".sumbi/.gitignore"
    before = read_bytes(root, relative)
    if before is not None and before.endswith(LOCAL_IGNORES):
        _checked(root, relative, before)
        return
    data = before or b""
    if data and not data.endswith(b"\n"):
        data += b"\n"
    mode = stat.S_IMODE(safe_path(root, relative).stat().st_mode) if before is not None else 0o644
    _write(root, relative, before, data + LOCAL_IGNORES, mode)


def apply_plan(plan: Plan, *, home: Path | None = None, salt: bytes | None = None) -> dict:
    if not plan.changes:
        return {"applied": [], "baseline": {"status": "not requested (empty plan)"}}
    root = plan.root
    local = safe_path(root, ".sumbi")
    local.mkdir(mode=0o700, exist_ok=True)
    lock = safe_path(root, ".sumbi/install.lock")
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise InstallError("Another install is active or a recovery lock remains.") from None
    os.close(descriptor)
    originals: dict[str, bytes | None] = {}
    modes = {}
    written: list[tuple[str, bytes]] = []
    created_dirs: set[Path] = set()
    backup_relative = None
    try:
        # The public Plan object contains mutable lists. Rebuild from the trusted
        # catalog so a modified or hand-crafted object cannot bypass additions.
        fresh = build_plan(root, budget=plan.report["cost"]["budget"],
                           select=[p["id"] for p in plan.practices], exclude=plan.exclude)
        if fresh.changes != plan.changes or fresh.practices != plan.practices:
            raise InstallError("Plan no longer matches the repository and bundled catalog.")
        for change in plan.changes:
            path = _checked(root, change.path, change.before)
            originals[change.path] = change.before
            modes[change.path] = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o644
        ledger_path = ".sumbi/interventions.jsonl"
        ledger = read_bytes(root, ledger_path)
        path = _checked(root, ledger_path, ledger)
        if ledger and not ledger.endswith(b"\n"):
            raise InstallError("Intervention ledger lacks a final newline.")
        if ledger:
            try:
                if any(not isinstance(json.loads(line), dict) for line in ledger.splitlines()):
                    raise ValueError
            except (ValueError, UnicodeError):
                raise InstallError("Intervention ledger is not valid JSON lines.") from None
        originals[ledger_path] = ledger
        modes[ledger_path] = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600
        _protect_local_artifacts(root)
        baseline_result = baseline(root, run=True, home=home, salt=salt)
        now = datetime.now(timezone.utc)
        utc = now.isoformat(timespec="microseconds").replace("+00:00", "Z")
        backup_relative = ".sumbi/backups/" + now.strftime("%Y%m%dT%H%M%S.%fZ")
        backup = safe_path(root, backup_relative)
        backup.mkdir(parents=True, mode=0o700, exist_ok=False)
        records = [{"practice_id": p["id"], "catalog_version": VERSION,
                    "content_hash": p["content_hash"], "files": p["files"],
                    "prediction": p["prediction"], "judgment": p["judgment"],
                    "provenance": p["provenance"], "utc_time": utc,
                    "baseline": baseline_result, "judgment_policy": load_judgment_policy()} for p in plan.practices]
        # Register predictions in the recovery manifest before changing targets.
        applied = {c.path: c.after for c in plan.changes}
        manifest = {"version": 1, "status": "prepared", "records": records,
                    "files": [{"path": name, "before_hash": digest(data), "mode": modes[name],
                               "applied_hash": digest(applied.get(name)) if name in applied else None,
                               "absent": data is None} for name, data in originals.items()]}
        for name, data in originals.items():
            destination = safe_path(root, backup_relative + "/files/" + name)
            if data is not None:
                destination.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
                with destination.open("xb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.chmod(destination, 0o600)
            # New files have no bytes to back up; the manifest records absence.
        manifest_path = safe_path(root, backup_relative + "/manifest.json")
        with manifest_path.open("x", encoding="utf-8") as stream:
            json.dump(manifest, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(manifest_path, 0o600)
        updates = [(c.path, c.after) for c in plan.changes]
        updates.append((ledger_path, (ledger or b"") + b"".join(
            (json.dumps(record, sort_keys=True) + "\n").encode() for record in records)))
        # Check the whole transaction after the baseline and backups too.
        for name, original in originals.items():
            _checked(root, name, original)
        for name, data in updates:
            parent = safe_path(root, name).parent
            while not parent.exists() and parent != root:
                created_dirs.add(parent)
                parent = parent.parent
            _write(root, name, originals[name], data, modes[name])
            written.append((name, data))
        completed = {**manifest, "status": "applied"}
        relative_manifest = backup_relative + "/manifest.json"
        _write(root, relative_manifest, read_bytes(root, relative_manifest),
               (json.dumps(completed, indent=2) + "\n").encode(), 0o600)
        return {"applied": [p["id"] for p in plan.practices],
                "baseline": baseline_result, "backup": backup_relative,
                "backup_id": backup_relative.rsplit("/", 1)[1]}
    except BaseException as error:
        rollback_failed = False
        for name, data in reversed(written):
            try:
                if originals[name] is None:
                    _checked(root, name, data).unlink()
                else:
                    _write(root, name, data, originals[name], modes[name])
            except (OSError, InstallError):
                rollback_failed = True
        for directory in sorted(created_dirs, key=lambda p: len(p.parts), reverse=True):
            try:
                directory.rmdir()
            except OSError:
                pass
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            raise
        if rollback_failed:
            raise InstallError("Apply failed; recovery needs owner attention in .sumbi/backups.") from None
        if isinstance(error, InstallError):
            raise
        raise InstallError("Apply failed; prior files were restored; backups remain if created.") from None
    finally:
        safe_path(root, ".sumbi/install.lock").unlink()
