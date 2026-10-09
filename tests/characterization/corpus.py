"""Run real CLI functions against private copies of authored fixtures."""

from contextlib import ExitStack, contextmanager, redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
from unittest.mock import patch

from sumbi.cli import main
from support import IsolatedTemporaryDirectory
from worker_github_fixtures import build_round
from .normalizers import Normalizer

DIRECTORY = Path(__file__).resolve().parent
TESTS = DIRECTORY.parent
MANIFEST = json.loads((DIRECTORY / "manifest.json").read_text(encoding="utf-8"))
CASES = MANIFEST["cases"]


def copy_text_tree(source, destination):
    """Materialize the same authored LF inputs on both operating systems."""
    destination.mkdir(parents=True, exist_ok=True)
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source)
        target = destination / relative
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif path.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(path.read_text(encoding="utf-8").encode("utf-8"))


def repository(path):
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(path), "remote", "add", "origin",
                    "https://example.test/corpus/sample.git"], check=True, capture_output=True)


def save(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


def prepare(root, case):
    home, repo, fixture = root / "home", root / "repository", root / "fixture"
    home.mkdir()
    save(root / "empty-salt", "")
    save(root / "invalid-ledger.csv", "invalid\n")
    save(root / "invalid-registration.json", "{}\n")
    registration = root / "registration.json"
    setup = case["setup"]
    if setup.startswith("install:"):
        copy_text_tree(TESTS / "install/fixtures" / setup.split(":", 1)[1], repo)
        if "install_config" in case:
            save(repo / ".sumbi/config.toml", case["install_config"])
        if "install_guidance" in case:
            save(repo / "AGENTS.md", case["install_guidance"])
        if "install_gitignore" in case:
            repository(repo)
            save(repo / ".gitignore", case["install_gitignore"])
    elif setup in ("deliver", "compare"):
        copy_text_tree(TESTS / "fixtures" / setup, fixture)
        home = fixture / "home"
        if setup == "compare":
            # Include both native adapters, with the optional Claude fixture
            # exercised by additional manifest cases when explicitly selected.
            copy_text_tree(fixture / "claude", home / ".claude/projects/fixture")
            save(fixture / "interventions.jsonl", json.dumps({
                "intervention_id": "round-01", "practice_id": "handoff",
                "utc_time": "2030-01-08T00:01:00Z"}) + "\n")
    elif setup == "native":
        repository(repo)
        for source, target in (
            ("claude/main.jsonl", ".claude/projects/fixture/main.jsonl"),
            ("claude/child.jsonl", ".claude/projects/fixture/main/subagents/child.jsonl"),
            ("codex/main.jsonl", ".codex/sessions/rollout-main.jsonl"),
            ("codex/resumed.jsonl", ".codex/sessions/rollout-resumed.jsonl"),
        ):
            text = (TESTS / "fixtures" / source).read_text(encoding="utf-8")
            save(home / target, text.replace("fixture-one", json.dumps(str(repo))[1:-1]))
    elif setup == "events" or setup.startswith("local-native"):
        repository(repo)
        if setup == "events":
            for name in ("valid.jsonl", "resumed.jsonl"):
                text = (TESTS / "fixtures/open_events" / name).read_text(encoding="utf-8")
                save(home / ".sumbi/events" / name,
                     text.replace("/fixture/workspace", json.dumps(str(repo))[1:-1]))
            # Replay an independent after arm from the same authored stream.
            rows = [json.loads(line) for line in (home / ".sumbi/events/valid.jsonl").read_text().splitlines()]
            for row in rows:
                for field in ("event_id", "session_id", "parent_session_id", "tool_call_id"):
                    if field in row:
                        row[field] = "after-" + row[field]
                for field in ("timestamp", "started_at"):
                    if field in row:
                        row[field] = row[field].replace("2030-01-01", "2030-01-02")
            save(home / ".sumbi/events/after.jsonl", "".join(json.dumps(r) + "\n" for r in rows))
        else:
            copy_text_tree(DIRECTORY / "fixtures/local-native", home)
            if setup == "local-native-claude":
                copy_text_tree(DIRECTORY / "fixtures/local-claude", home)
            for path in home.rglob("*.jsonl"):
                text = path.read_text(encoding="utf-8")
                save(path, text.replace("fixture-repository", json.dumps(str(repo))[1:-1]))
                if setup == "local-native-failed-verifier":
                    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
                    for row in rows:
                        item = row.get("payload", {}).get("item", {})
                        if "exit_code" in item:
                            item["exit_code"] = 9
                    save(path, "".join(json.dumps(row) + "\n" for row in rows))
        data = json.loads((DIRECTORY / "fixtures/local-registration.json").read_text(encoding="utf-8"))
        save(registration, json.dumps(data, indent=2) + "\n")
        save(fixture / "interventions.jsonl", json.dumps({
            "intervention_id": "synthetic-local-round", "practice_id": "handoff",
            "utc_time": "2030-01-02T01:00:01Z"}) + "\n")
    elif setup == "improve":
        copy_text_tree(DIRECTORY / "fixtures/improve", fixture)
    elif setup == "register-existing":
        save(root / "published-registration.json", '{"existing": "synthetic sentinel"}\n')
    elif setup.startswith("worker-github"):
        build_round(root)
        if setup == "worker-github-withhold":
            data = json.loads(registration.read_text(encoding="utf-8"))
            data["sample_size_per_arm"] = 13
            save(registration, json.dumps(data, indent=2) + "\n")
    return {"home": str(home), "repository": str(repo), "fixture": str(fixture),
            "registration": str(registration)}


@contextmanager
def offline_environment(root):
    old_cwd = Path.cwd()
    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, {
            "SUMBI_SALT": MANIFEST["salt"], "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
        }))
        stack.enter_context(patch.object(Path, "home", return_value=root / "home"))
        for target in ("socket.socket", "socket.create_connection", "socket.getaddrinfo",
                       "urllib.request.OpenerDirector.open"):
            stack.enter_context(patch(target, side_effect=AssertionError("Corpus cannot use the network")))
        popen = subprocess.Popen

        def local_git_only(argv, *args, **kwargs):
            if not isinstance(argv, (list, tuple)) or not argv or argv[0] != "git":
                raise AssertionError("Corpus subprocesses must be local Git probes")
            command = list(argv[1:])
            while command[:1] in (["-C"], ["-c"]):
                command = command[2:]
            if not command or command[0] not in ("init", "remote", "rev-parse", "config", "check-ignore", "ls-files"):
                raise AssertionError("Corpus cannot run a network Git command")
            if command[0] == "remote" and command[1:2] != ["add"]:
                raise AssertionError("Corpus can only configure a synthetic origin")
            return popen(argv, *args, **kwargs)

        stack.enter_context(patch("subprocess.Popen", side_effect=local_git_only))
        os.chdir(root)
        try:
            yield
        finally:
            os.chdir(old_cwd)


