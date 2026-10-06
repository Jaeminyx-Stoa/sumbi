"""Stable, evidence-bearing candidate rules; detection is not enforcement proof."""

from __future__ import annotations

from pathlib import PurePosixPath


def shared_target(source: str, agents: list[str]) -> str:
    """Share the nearest ancestor's AGENTS.md, including a scoped sibling."""
    directory = PurePosixPath(source).parent
    for parent in (directory, *directory.parents):
        candidate = (parent / "AGENTS.md").as_posix()
        if candidate in agents:
            return candidate
    return "AGENTS.md"


def has_import(report: dict, source: str, target: str) -> bool:
    pending, visited = [source], set()
    while pending:
        current = pending.pop()
        if current in visited:
            continue
        visited.add(current)
        for item in report["claude_imports"]:
            if item["source"] == current and item["status"] == "resolved":
                if item["path"] == target:
                    return True
                pending.append(item["path"])
    return False


def find_gaps(report: dict) -> list[dict]:
    gaps: list[dict] = []
    def add(identifier: str, evidence: dict, candidates: list[str]) -> None:
        gaps.append({"id": identifier, "evidence": evidence, "candidates": candidates})
    instructions = report["instructions"]
    if "AGENTS.md" not in instructions["agents"]["paths"]:
        add("missing-agents", {"paths": [], "expected_path": "AGENTS.md"}, ["instruction-map"])
    if instructions["claude"]["count"]:
        # The proposed AGENTS.md also makes a Claude-only repository shared.
        for path in instructions["claude"]["paths"]:
            target = shared_target(path, instructions["agents"]["paths"])
            if instructions["agents"]["count"] and not has_import(report, path, target):
                add("missing-shared-import", {"paths": [path, target]}, ["shared-instructions"])
                break
    for agent, cost in report["cost"]["instructions"].items():
        if cost["estimated_tokens"] > report["cost"]["budget"]:
            add("instruction-budget", {"agent": agent, "paths": cost["paths"], "estimated_tokens": cost["estimated_tokens"], "budget": report["cost"]["budget"]}, ["instruction-map"])
    if not report["enforcement"]["test_sources"]:
        add("missing-test-command", {"paths": []}, ["test-and-verify"])
    for key, identifier, practice in (
        ("review_gate", "missing-risk-review", "review-gates"),
        ("handoff", "missing-handoff", "handoff"),
        ("friction_line", "missing-friction-line", "friction-line"),
        ("parallel_worktree", "missing-worktree-rule", "parallel-worktrees"),
        ("plan_approval", "missing-plan-approval", "plan-and-approval"),
    ):
        if report["conventions"][key]["status"] == "absent":
            add(identifier, {"paths": [], "searched_paths": report["convention_search_paths"]}, [practice])
    return gaps
