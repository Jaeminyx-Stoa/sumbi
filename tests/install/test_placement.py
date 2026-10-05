"""Synthetic session starts across workspace and repository boundaries."""

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
from unittest.mock import patch

from sumbi.install.placement import annotate_placement, load_rules, observed_starts, read_starts
from sumbi.install.planner import build_plan
from sumbi.model import Session, Window
from .test_inventory import OfflineTest

NOW = datetime(2030, 1, 15, tzinfo=timezone.utc)


def session(agent, identifier, cwd):
    value = Session(agent, identifier)
    value.times.add(NOW - timedelta(days=1))
    value.cwd(str(cwd), NOW - timedelta(days=1))
    return value


class PlacementTests(OfflineTest):
    def workspace(self):
        root = self.copy_fixture("empty")
        for name in ("app-a", "app-b"):
            (root / name).mkdir()
            subprocess.run(["git", "init", "--quiet", str(root / name)], check=True, capture_output=True)
        (root / "notes").mkdir()
        return root

    def test_versioned_neutral_rules_and_qualified_claude_fallback(self):
        rules = load_rules()
        self.assertEqual(rules["version"], 1)
        rows = {r["agent"]: r for r in rules["agents"]}
        self.assertEqual(set(rows), {"codex", "claude-code", "gemini", "copilot", "cursor", "agents"})
        self.assertEqual(rows["agents"]["support"], "unverified")
        self.assertEqual(rows["claude-code"]["agents_fallback"]["minimum_version"], "2.1.277")
        self.assertIn("session-support", rows["claude-code"]["agents_fallback"]["conditions"])

    def test_distinct_agents_at_non_git_root_nested_repos_and_subfolders(self):
        root = self.workspace()
        (root / "AGENTS.md").write_text("Shared instructions.\n", encoding="utf-8")
        (root / "app-a/CLAUDE.md").write_text("Scoped instructions.\n", encoding="utf-8")
        plan = build_plan(root, select=["shared-instructions", "handoff"])
        sessions = [session("codex", "root-1", root), session("codex", "root-2", root),
                    session("claude-code", "nested-1", root / "app-a"),
                    session("codex", "nested-2", root / "app-b"),
                    session("claude-code", "other-1", root / "notes")]
        annotate_placement(plan, sessions)
        starts = plan.report["observed_starts"]
        self.assertEqual(starts["sessions"], 5)
        self.assertIn({"agent": "codex", "path": ".", "kind": "workspace-root", "repository": None, "count": 2, "roles": {"top-level": 2, "worker": 0}}, starts["paths"])
        self.assertIn({"agent": "claude-code", "path": "app-a", "kind": "nested-repository", "repository": "app-a", "count": 1, "roles": {"top-level": 1, "worker": 0}}, starts["paths"])
        self.assertIn({"agent": "claude-code", "path": "notes", "kind": "subfolder", "repository": None, "count": 1, "roles": {"top-level": 1, "worker": 0}}, starts["paths"])
        warning = next(w for w in plan.report["warnings"] if w["kind"] == "target-not-loaded" and w["agent"] == "codex" and w["target"] == "app-a/CLAUDE.md")
        self.assertEqual(warning["missed_starts"], 3)
        self.assertEqual(warning["recommended_paths"], [{"path": "AGENTS.md", "starts": 2}, {"path": "app-b/AGENTS.md", "starts": 1}])
        # The new launch import makes the shared file reachable for nested Claude.
        self.assertFalse(any(w["kind"] == "target-not-loaded" and w["agent"] == "claude-code" and w["target"] == "AGENTS.md" for w in plan.report["warnings"]))
        self.assertNotIn(str(root), json.dumps(plan.as_dict()))

    def test_git_boundary_non_git_cwd_override_and_strict_majority(self):
        root = self.workspace()
        (root / "app-a/child").mkdir()
        plan = build_plan(root, select=["handoff"])
        annotate_placement(plan, [session("codex", "a", root / "app-a/child")])
        warning = next(w for w in plan.report["warnings"] if w["kind"] == "target-not-loaded")
        self.assertEqual(warning["recommended_paths"], [{"path": "app-a/AGENTS.md", "starts": 1}])
        plan = build_plan(root, select=["handoff"])
        annotate_placement(plan, [session("codex", "a", root), session("codex", "b", root / "notes")])
        self.assertFalse(any(w["kind"] == "target-not-loaded" for w in plan.report["warnings"]))
        (root / "AGENTS.override.md").write_text("Override.\n", encoding="utf-8")
        plan = build_plan(root, select=["handoff"])
        annotate_placement(plan, [session("codex", "a", root)])
        self.assertTrue(any(w["kind"] == "target-not-loaded" for w in plan.report["warnings"]))
        warning = next(w for w in plan.report["warnings"] if w["kind"] == "target-not-loaded")
        self.assertEqual(warning["recommended_paths"], [{"path": "AGENTS.override.md", "starts": 1}])

    def test_start_is_fixed_and_duplicates_outside_ambiguous_missing_excluded_are_counts(self):
        root = self.workspace()
        plan = build_plan(root)
        first = session("codex", "first", root)
        first.cwd(str(root / "app-a"), NOW)
        other = session("codex", "outside", root.parent / "outside-private")
        ambiguous = session("codex", "ambiguous", root)
        ambiguous.cwd(str(root / "app-a"), NOW - timedelta(days=1))
        missing = Session("codex", "missing")
        excluded = session("codex", "excluded", root / ".tmp/private")
        report = observed_starts(root, plan.report, [first, first, other, ambiguous, missing, excluded])
        self.assertEqual(report["sessions"], 1)
        self.assertEqual(report["paths"][0]["path"], ".")
        self.assertEqual(report["coverage"]["duplicate_sessions"], 1)
        self.assertEqual(report["coverage"]["missing_or_ambiguous_cwd"], 2)
        self.assertEqual(report["coverage"]["outside_workspace"], 1)
        self.assertEqual(report["coverage"]["excluded_cwd"], 1)
        self.assertNotIn("private", json.dumps(report))

    def test_claude_agents_fallback_and_generic_support_are_unknown(self):
        root = self.workspace()
        plan = build_plan(root, select=["handoff"])
        annotate_placement(plan, [session("claude-code", "a", root / "app-a"), session("agents", "b", root)])
        self.assertTrue(any(w["kind"] == "target-load-unknown" for w in plan.report["warnings"]))
        self.assertTrue(any(w["kind"] == "load-rules-unverified" for w in plan.report["warnings"]))

    def test_registry_adapters_supply_launch_counts_and_window(self):
        root = self.workspace()
        home = Path.home()
        codex = home / ".codex/sessions/rollout-synthetic.jsonl"
        claude = home / ".claude/projects/synthetic/session.jsonl"
        for path, event in [(codex, {"type": "session_meta", "timestamp": "2030-01-14T00:00:00Z", "payload": {"id": "a", "cwd": str(root)}}),
                            (claude, {"type": "user", "timestamp": "2030-01-14T00:00:00Z", "sessionId": "b", "cwd": str(root / "app-a"), "message": {"content": "Synthetic private prompt."}})]:
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps(event) + "\n", encoding="utf-8")
        sessions, window, coverage = read_starts(home, now=NOW)
        plan = build_plan(root)
        annotate_placement(plan, sessions, window, coverage)
        self.assertEqual(plan.report["observed_starts"]["sessions"], 2)
        self.assertEqual(set(coverage), {"claude-code", "codex"})
        self.assertNotIn("Synthetic private prompt", json.dumps(plan.as_dict()))
        old = session("codex", "old", root)
        old.times = {NOW - timedelta(days=20), NOW - timedelta(days=1)}
        old.cwd_events = [(NOW - timedelta(days=20), str(root))]
        report = observed_starts(root, plan.report, [old], window)
        self.assertEqual(report["sessions"], 0)
        self.assertEqual(report["coverage"]["outside_window"], 1)

    def test_inherited_parent_time_does_not_replace_child_cwd_launch(self):
        root = self.workspace()
        home = Path.home()
        path = home / ".codex/sessions/rollout-child.jsonl"
        path.parent.mkdir(parents=True)
        events = [
            {"type": "session_meta", "timestamp": "2030-01-14T00:00:00Z", "payload": {"id": "child", "cwd": str(root)}},
            {"type": "session_meta", "timestamp": "2029-12-01T00:00:00Z", "payload": {"id": "parent", "cwd": str(root.parent)}},
        ]
        path.write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")
        sessions, window, _ = read_starts(home, now=NOW)
        plan = build_plan(root)
        report = observed_starts(root, plan.report, sessions, window)
        self.assertEqual(report["sessions"], 1)
        self.assertEqual(report["paths"][0]["path"], ".")

    def test_adapter_failure_is_degraded_coverage_and_inventory_remains_available(self):
        root = self.workspace()
        from sumbi.report import ADAPTERS
        with patch.object(ADAPTERS["codex"], "collect", side_effect=RuntimeError("Synthetic private failure")):
            sessions, window, coverage = read_starts(Path.home(), now=NOW)
        plan = build_plan(root)
        annotate_placement(plan, sessions, window, coverage)
        self.assertEqual(coverage["codex"]["status"], "unavailable")
        self.assertIn({"kind": "start-coverage-incomplete", "agent": "codex"}, plan.report["warnings"])
        self.assertNotIn("private failure", json.dumps(plan.as_dict()))

    def test_nested_repository_spelling_preserved_and_explicit_start_preferred(self):
        root = self.copy_fixture("empty")
        nested = root / "MixedCase"
        nested.mkdir()
        subprocess.run(["git", "init", "--quiet", str(nested)], check=True, capture_output=True)
        plan = build_plan(root)
        value = session("codex", "a", root)
        value.start_at = NOW - timedelta(days=1)
        value.start_cwd = str(nested)
        report = observed_starts(root, plan.report, [value])
        self.assertEqual(report["paths"][0]["repository"], "MixedCase")
        self.assertEqual(report["paths"][0]["path"], "MixedCase")

    def test_mixed_case_claude_launch_directory_loads_planned_import(self):
        root = self.copy_fixture("both")
        nested = root / "MixedDir"
        nested.mkdir()
        (nested / "CLAUDE.md").write_text("Scoped rules.\n", encoding="utf-8")
        plan = build_plan(root, select=["shared-instructions", "handoff"])
        annotate_placement(plan, [session("claude-code", "a", nested)])
        self.assertEqual(plan.report["observed_starts"]["paths"][0]["path"], "MixedDir")
        self.assertFalse(any(w["kind"] == "target-not-loaded" for w in plan.report["warnings"]))

    def test_ignored_root_and_nested_cwds_never_reach_paths_or_recommendations(self):
        root = self.workspace()
        subprocess.run(["git", "init", "--quiet", str(root)], check=True, capture_output=True)
        (root / ".gitignore").write_text("scratch/\n", encoding="utf-8")
        (root / "app-a/.gitignore").write_text("hidden/\n", encoding="utf-8")
        for relative in ("scratch/private-root", "app-a/hidden/private-nested"):
            (root / relative).mkdir(parents=True)
        plan = build_plan(root, select=["handoff"])
        annotate_placement(plan, [session("codex", "root-private", root / "scratch/private-root"),
            session("codex", "nested-private", root / "app-a/hidden/private-nested"),
            session("codex", "valid", root / "app-b")])
        starts = plan.report["observed_starts"]
        self.assertEqual(starts["sessions"], 1)
        self.assertEqual(starts["coverage"]["excluded_cwd"], 2)
        output = json.dumps(plan.as_dict())
        for private in ("scratch", "hidden", "private-root", "private-nested"):
            self.assertNotIn(private, output)
        warning = next(w for w in plan.report["warnings"] if w["kind"] == "target-not-loaded")
        self.assertEqual(warning["recommended_paths"], [{"path": "app-b/AGENTS.md", "starts": 1}])

    def test_deleted_and_file_cwds_are_not_exported(self):
        root = self.workspace()
        removed = root / "private-deleted"
        removed.mkdir()
        gone = session("codex", "gone", removed)
        removed.rmdir()
        regular = root / "private-file"
        regular.write_text("Synthetic data.", encoding="utf-8")
        plan = build_plan(root)
        starts = observed_starts(root, plan.report, [gone, session("codex", "file", regular)])
        self.assertEqual(starts["sessions"], 0)
        self.assertEqual(starts["coverage"]["missing_or_non_directory_cwd"], 2)
        self.assertNotIn("private", json.dumps(starts))

    def test_link_cwd_is_not_exported(self):
        root = self.workspace()
        plan = build_plan(root)
        link = root / "private-link"
        try:
            link.symlink_to(root / "notes", target_is_directory=True)
        except OSError:
            self.skipTest("Directory symlink creation is unavailable on this host.")
        starts = observed_starts(root, plan.report, [session("codex", "link", link)])
        self.assertEqual(starts["coverage"]["unsafe_cwd"], 1)
        self.assertNotIn("private", json.dumps(starts))

    def test_unavailable_ignore_check_fails_closed_without_private_paths(self):
        root = self.workspace()
        (root / "private-unknown").mkdir()
        plan = build_plan(root)
        with patch("sumbi.install.placement.GitIgnore") as factory:
            factory.return_value.available = False
            factory.return_value.excludes.return_value = False
            factory.return_value.check.return_value = False
            annotate_placement(plan, [session("codex", "unknown", root / "private-unknown")])
        self.assertEqual(plan.report["observed_starts"]["coverage"]["gitignore_unknown_cwd"], 1)
        self.assertIn({"kind": "start-coverage-incomplete"}, plan.report["warnings"])
        self.assertNotIn("private-unknown", json.dumps(plan.as_dict()))

    def test_worker_fanout_is_visible_but_inferred_cwds_do_not_vote(self):
        root = self.workspace()
        (root / "app-a/CLAUDE.md").write_text("Scoped rules.\n", encoding="utf-8")
        home = Path.home()
        parent = home / ".claude/projects/synthetic/parent.jsonl"
        parent.parent.mkdir(parents=True)
        paths = [parent] + [parent.parent / f"parent/subagents/agent-{n}.jsonl" for n in range(3)]
        for path in paths:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"type": "user", "sessionId": "parent",
                "timestamp": "2030-01-14T00:00:00Z", "cwd": str(root)}) + "\n", encoding="utf-8")
        sessions, window, _ = read_starts(home, now=NOW)
        sessions.extend([session("claude-code", "nested-a", root / "app-a"),
                         session("claude-code", "nested-b", root / "app-a")])
        # Newer adapters can fix a child's first observation without proving a header.
        for value in sessions:
            if value.parent_raw_id:
                value.start_at, value.start_cwd = NOW - timedelta(days=1), str(root)
                value.start_evidence = "first-observed-cwd"
        plan = build_plan(root, select=["handoff"])
        annotate_placement(plan, sessions, window)
        starts = plan.report["observed_starts"]
        self.assertEqual(starts["sessions"], 6)
        self.assertEqual(starts["placement_sessions"], 3)
        self.assertEqual(starts["roles"], {"top-level": 3, "worker": 3})
        self.assertEqual(starts["coverage"]["worker_start_unknown"], 3)
        self.assertFalse(any(w["kind"] == "target-not-loaded" and w["target"] == "app-a/CLAUDE.md"
            for w in plan.report["warnings"]))

    def test_own_header_worker_session_starts_are_counted_and_attributed(self):
        root = self.workspace()
        plan = build_plan(root, select=["handoff"])
        values = [session("codex", "parent", root / "app-a")]
        for index in range(3):
            value = session("codex", f"worker-{index}", root)
            value.parent_raw_id = "parent"
            value.start_at, value.start_cwd = NOW - timedelta(days=1), str(root)
            value.start_evidence = "session-header"
            values.append(value)
        unknown = session("codex", "metadata-less-child", root / "notes")
        unknown.parent_raw_id = "parent"
        values.append(unknown)
        annotate_placement(plan, values)
        starts = plan.report["observed_starts"]
        self.assertEqual(starts["sessions"], 5)
        self.assertEqual(starts["placement_sessions"], 4)
        self.assertEqual(starts["roles"], {"top-level": 1, "worker": 4})
        row = next(row for row in starts["paths"] if row["path"] == ".")
        self.assertEqual(row["roles"], {"top-level": 0, "worker": 3})
        self.assertEqual(starts["coverage"]["worker_start_unknown"], 1)
        self.assertFalse(any(w["kind"] == "target-not-loaded" and w["target"] == "AGENTS.md"
            for w in plan.report["warnings"]))
        self.assertFalse(any(row["path"] == "notes" for row in starts["paths"]))
        self.assertEqual(starts["agents"], [{"agent": "codex", "roles": {"top-level": 1, "worker": 4}, "placement_sessions": 4}])