def invoke(argv):
    output, errors = io.StringIO(), io.StringIO()
    with redirect_stdout(output), redirect_stderr(errors):
        try:
            code = main(argv)
        except SystemExit as exc:
            code = exc.code
    if not isinstance(code, int):
        raise AssertionError("CLI exit status must be an integer")
    return {"stdout.txt": output.getvalue(), "stderr.txt": errors.getvalue(),
            "exit-code.txt": str(code) + "\n"}


def register_variables(root, streams, normalizer):
    """Register clock fields only from successful install output/artifacts."""
    for line in streams["stdout.txt"].splitlines():
        if line.startswith("  observed starts: "):
            normalizer.starts({"observed_starts": json.loads(line.split(": ", 1)[1])})
        if line.startswith("Backup ID: "):
            normalizer.add(line.removeprefix("Backup ID: "), "__BACKUP_ID__")
    repo = root / "repository"
    plan = repo / "plan.json"
    if plan.exists():
        normalizer.starts(json.loads(plan.read_text(encoding="utf-8"))["inventory"])
    for path in (repo / ".sumbi/baseline").glob("*.json"):
        normalizer.baseline(path.relative_to(repo).as_posix(), json.loads(path.read_text(encoding="utf-8")))
    ledger = repo / ".sumbi/interventions.jsonl"
    if ledger.exists():
        normalizer.ledger([json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()])
    registration = root / "published-registration.json"
    if registration.exists():
        data = json.loads(registration.read_text(encoding="utf-8"))
        if "registered_at" in data:
            normalizer.add(data["registered_at"], "<REGISTERED_AT>")


