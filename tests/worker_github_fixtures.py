"""Authored offline worker streams and recorded GitHub responses."""

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess

REPO = "example/sample"


def push_output(branch, repo=REPO):
    return f"To https://github.com/{repo}.git\n   abc1234..def5678 HEAD -> {branch}\n"


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8", newline="\n")


def stream(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8", newline="\n")


def at(day, seconds=0):
    return datetime(2030, 1, day, 1, tzinfo=timezone.utc) + timedelta(seconds=seconds)


def pull(number, day=1, seconds=100, head=None, merged=True, state="closed", checks="green"):
    sha = f"{number:040x}"
    finish = at(day, seconds).isoformat() if state == "closed" else None
    merge = finish if merged else None
    return {"response": {"number": number, "state": state, "created_at": at(day, 10).isoformat(),
        "closed_at": finish, "merged": merged, "merged_at": merge,
        "head": {"sha": sha, "ref": head or f"worker-{number}"},
        "merge_commit_sha": f"{number + 1000:040x}" if merged else None,
        "title": "Synthetic change", "body": ""}, "checks_at_merge": {
            "head_sha": sha, "captured_at": merge, "required": ["check"], "check_runs": [],
            "statuses": [{"context": "check", "sha": sha, "updated_at": merge,
                "state": "success" if checks == "green" else "failure"}]} if merged else None}


def recording(pulls, end="2030-02-01T00:00:00Z", commits=None):
    return {"repository": REPO, "coverage_start": "2030-01-01T00:00:00Z", "observed_at": end,
        "pulls_complete": True, "commits_complete": True, "pulls": pulls, "commits": commits or []}


def codex_worker(home, identity, cwd, number=None, *, day=1, cost=100, kind="exec", edit=True,
    command=None, output=None, context_branch=None, seconds=100, exit_code=0):
    source = {"subagent": {"thread_spawn": {"parent_thread_id": "main"}}} if kind == "subagent" else kind
    header = {"id": identity, "cwd": str(cwd), "source": source,
        "originator": "codex_exec" if kind == "exec" else "desktop", "cli_version": "1.0"}
    if context_branch:
        header["git"] = {"branch": context_branch}
    rows = [{"type": "session_meta", "timestamp": at(day).isoformat(), "payload": header},
        {"type": "turn_context", "timestamp": at(day, 1).isoformat(),
            "payload": {"cwd": str(cwd), "model": "synthetic-codex", "effort": "high"}}]
    if edit:
        rows.append({"type": "response_item", "timestamp": at(day, 2).isoformat(),
            "payload": {"type": "custom_tool_call", "call_id": "edit", "name": "apply_patch",
                "input": "synthetic edit"}})
    if command is None and number is not None:
        command = "git push -u origin worker-" + str(number)
        if output is None:
            output = push_output("worker-" + str(number))
    if command is not None:
        rows.append({"type": "response_item", "timestamp": at(day, 3).isoformat(), "payload": {
            "type": "function_call", "call_id": "shell", "name": "exec_command",
            "arguments": json.dumps({"cmd": command})}})
        rows.extend(execution(command, output, cwd, day=day, exit_code=exit_code))
    rows.extend([{"type": "event_msg", "timestamp": at(day, seconds).isoformat(), "payload": {
        "type": "token_count", "info": {"total_token_usage": {"input_tokens": cost - 10,
            "cached_input_tokens": 0, "output_tokens": 10, "reasoning_output_tokens": 0}}}},
        {"type": "event_msg", "timestamp": at(day, seconds).isoformat(),
            "payload": {"type": "task_complete"}}])
    stream(home / ".codex/sessions" / ("rollout-" + identity + ".jsonl"), rows)
    return rows


def execution(command, output, cwd, *, day=1, identity="shell", start=3, end=11,
    exit_code=0):
    """Native execution events, with response output mirrored as in a rollout."""
    return [{"type": "event_msg", "timestamp": at(day, start).isoformat(), "payload": {
        "type": "exec_command_begin", "call_id": identity, "command": command,
        "cwd": str(cwd)}},
        {"type": "event_msg", "timestamp": at(day, end).isoformat(), "payload": {
            "type": "exec_command_end", "call_id": identity, "exit_code": exit_code,
            "output": output}},
        {"type": "response_item", "timestamp": at(day, end).isoformat(), "payload": {
            "type": "function_call_output", "call_id": identity, "output": output}}]


def claude_worker(home, identity, cwd, number, *, day=1, cost=100, seconds=100):
    rows = [{"type": "user", "sessionId": "main", "agentId": identity, "cwd": str(cwd),
        "timestamp": at(day).isoformat(), "message": {"content": "Synthetic dispatch"}},
        {"type": "assistant", "sessionId": "main", "agentId": identity, "cwd": str(cwd),
            "timestamp": at(day, 2).isoformat(), "version": "1.0", "message": {
                "id": "edit", "model": "synthetic-claude", "content": [{"type": "tool_use",
                    "id": "edit", "name": "Edit", "input": {"file_path": "synthetic.py"}}]}},
        {"type": "assistant", "sessionId": "main", "agentId": identity, "cwd": str(cwd),
            "timestamp": at(day, seconds).isoformat(), "message": {"id": "finish",
                "model": "synthetic-claude", "usage": {"input_tokens": cost - 10,
                    "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
                    "output_tokens": 10}, "content": [{"type": "tool_use", "id": "push",
                        "name": "Bash", "input": {"command": f"git push origin worker-{number}"}}]}},
        {"type": "user", "sessionId": "main", "agentId": identity, "cwd": str(cwd),
            "timestamp": at(day, seconds).isoformat(), "toolUseResult": {
                "stdout": "", "stderr": push_output(f"worker-{number}"), "interrupted": False},
            "message": {"content": [
                    {"type": "tool_result", "tool_use_id": "push",
                        "content": push_output(f"worker-{number}"),
                        "is_error": False}]}}]
    stream(home / ".claude/projects/group/main/subagents" / ("agent-" + identity + ".jsonl"), rows)


def repository(path):
    path.mkdir(parents=True, exist_ok=True)
    for args in (["init", "-q", str(path)], ["-C", str(path), "remote", "add", "origin",
        "https://github.com/" + REPO + ".git"]):
        subprocess.run(["git", *args], capture_output=True, check=True)


def build_round(root, count=12):
    home, repo = root / "home", root / "repository"
    repository(repo)
    pulls = []
    for day, cost in ((1, 100), (8, 50)):
        for i in range(count):
            number = day * 100 + i
            identity = f"worker-{number}"
            if i % 3 == 0:
                claude_worker(home, identity, repo, number, day=day, cost=cost, seconds=cost)
            else:
                codex_worker(home, identity, repo, number, day=day, cost=cost, seconds=cost,
                    kind="subagent" if i % 2 else "exec")
            pulls.append(pull(number, day, cost))
        # The dispatcher opens a worker's PR; its own evidence cannot belong to a child.
        codex_worker(home, f"main-{day}", repo, day * 100, day=day, kind="vscode", edit=False,
            command=f"gh pr create --head worker-{day * 100}",
            output=f"https://github.com/{REPO}/pull/{day * 100}")
    fixture = root / "fixture"
    save(fixture / "outcomes/repository.json", recording(pulls))
    registration = {"outcome_source": "worker-github", "intervention_id": "synthetic-round",
        "applied_at": "2030-01-08T00:00:00Z", "registered_at": "2029-12-31T00:00:00Z",
        "predictions": [{"metric": "tokens_per_success", "direction": "decrease",
            "rough_size_percent": 20}], "non_inferiority_margin_pp": 30,
        "sample_size_per_arm": count, "follow_up_days": 7,
        "before": {"since": "2030-01-01T00:00:00Z", "until": "2030-01-08T00:00:00Z"},
        "after": {"since": "2030-01-08T00:00:00Z", "until": "2030-01-15T00:00:00Z"}}
    save(root / "registration.json", registration)
    return home, repo, fixture / "outcomes", root / "registration.json"
