"""Synthetic per-role profile: roles, long tail, decile, fan-out, models, tool latency."""

from datetime import datetime, timedelta, timezone
import json
import unittest

from sumbi.core.time import Window
from sumbi.measure import hook_denials
from sumbi.measure.profile import measure, tail_lines, text_lines, tool_class
from sumbi.sessions.session import Session

T0 = datetime(2030, 1, 1, tzinfo=timezone.utc)
WINDOW = Window(T0, T0 + timedelta(days=2))
KINDS = ("new_input", "cache_write", "cache_read", "output", "reasoning_output")


def tokens(total):
    return {"new_input": total // 2, "cache_write": 0, "cache_read": total - total // 2,
        "output": 0, "reasoning_output": None, "total": total}


def link(key="project_a", bucket="project", rule=None):
    return {"bucket": bucket, "project_key": key, "rule": rule}


def make(raw, *, agent="claude-code", parent=None, worker=False, total=100, active=60.0,
    calls=10, errors=1, compactions=0, models=("model-a",), allocations=None,
    project=None, calls_seen=()):
    session = Session(agent, raw, parent_raw_id=parent, is_worker=worker)
    session.hook_calls.observations.extend(calls_seen)
    row = {"id": session.id(), "parent_id": parent and Session(agent, parent).id(),
        "agent": agent, "project": project or link(), "models": list(models),
        "allocations": [{**link(), "tokens": tokens(total)}] if allocations is None
            else allocations,
        "tokens": tokens(total),
        "counts": {"tool_calls": calls, "tool_results": calls, "tool_errors": errors,
            "compactions": compactions},
        "time": {"active": {"seconds": active}, "durations": {"tool": {"seconds": calls * 2.0,
            "paired_intervals": calls}}}}
    return session, row


def call(name, start_minutes, seconds, *, paired=True):
    at = T0 + timedelta(minutes=start_minutes)
    result = (at + timedelta(seconds=seconds), (1, 0), True) if paired else None
    return (at, (0, 0), name, result, [])


def run(pairs, **options):
    found = [s for s, _ in pairs]
    return measure(found, {r["id"]: r for _, r in pairs}, WINDOW, **options)