def capture_artifacts(root, normalizer):
    artifacts = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        # These are input trees, never CLI output files.
        if relative.startswith(("home/", "fixture/")) or ".git" in path.relative_to(root).parts:
            continue
        if relative in ("empty-salt", "invalid-ledger.csv", "invalid-registration.json", "registration.json"):
            continue
        if relative.startswith("repository/") and relative != "repository/plan.json":
            continue
        # read_text translates the OS text-file newline convention to LF.
        text = path.read_text(encoding="utf-8")
        if path.suffix == ".json":
            json.loads(text)  # Invalid JSON output is always a test failure.
        artifacts["files/" + relative] = normalizer.text(text)
    return artifacts


def snapshot_tree(repo, normalizer):
    """Pin the complete tree and content, leaving private recovery modes local.

    Recovery manifests are listed, but their content is outside the public
    output contract (POSIX modes differ from Windows). Original backup bytes,
    applied files, the ledger and baseline JSON are captured separately.
    """
    tree, artifacts = [], {}
    # Define the ordering of this test-authored tree explicitly. Path ordering
    # itself is case-insensitive on Windows and case-sensitive on POSIX.
    for path in sorted(repo.rglob("*"), key=lambda p: tuple(part.casefold() for part in p.relative_to(repo).parts)):
        relative = path.relative_to(repo).as_posix()
        if ".git" in path.relative_to(repo).parts:
            continue
        name = normalizer.text(relative)
        tree.append({"path": name, "kind": "directory" if path.is_dir() else "file"})
        if path.is_file() and not (relative.startswith(".sumbi/backups/") and path.name == "manifest.json"):
            text = normalizer.text(path.read_text(encoding="utf-8"))
            # A golden .gitignore must not become an active ignore policy for
            # sibling golden artifacts (including baseline and backup files).
            storage_name = name + ".txt" if path.name == ".gitignore" else name
            artifacts["tree-files/" + storage_name] = text
    artifacts["tree.json"] = json.dumps(tree, indent=2) + "\n"
    return artifacts


def run_case(case):
    with IsolatedTemporaryDirectory(prefix="sumbi-corpus-") as temporary:
        root = Path(temporary).resolve()
        normalizer = Normalizer(root)
        with offline_environment(root):
            substitutions = prepare(root, case)
            argv = [part.format_map(substitutions) for part in case["argv"]]
            streams = invoke(argv)
            code = int(streams["exit-code.txt"])
            if code != case["expected_exit"]:
                raise AssertionError(case["name"] + ": unexpected exit status " + str(code) + "\n" + streams["stderr.txt"])
            register_variables(root, streams, normalizer)
            result = {name: normalizer.text(text) for name, text in streams.items()}
            if case.get("lifecycle"):
                repo = Path(substitutions["repository"])
                result.update({"apply/" + name: value for name, value in snapshot_tree(repo, normalizer).items()})
                backup = next((line.removeprefix("Backup ID: ") for line in streams["stdout.txt"].splitlines()
                               if line.startswith("Backup ID: ")), None)
                if backup:
                    if "revert_edit" in case:
                        edit = case["revert_edit"]
                        target = repo / edit["path"]
                        target.write_bytes(target.read_bytes() + edit["append"].encode("utf-8"))
                    reverted = invoke(["install", "--root", str(repo), "--revert", backup])
                    if reverted["exit-code.txt"] != "0\n":
                        raise AssertionError("Revert failed: " + reverted["stderr.txt"])
                    register_variables(root, reverted, normalizer)
                    result.update({"revert/" + name: normalizer.text(value) for name, value in reverted.items()})
                    result.update({"revert/" + name: value for name, value in snapshot_tree(repo, normalizer).items()})
                else:
                    # An already configured fixture has no changes or backup.
                    reverted = invoke(["install", "--root", str(repo), "--revert", "20300115T000000.000000Z"])
                    if reverted["exit-code.txt"] != "1\n":
                        raise AssertionError("Empty-plan revert must explain missing metadata")
                    result.update({"revert/" + name: value for name, value in reverted.items()})
                    result.update({"revert/" + name: value for name, value in snapshot_tree(repo, normalizer).items()})
            result.update(capture_artifacts(root, normalizer))
            if argv and argv[0] in ("collect", "deliver", "compare") and code == 0 and "-" in argv:
                json.loads(result["stdout.txt"])
                result["stdout.json"] = result["stdout.txt"]
            return result


def run_corpus():
    return {case["name"]: run_case(case) for case in CASES}