class ProfileTests(unittest.TestCase):
    def test_roles_follow_parent_header_or_explicit_open_role(self):
        pairs = [make("root"), make("child", parent="root"),
            make("header-worker", worker=True),
            make("open-orchestrator", agent="sumbi-events", parent="x", worker=False),
            make("open-worker", agent="sumbi-events", worker=True)]
        roles = run(pairs)["summary"]["roles"]
        self.assertEqual(roles["orchestrator"]["sessions"], 2)
        self.assertEqual(roles["worker"]["sessions"], 3)
        per_agent = run(pairs)["by_agent"]["sumbi-events"]["roles"]
        self.assertEqual((per_agent["orchestrator"]["sessions"], per_agent["worker"]["sessions"]),
            (1, 1))

    def test_per_day_values_divide_by_window_length(self):
        role = run([make("a", total=400, calls=8, errors=2, compactions=2, active=7200)]
            )["summary"]["roles"]["orchestrator"]
        self.assertEqual(role["sessions_per_day"], 0.5)
        self.assertEqual(role["tokens_per_day"]["total"], 200)
        self.assertIsNone(role["tokens_per_day"]["reasoning_output"])
        self.assertEqual((role["tool_calls_per_day"], role["compactions_per_day"]), (4, 1))
        self.assertEqual(role["tool_error_rate"], 0.25)
        self.assertEqual(role["active_seconds_per_day"], 3600)
        self.assertEqual(role["cache_read_per_tool_call"], 25)
        self.assertEqual(role["tool_seconds_per_paired_call"], 2)

    def test_long_tail_threshold_is_strict_and_configurable(self):
        pairs = [make("a", total=600, active=3601), make("b", total=300, active=3600),
            make("c", total=100, active=1900)]
        tail = run(pairs)["summary"]["roles"]["orchestrator"]["long_tail"]
        self.assertEqual((tail["sessions"], tail["total_tokens"], tail["token_share"]),
            (1, 600, 0.6))
        tail = run(pairs, long_session_minutes=30)["summary"]["roles"]["orchestrator"]
        self.assertEqual(tail["long_tail"]["sessions"], 3)
        self.assertEqual(run(pairs, long_session_minutes=30)["long_session_minutes"], 30)

    def test_top_decile_uses_ceiling_rank(self):
        pairs = [make(f"s{i}", total=10 * (i + 1)) for i in range(11)]
        top = run(pairs)["summary"]["roles"]["orchestrator"]["concentration"]
        self.assertEqual(top["top_decile_sessions"], 2)  # ceil(11 / 10)
        self.assertEqual(top["top_decile_total_tokens"], 110 + 100)
        self.assertAlmostEqual(top["top_decile_share"], 210 / 660)

    def test_fan_out_counts_children_per_parent(self):
        pairs = [make("p1"), make("p2"), *(make(f"a{i}", parent="p1") for i in range(3)),
            make("b0", parent="p2"), make("orphan", parent="gone")]
        fan = run(pairs)["summary"]["fan_out"]
        self.assertEqual(fan, {"parents": 3, "children": 5, "children_max": 3,
            "children_median": 1})
        self.assertEqual(run([make("solo")])["summary"]["fan_out"],
            {"parents": 0, "children": 0, "children_max": None, "children_median": None})

    def test_model_label_shares(self):
        pairs = [make("a", total=300, models=("m-big",)), make("b", total=100, models=("m-small",)),
            make("c", total=0, models=())]
        models = run(pairs)["summary"]["roles"]["orchestrator"]["models"]
        self.assertEqual([(m["models"], m["sessions"], m["token_share"]) for m in models],
            [(["m-big"], 1, 0.75), (["m-small"], 1, 0.25), ([], 1, 0.0)])

    def test_model_rows_beyond_five_are_folded_visibly(self):
        pairs = [make(f"s{i}", total=100 - i, models=(f"m-{i}",)) for i in range(7)]
        text = "\n".join(text_lines(run(pairs)))
        line = next(l for l in text.splitlines() if l.startswith("  Models:"))
        self.assertIn("m-4 sessions 1", line)
        self.assertNotIn("m-5 ", line)
        self.assertIn("2 more model sets sessions 2 token share " + format(
            (95 + 94) / sum(range(94, 101)), ".6g"), line)

    def test_project_without_usage_keeps_its_rule_label(self):
        pairs = [make("idle", total=0, allocations=[],
            project=link("project_a", rule="sample"))]
        project = run(pairs)["projects"]["project_a"]
        self.assertEqual(project["rules"], ["sample"])
        self.assertIn("Profile project sample project_a orchestrator",
            "\n".join(text_lines(run(pairs))))

    def test_tool_classes_and_quantiles(self):
        self.assertEqual([tool_class(n) for n in ("Read", "Edit", "Grep", "Bash",
            "functions.exec_command", "Task", "WebFetch", "mcp__srv__do", "TodoWrite", None)],
            ["read", "edit", "search", "shell", "shell", "agent", "web", "mcp", "other",
                "unnamed"])
        seen = [call("Read", 1, s) for s in (0.1, 0.2, 0.3, 0.4, 5.0)]
        seen += [call("Bash", 2, 9.0, paired=False), call("Read", -5, 99.0),
            call("mcp__x__y", 3, 2.0)]
        latency = run([make("a", calls_seen=seen)])["summary"]["roles"]["orchestrator"][
            "tool_latency"]
        self.assertEqual(latency["read"], {"paired_calls": 5, "median_seconds": 0.3,
            "p90_seconds": 5.0})
        self.assertEqual(latency["shell"]["paired_calls"], 0)
        self.assertIsNone(latency["shell"]["median_seconds"])
        self.assertEqual(latency["mcp"]["paired_calls"], 1)

    def test_projects_use_allocated_tokens_and_bucket_refs(self):
        split = [{**link("project_a"), "tokens": tokens(70)},
            {**link(None, "other"), "tokens": tokens(30)}]
        pairs = [make("mixed", total=100, allocations=split),
            make("idle", total=0, allocations=[], project=link(None, "unassigned"))]
        projects = run(pairs)["projects"]
        self.assertEqual(sorted(projects), ["other", "project_a", "unassigned"])
        a = projects["project_a"]
        self.assertEqual(a["roles"]["orchestrator"]["tokens"]["total"], 70)
        self.assertEqual(a["agents"]["claude-code"]["roles"]["orchestrator"]["tokens"]["total"],
            70)
        self.assertEqual(projects["other"]["roles"]["orchestrator"]["counts"]["tool_calls"], 10)

    def test_text_names_classes_not_tools_and_tail_recap(self):
        pairs = [make("a", calls_seen=[call("SecretToolName", 1, 1.0), call("Read", 2, 0.5)]),
            make("w", parent="a")]
        profile = run(pairs)
        text = "\n".join(text_lines(profile))
        self.assertIn("Profile all worker: sessions 1", text)
        self.assertIn("Tool latency orchestrator (median/p90 seconds): read 0.5/0.5 n 1; "
            "other 1/1 n 1", text)
        report = {"window": {"since": "2030-01-01T00:00:00Z", "until": "2030-01-03T00:00:00Z"},
            "summary": {"sessions": 2, "tokens": {"total": 200}}}
        tail = tail_lines(report, profile)
        self.assertTrue(tail[0].startswith("Key summary: window"))
        self.assertIn("  worker: sessions 1;", tail[2])
        self.assertNotIn("SecretToolName", text + json.dumps(profile) + "\n".join(tail))

    def test_quiet_hook_denial_projects_fold_to_one_line(self):
        quiet = {"denials": 0, "timeouts": 0}
        group = {"sessions_with_denials": 1, "denials": 2, "timeouts": 0,
            "denials_per_100_tool_calls": 1.0, "timeouts_per_100_tool_calls": 0.0,
            "inner_denials_in_exec": 0, "denials_by_class": {}, "top_reason_ids": [],
            "first_call_denials": {"sessions": 0, "denied_sessions": 1, "share": 0.0},
            "recovered": 0, "unrecovered": 2}
        rows = [{**group, **quiet, "bucket": "project", "rule": None, "project_key": f"k{i}",
            "agent": "codex"} for i in range(3)]
        rows.append({**group, "bucket": "project", "rule": None, "project_key": "loud",
            "agent": "codex"})
        lines = hook_denials.text_lines({"summary": group, "by_agent": {}, "reason_ids": {
            "key": "ephemeral"}, "by_project_and_agent": rows})
        self.assertEqual(sum("Hook denials project" in line for line in lines), 1)
        self.assertIn("Hook denials: 3 project/agent rows: no hook denials", lines)


if __name__ == "__main__":
    unittest.main()
