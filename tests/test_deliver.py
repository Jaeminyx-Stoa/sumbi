"""Synthetic ledger/outcome truths; no network and no real session data."""

import contextlib
import csv
from datetime import timedelta
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sumbi.cli import main
from sumbi.deliver import deliver, id_matches, judge, text_summary, wilson
from sumbi.deliver_evidence import branch_query, tool_refs
from sumbi.ledger import COLUMNS, Deliverable, read_ledger
from sumbi.model import ProjectRule, Window, timestamp
from sumbi.outcomes import FixtureOutcomes

START = timestamp("2030-01-01T00:00:00Z")
WINDOW = Window(START, timestamp("2030-01-10T00:00:00Z"))
REPO = "example/sample"


def time(day, seconds=0):
    return (START + timedelta(days=day - 1, seconds=seconds)).isoformat().replace("+00:00", "Z")


def pull(number, merged_day=2, created_day=1, *, title="Synthetic change", body=None, state="closed", checks="success"):
    head, merge = f"{number:040x}", f"{number + 100:040x}"
    merged_at = time(merged_day) if merged_day else None
    pr = {"number": number, "state": state, "created_at": time(created_day), "merged": bool(merged_day),
          "merged_at": merged_at, "closed_at": merged_at or (time(created_day + 1) if state == "closed" else None),
          "head": {"sha": head}, "merge_commit_sha": merge if merged_day else None, "title": title, "body": body}
    snapshot = None if not merged_day else {"head_sha": head, "captured_at": merged_at, "required": ["test"],
          "check_runs": [{"name": "test", "head_sha": head, "started_at": time(merged_day - 1),
                           "completed_at": merged_at, "status": "completed", "conclusion": checks}], "statuses": []}
    return {"response": pr, "checks_at_merge": snapshot}


def outcome_fixture():
    pulls = [pull(1), pull(2), pull(3, 28), pull(4), pull(5), pull(6),
             pull(7, 5, 4, title="Fix #6")]
    return {"repository": REPO, "coverage_start": time(1), "observed_at": time(32),
            "pulls_complete": True, "commits_complete": True, "pulls": pulls,
            "commits": [{"sha": "f" * 40, "commit": {"committer": {"date": time(3)},
                         "message": "Revert change\nThis reverts commit " + f"{102:040x}" + "."}}]}


def ledger_fixture():
    return [
        ["D1", time(1), "Synthetic acceptance", REPO, REPO + "#1", "work/D1", "", "", ""],
        ["Dfail", time(1), "Synthetic acceptance", REPO, "", "work/Dfail", "abandoned@" + time(2), "", ""],
        ["Drev", time(1), "Synthetic acceptance", REPO, REPO + "#2", "work/Drev", "", "", ""],
        ["Dimm", time(1), "Synthetic acceptance", REPO, REPO + "#3", "work/Dimm", "", "", ""],
        ["Dsplit", time(1), "Synthetic acceptance", REPO, REPO + "#4;" + REPO + "#5", "work/Dsplit", "", "y", ""],
        ["Dfollow", time(1), "Synthetic acceptance", REPO, REPO + "#6", "work/Dfollow", "", "", ""],
    ]


def message(identity, deliverable=None, *, session="fixture", day=1, seconds=60, cwd=None, content=()):
    event = {"type": "assistant", "sessionId": session, "timestamp": time(day, seconds),
             "message": {"id": identity, "content": list(content), "usage": {"input_tokens": 10,
                 "cache_creation_input_tokens": 2, "cache_read_input_tokens": 3, "output_tokens": 5}}}
    if deliverable or cwd:
        event["cwd"] = cwd or "/synthetic/sample/" + deliverable
    return event


def write_fixture(root):
    ledger = root / "deliverables.csv"
    with ledger.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(COLUMNS)
        writer.writerows(ledger_fixture())
    outcomes = root / "outcomes"
    outcomes.mkdir(exist_ok=True)
    (outcomes / "repository.json").write_text(json.dumps(outcome_fixture(), indent=2) + "\n", encoding="utf-8")
    home = root / "home"
    logs = home / ".claude/projects/fixture"
    logs.mkdir(parents=True, exist_ok=True)
    events = [message("m-" + d, d, session=d) for d in ("D1", "Dfail", "Drev", "Dimm", "Dsplit", "Dfollow")]
    events += [message("split-a", "D1", session="split", seconds=120),
               message("split-b", "Dsplit", session="split", seconds=180),
               message("unallocated", session="unallocated", cwd="/synthetic/sample", seconds=240),
               message("unassigned", session="unassigned", seconds=240),
               message("other", session="other", cwd="/synthetic/unrelated", seconds=240),
               message("late", "Dfollow", session="late", day=11)]
    # Separate files preserve adapter session identities.
    grouped = {}
    for e in events:
        grouped.setdefault(e["sessionId"], []).append(e)
    for session, records in grouped.items():
        (logs / (session + ".jsonl")).write_text("\n".join(json.dumps(e) for e in records) + "\n", encoding="utf-8")
    return ledger, outcomes, home


class DeliverTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.ledger, self.outcome_dir, self.home = write_fixture(self.root)
        self.rules = [ProjectRule("sample", paths=["/synthetic/sample*"])]
        self.guard = patch.dict(os.environ, {}, clear=False)
        self.guard.start()
        self.addCleanup(self.guard.stop)
        os.environ.pop("SUMBI_SALT", None)
        for target in ("socket.socket", "socket.create_connection", "socket.getaddrinfo"):
            guard = patch(target, side_effect=AssertionError("No network in fixtures"))
            guard.start()
            self.addCleanup(guard.stop)

    def outcomes(self, raw=None):
        if raw is not None:
            (self.outcome_dir / "repository.json").write_text(json.dumps(raw), encoding="utf-8")
        return FixtureOutcomes(self.outcome_dir)

    def report(self, **kwargs):
        return deliver(self.home, WINDOW, self.ledger, self.outcomes(), rules=self.rules, **kwargs)

    def d(self, number="D1"):
        return next(d for d in read_ledger(self.ledger) if d.id == number)

    def write_rows(self, rows):
        with self.ledger.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(COLUMNS)
            writer.writerows(rows)

    def write_log(self, events, agent="claude-code"):
        root = self.home / (".claude/projects/new" if agent == "claude-code" else ".codex/sessions/new")
        root.mkdir(parents=True, exist_ok=True)
        (root / ("new.jsonl" if agent == "claude-code" else "rollout-new.jsonl")).write_text(
            "\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")

    def test_hand_computed_truths(self):
        report = self.report()
        self.assertEqual(report["states"], {"success": 3, "failed": 2, "in_progress": 0, "immature": 1})
        self.assertEqual(report["success_rate"]["numerator"], 3)
        self.assertEqual(report["success_rate"]["denominator"], 6)
        self.assertEqual(report["first_pass_success_rate"]["numerator"], 2)
        self.assertAlmostEqual(report["success_rate"]["wilson_95"][0], 0.18761630648)
        self.assertEqual(report["cost"]["operational"]["spend"]["total"], 180)
        self.assertEqual(report["cost"]["operational"]["tokens_per_success"]["total"], 60)
        self.assertEqual(report["cost"]["operational_linked"]["spend"]["total"], 160)
        self.assertEqual(report["cost"]["cohort"]["spend"]["total"], 180)
        self.assertEqual(report["cost"]["cohort"]["tokens_per_success"]["total"], 60)
        self.assertEqual(report["cost"]["cohort"]["spend"]["new_input"], 90)
        self.assertIsNone(report["cost"]["cohort"]["spend"]["reasoning_output"])
        self.assertEqual({k: v["total"] for k, v in report["cost"]["period_spend"].items()},
                         {"linked": 160, "unallocated": 20, "unassigned": 20, "other": 20})

    def test_failure_without_pr_and_elapsed(self):
        row = judge(self.d("Dfail"), self.outcomes())
        self.assertEqual(row["state"], "failed")
        self.assertEqual((row["closed_at"] - START).total_seconds(), 86400)
        rows = ledger_fixture()
        rows[1][6] = "abandoned"
        self.write_rows(rows)
        self.assertIsNone(judge(self.d("Dfail"), self.outcomes())["closed_at"])

    def test_revert_inside_window(self):
        self.assertEqual(judge(self.d("Drev"), self.outcomes())["reason"], "reverted")

    def test_immature_merge(self):
        self.assertEqual(judge(self.d("Dimm"), self.outcomes())["state"], "immature")

    def test_split_two_prs_requires_both(self):
        self.assertEqual(judge(self.d("Dsplit"), self.outcomes())["state"], "success")
        raw = outcome_fixture()
        raw["pulls"][4] = pull(5, None, state="open")
        self.assertEqual(judge(self.d("Dsplit"), self.outcomes(raw))["state"], "in_progress")
        raw["pulls"][4] = pull(5, None)
        self.assertEqual(judge(self.d("Dsplit"), self.outcomes(raw))["state"], "failed")

    def test_session_links_two_deliverables_by_event(self):
        report = self.report()
        grouped = {}
        for row in report["links"]:
            if row["deliverable_id"]:
                grouped.setdefault(row["session_id"], set()).add(row["deliverable_id"])
        self.assertIn({"D1", "Dsplit"}, grouped.values())
        for kind in ("new_input", "cache_write", "cache_read", "output", "reasoning_output", "total"):
            self.assertEqual(sum(r["tokens"][kind] or 0 for r in report["links"]),
                             sum(v[kind] or 0 for v in report["cost"]["lifetime_scan_spend"].values()))

    def test_unlinked_session_is_not_spread(self):
        report = self.report()
        unassigned = [r for r in report["links"] if r["bucket"] == "unassigned"]
        self.assertEqual(len(unassigned), 1)
        self.assertIsNone(unassigned[0]["deliverable_id"])
        self.assertEqual(unassigned[0]["tokens"]["total"], 20)

    def test_weak_project_time_fallback_is_labelled(self):
        self.write_log([message("weak", cwd="/synthetic/sample", day=9, session="weak")])
        rows = [r for r in self.report()["links"] if r["evidence"] == "project_time_weak"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["deliverable_id"], "Dimm")

    def test_branch_id_has_priority_over_pr_tool_reference(self):
        block = {"type": "tool_use", "id": "t", "name": "Bash", "input": {"command": "gh pr view https://github.com/example/sample/pull/4"}}
        e = message("priority", session="priority", content=[block])
        e["gitBranch"] = "feature/D1-change"
        self.write_log([e])
        rows = [r for r in self.report()["links"] if r["tokens"]["total"] == 20 and r["evidence"] == "deliverable_id" and r["deliverable_id"] == "D1"]
        self.assertGreaterEqual(len(rows), 2)

    def test_tool_url_from_input_and_output(self):
        for output in (False, True):
            with self.subTest(output=output):
                tool = {"type": "tool_use", "id": "t", "name": "Bash", "input": {"command": "gh pr view https://github.com/example/sample/pull/1" if not output else "gh pr create"}}
                events = [{"type": "user", "timestamp": time(1, 1), "sessionId": "url", "message": {"content": "DO NOT READ https://github.com/example/sample/pull/4"}},
                          {"type": "assistant", "timestamp": time(1, 2), "sessionId": "url", "message": {"content": [tool]}}]
                if output:
                    events.append({"type": "user", "timestamp": time(1, 3), "sessionId": "url", "message": {"content": [
                        {"type": "tool_result", "tool_use_id": "t", "content": "https://github.com/example/sample/pull/1"}]}})
                events.append(message("url", session="url", seconds=4))
                self.write_log(events)
                rows = [r for r in self.report()["links"] if r["evidence"] == "tool_reference"]
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["deliverable_id"], "D1")

    def test_message_prose_is_not_link_evidence(self):
        e = message("prose", session="prose", content=[{"type": "text", "text": "work/D1 https://github.com/example/sample/pull/1"}])
        self.write_log([e])
        self.assertEqual(self.report()["cost"]["period_spend"]["unassigned"]["total"], 40)

    def test_conflicting_ids_do_not_split_equally(self):
        self.write_log([message("conflict", session="conflict", cwd="/synthetic/sample/D1/Dsplit")])
        report = self.report()
        self.assertEqual(report["cost"]["period_spend"]["unallocated"]["total"], 40)
        self.assertEqual(report["coverage"]["evidence_counts"]["ambiguous"], 2)

    def test_codex_events_keep_counter_delta_and_context(self):
        self.write_log([
            {"type": "session_meta", "timestamp": time(1, 1), "payload": {"id": "codex", "cwd": "/synthetic/sample/D1", "git": {"branch": "work/D1"}}},
            {"type": "event_msg", "timestamp": time(1, 2), "payload": {"type": "token_count", "info": {"total_token_usage": {"input_tokens": 15, "cached_input_tokens": 5, "output_tokens": 5, "reasoning_output_tokens": 2}}}},
            {"type": "turn_context", "timestamp": time(1, 3), "payload": {"cwd": "/synthetic/sample/Dsplit"}},
            {"type": "event_msg", "timestamp": time(1, 4), "payload": {"type": "token_count", "info": {"total_token_usage": {"input_tokens": 30, "cached_input_tokens": 10, "output_tokens": 10, "reasoning_output_tokens": 4}}}},
        ], "codex")
        rows = [r for r in self.report()["links"] if r["tokens"]["cache_write"] is None and r["bucket"] == "linked"]
        self.assertEqual({r["deliverable_id"] for r in rows}, {"D1", "Dsplit"})
        self.assertEqual(sum(r["tokens"]["total"] for r in rows), 40)
        self.assertEqual(sum(r["tokens"]["reasoning_output"] for r in rows), 4)

    def test_codex_header_branch_survives_same_directory_turns(self):
        self.write_log([
            {"type": "session_meta", "timestamp": time(1, 1), "payload": {"id": "branch-header", "cwd": "/synthetic/sample", "git": {"branch": "work/D1"}}},
            {"type": "turn_context", "timestamp": time(1, 2), "payload": {"cwd": "/synthetic/sample"}},
            {"type": "event_msg", "timestamp": time(1, 3), "payload": {"type": "token_count", "info": {"total_token_usage": {"input_tokens": 10, "cached_input_tokens": 0, "output_tokens": 5, "reasoning_output_tokens": 0}}}},
            {"type": "turn_context", "timestamp": time(1, 4), "payload": {"cwd": "/synthetic/sample"}},
            {"type": "response_item", "timestamp": time(1, 5), "payload": {"type": "function_call", "name": "exec_command", "call_id": "switch", "arguments": {"cmd": "git switch work/Dsplit"}}},
            {"type": "event_msg", "timestamp": time(1, 6), "payload": {"type": "token_count", "info": {"total_token_usage": {"input_tokens": 20, "cached_input_tokens": 0, "output_tokens": 10, "reasoning_output_tokens": 0}}}},
        ], "codex")
        rows = [r for r in self.report()["links"] if r["tokens"]["cache_write"] is None and r["bucket"] == "linked"]
        self.assertEqual({(r["deliverable_id"], r["evidence"], r["tokens"]["total"]) for r in rows},
                         {("D1", "deliverable_id", 15), ("Dsplit", "tool_reference", 15)})

    def test_human_acceptance_is_required_when_n(self):
        rows = ledger_fixture()
        rows[0][7] = "n"
        self.write_rows(rows)
        self.assertEqual(judge(self.d(), self.outcomes())["reason"], "human_acceptance_pending")

    def test_followup_success_is_eventual_only(self):
        row = judge(self.d("Dfollow"), self.outcomes())
        self.assertEqual(row["state"], "success")
        self.assertFalse(row["first_pass_success"])
        self.assertEqual(row["closed_at"], timestamp(time(5)))

    def test_followup_reference_boundaries_and_window(self):
        raw = outcome_fixture()
        raw["pulls"].extend([pull(8, 4, 3, title="Fix #10"), pull(9, 11, 9, title="Fix #1")])
        self.assertTrue(judge(self.d(), self.outcomes(raw))["first_pass_success"])
        raw["pulls"][7] = pull(8, 4, 3, body="Fix regression in " + f"{101:040x}")
        self.assertFalse(judge(self.d(), self.outcomes(raw))["first_pass_success"])

    def test_followup_url_and_explicit_number_references(self):
        for reference in ("https://github.com/example/sample/pull/1", "example/sample#1", "PR 1", "pull request 1"):
            raw = outcome_fixture()
            raw["pulls"].append(pull(8, 4, 3, title="Fix " + reference))
            with self.subTest(reference=reference):
                self.assertFalse(judge(self.d(), self.outcomes(raw))["first_pass_success"])
        for reference in ("https://github.com/example/other/pull/1", "example/other#1", "#1abc", "#10"):
            raw = outcome_fixture()
            raw["pulls"].append(pull(8, 4, 3, title="Fix " + reference))
            with self.subTest(reference=reference):
                self.assertTrue(judge(self.d(), self.outcomes(raw))["first_pass_success"])

    def test_revert_pr_cannot_count_as_repair(self):
        raw = outcome_fixture()
        raw["pulls"].append(pull(8, 4, 3, title="Revert #1"))
        self.assertEqual(judge(self.d(), self.outcomes(raw))["state"], "failed")

    def test_repair_after_revert_can_recover(self):
        raw = outcome_fixture()
        raw["pulls"].append(pull(8, 5, 4, title="Fix regression #2"))
        row = judge(self.d("Drev"), self.outcomes(raw))
        self.assertEqual(row["state"], "success")
        self.assertFalse(row["first_pass_success"])

    def test_unknown_required_checks_cannot_succeed(self):
        raw = outcome_fixture()
        raw["pulls"][0]["checks_at_merge"] = None
        row = judge(self.d(), self.outcomes(raw))
        self.assertEqual(row["reason"], "checks_policy_unreadable")
        raw["pulls"][0] = pull(1)
        raw["pulls"][0]["checks_at_merge"]["check_runs"] = []
        self.assertEqual(judge(self.d(), self.outcomes(raw))["reason"], "checks_missing_required")

    def test_red_checks_cannot_succeed(self):
        raw = outcome_fixture()
        raw["pulls"][0] = pull(1, checks="failure")
        self.assertEqual(judge(self.d(), self.outcomes(raw))["reason"], "checks_red")

    def test_mixed_basis_counts_and_per_pr_verdicts(self):
        raw = outcome_fixture()
        for index, required in ((0, [{"context": "test", "app_id": None}]), (3, [])):
            entry = raw["pulls"][index]
            results = entry["checks_at_merge"]
            results["required"] = []
            entry["checks_at_merge"] = None
            entry["current_policy_evidence"] = {"required": required, "results": results}
        self.outcomes(raw)
        report = self.report()
        self.assertEqual(report["checks_basis_counts"], {
            "historical": 3, "current_policy": 1, "all_visible": 1, "unknown": 1})
        rows = {row["id"]: row for row in report["deliverables"]}
        self.assertEqual(rows["D1"]["checks_basis"], "current_policy")
        self.assertEqual(rows["Dsplit"]["checks_basis"], "all_visible")
        self.assertEqual([pr["checks_basis"] for pr in rows["Dsplit"]["pr_outcomes"]], ["all_visible", "historical"])
        self.assertTrue(all(pr["checks_at_merge"] == "green" for pr in rows["Dsplit"]["pr_outcomes"]))
        self.assertIn("historical 3; current_policy 1; all_visible 1; unknown 1", text_summary(report))

    def test_wrong_head_and_postmerge_checks_are_rejected(self):
        for field in ("head_sha", "completed_at"):
            raw = outcome_fixture()
            raw["pulls"][0]["checks_at_merge"]["check_runs"][0][field] = "e" * 40 if field == "head_sha" else time(3)
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "PR head"):
                self.outcomes(raw)

    def test_incomplete_observation_prevents_success(self):
        for key in ("pulls_complete", "commits_complete", "coverage_start"):
            raw = outcome_fixture()
            raw[key] = time(3) if key == "coverage_start" else False
            if key == "coverage_start":
                raw["commits"] = []
            with self.subTest(key=key):
                self.assertEqual(judge(self.d(), self.outcomes(raw))["state"], "immature")

    def test_missing_pr_stays_pending(self):
        raw = outcome_fixture()
        raw["pulls"] = raw["pulls"][1:]
        self.assertEqual(judge(self.d(), self.outcomes(raw))["reason"], "missing_pr")

    def test_missing_constituent_does_not_hide_known_failure(self):
        raw = outcome_fixture()
        raw["pulls"] = [p for p in raw["pulls"] if p["response"]["number"] != 4]
        raw["pulls"] = [pull(5, None) if p["response"]["number"] == 5 else p for p in raw["pulls"]]
        self.assertEqual(judge(self.d("Dsplit"), self.outcomes(raw))["state"], "failed")

    def test_ledger_strict_validation_and_safe_errors(self):
        changes = [(0, "private secret value"), (1, "2030-01-01"), (2, ""), (3, "bad"),
                   (4, "other/repo#1"), (5, "bad branch"), (6, "success"), (7, "yes"), (8, "line\nbreak")]
        for column, value in changes:
            rows = ledger_fixture()
            rows[0][column] = value
            self.write_rows(rows)
            with self.subTest(column=column), self.assertRaises(ValueError) as error:
                read_ledger(self.ledger)
            if value:
                self.assertNotIn(value, str(error.exception))

    def test_duplicate_ids_and_prs_fail(self):
        for column in (0, 4):
            rows = ledger_fixture()
            rows[1][column] = rows[0][column]
            self.write_rows(rows)
            with self.subTest(column=column), self.assertRaises(ValueError):
                read_ledger(self.ledger)

    def test_report_has_no_free_text_paths_or_branches(self):
        report = self.report(salt=b"synthetic-key")
        data = json.dumps(report) + text_summary(report)
        for secret in ("Synthetic acceptance", "/synthetic", "work/", "Synthetic change", "This reverts", REPO, "fixture"):
            self.assertNotIn(secret, data)
        self.assertTrue(report["pseudonyms"]["salted"])

    def test_cli_json_and_safe_destinations(self):
        args = ["deliver", "--ledger", str(self.ledger), "--outcomes", str(self.outcome_dir),
                "--home", str(self.home), "--since", time(1), "--until", time(10),
                "--project", "sample", "--match-path", "/synthetic/sample*", "--json", "-"]
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(main(args), 0)
        self.assertEqual(json.loads(out.getvalue())["states"]["success"], 3)
        self.assertIn("sumbi deliver", err.getvalue())
        for dest in (self.ledger, self.outcome_dir / "repository.json", self.home / ".claude/projects/x.json"):
            with self.subTest(dest=dest), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                main(args[:-1] + [str(dest)])

    def test_empty_ledger_and_zero_success(self):
        self.write_rows([])
        report = self.report()
        self.assertEqual(report["success_rate"]["denominator"], 0)
        self.assertIsNone(report["cost"]["cohort"]["tokens_per_success"]["total"])

    def test_period_successes_use_final_merge_not_dispatch_or_maturity(self):
        period = Window(START, timestamp(time(3)))
        report = deliver(self.home, period, self.ledger, self.outcomes(), rules=self.rules)
        self.assertEqual(report["cost"]["operational"]["successes"], 2)
        self.assertEqual(report["cost"]["operational"]["tokens_per_success"]["total"], 90)
        self.assertEqual(report["cost"]["cohort"]["successes"], 3)
        self.assertEqual(report["cost"]["cohort"]["spend"]["total"], 180)

    def test_cohort_excludes_preperiod_dispatch_and_keeps_period_successes(self):
        period = Window(timestamp(time(2)), timestamp(time(3)))
        report = deliver(self.home, period, self.ledger, self.outcomes(), rules=self.rules)
        self.assertEqual(report["cost"]["cohort_success_rate"]["denominator"], 0)
        self.assertEqual(report["cost"]["operational"]["successes"], 2)
        self.assertEqual(report["cost"]["operational"]["spend"]["total"], 0)

    def test_followup_own_window_must_mature(self):
        raw = outcome_fixture()
        raw["observed_at"] = time(11)
        raw["pulls"] = [p for p in raw["pulls"] if p["response"]["number"] != 3]
        self.assertEqual(judge(self.d("Dfollow"), self.outcomes(raw))["reason"], "follow_up_window_open")

    def test_fix_pr_url_links_lifetime_retry_spend(self):
        block = {"type": "tool_use", "id": "t", "name": "Bash", "input": {"command": "gh pr view https://github.com/example/sample/pull/7"}}
        self.write_log([message("repair", session="repair", day=12, content=[block])])
        report = self.report()
        self.assertEqual(report["cost"]["cohort"]["spend"]["total"], 200)
        self.assertEqual(report["cost"]["operational"]["spend"]["total"], 180)

    def test_missing_token_counts_preserve_unknown_subset(self):
        report = self.report()
        self.assertEqual(report["coverage"]["not_reported_token_events"]["period"]["reasoning_output"], 11)
        self.assertEqual(report["coverage"]["not_reported_token_events"]["lifetime_scan"]["reasoning_output"], 12)
        self.assertEqual(report["coverage"]["not_reported_token_events"]["period"]["new_input"], 0)

    def test_latest_commit_status_at_merge_wins(self):
        raw = outcome_fixture()
        snapshot = raw["pulls"][0]["checks_at_merge"]
        snapshot["check_runs"] = []
        snapshot["statuses"] = [
            {"sha": snapshot["head_sha"], "context": "test", "updated_at": time(2), "state": "success"},
            {"sha": snapshot["head_sha"], "context": "test", "updated_at": time(1), "state": "failure"}]
        self.assertEqual(judge(self.d(), self.outcomes(raw))["state"], "success")

    def test_latest_check_attempt_pending_at_merge_blocks_success(self):
        raw = outcome_fixture()
        snapshot = raw["pulls"][0]["checks_at_merge"]
        snapshot["check_runs"].append({"name": "test", "head_sha": snapshot["head_sha"],
            "started_at": time(1, 3600), "completed_at": None, "status": "in_progress", "conclusion": None})
        self.assertEqual(judge(self.d(), self.outcomes(raw))["reason"], "checks_red")

    def test_explicit_no_required_checks_qualifies(self):
        raw = outcome_fixture()
        snapshot = raw["pulls"][0]["checks_at_merge"]
        snapshot["required"], snapshot["check_runs"] = [], []
        self.assertEqual(judge(self.d(), self.outcomes(raw))["state"], "success")

    def test_duplicate_repository_fixtures_fail(self):
        (self.outcome_dir / "duplicate.json").write_text(json.dumps(outcome_fixture()), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "duplicate repository"):
            self.outcomes()

    def test_fixture_observation_bounds_and_malformed_data(self):
        for key, value in (("observed_at", time(1)), ("pulls_complete", "yes"), ("repository", "secret invalid repo")):
            raw = outcome_fixture()
            raw[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError) as error:
                self.outcomes(raw)
            self.assertNotIn(value, str(error.exception))

    def test_bad_ledger_header_and_extra_columns(self):
        for text in ("id,notes\nD1,private\n", ",".join(COLUMNS) + "\n" + ",".join(ledger_fixture()[0]) + ",extra\n"):
            self.ledger.write_text(text, encoding="utf-8")
            with self.assertRaises(ValueError):
                read_ledger(self.ledger)

    def test_windows_id_matching_and_posix_case(self):
        ledger = read_ledger(self.ledger)
        self.assertEqual(id_matches("c:/synthetic/sample/d1", ledger, path=True), {"D1"})
        self.assertEqual(id_matches("/synthetic/sample/d1", ledger, path=True), set())
        self.assertEqual(id_matches("work/D10", ledger), set())

    def test_revert_elapsed_ends_at_original_merge(self):
        row = next(r for r in self.report()["deliverables"] if r["id"] == "Drev")
        self.assertEqual(row["elapsed_seconds"], 86400)

    def test_outside_project_success_does_not_lower_cost(self):
        rows = ledger_fixture()
        rows.append(["Dother", time(1), "Synthetic acceptance", "example/other", "example/other#1", "other/Dother", "", "", ""])
        self.write_rows(rows)
        raw = outcome_fixture()
        raw["repository"], raw["commits"], raw["pulls"] = "example/other", [], [pull(1)]
        (self.outcome_dir / "other.json").write_text(json.dumps(raw), encoding="utf-8")
        report = self.report()
        self.assertEqual(report["states"]["success"], 4)
        self.assertEqual(report["cost"]["operational"]["successes"], 3)
        self.assertEqual(report["cost"]["cohort"]["successes"], 3)

    def test_shipped_fixtures_have_same_hand_computed_truth(self):
        root = Path(__file__).parent / "fixtures/deliver"
        report = deliver(root / "home", WINDOW, root / "deliverables.csv", FixtureOutcomes(root / "outcomes"), rules=self.rules)
        self.assertEqual(report["states"], self.report()["states"])
        self.assertEqual(report["cost"]["cohort"]["tokens_per_success"]["total"], 60)

    def test_branch_query_outputs_are_bounded_machine_evidence(self):
        rows = ledger_fixture()
        rows[0][5] = "feature/opaque"
        self.write_rows(rows)
        tool = {"type": "tool_use", "id": "t", "name": "Bash", "input": {"command": "git branch --show-current"}}
        self.write_log([
            {"type": "assistant", "timestamp": time(1, 1), "sessionId": "branch", "message": {"content": [tool]}},
            {"type": "user", "timestamp": time(1, 2), "sessionId": "branch", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "t", "content": "feature/opaque\n"}]}},
            message("branch", session="branch", seconds=3)])
        row = next(r for r in self.report()["links"] if r["evidence"] == "tool_reference")
        self.assertEqual(row["deliverable_id"], "D1")

    def test_preperiod_context_and_future_references(self):
        events = [
            {"type": "session_meta", "timestamp": time(1), "payload": {"id": "preperiod"}},
            {"type": "response_item", "timestamp": time(1, 1), "payload": {"type": "function_call", "name": "exec_command",
                "call_id": "first", "arguments": {"cmd": "gh pr view https://github.com/example/sample/pull/1"}}},
            {"type": "event_msg", "timestamp": time(2, 1), "payload": {"type": "token_count", "info": {"total_token_usage": {
                "input_tokens": 10, "cached_input_tokens": 0, "output_tokens": 5, "reasoning_output_tokens": 0}}}},
            {"type": "response_item", "timestamp": time(2, 2), "payload": {"type": "function_call", "name": "exec_command",
                "call_id": "future", "arguments": {"cmd": "gh pr view https://github.com/example/sample/pull/4"}}}]
        self.write_log(events, "codex")
        report = deliver(self.home, Window(timestamp(time(2)), timestamp(time(3))), self.ledger, self.outcomes(), rules=self.rules)
        row = next(r for r in report["links"] if r["tokens"]["cache_write"] is None and r["deliverable_id"] == "D1")
        self.assertEqual(row["tokens"]["total"], 15)

    def test_huge_followup_window_has_safe_error(self):
        with self.assertRaisesRegex(ValueError, "timestamp range"):
            self.report(follow_up_days=1e308)


class WilsonTests(unittest.TestCase):
    def test_small_numbers(self):
        self.assertAlmostEqual(wilson(1, 2)["wilson_95"][0], 0.094531205734)
        self.assertAlmostEqual(wilson(1, 2)["wilson_95"][1], 0.905468794266)
        self.assertAlmostEqual(wilson(0, 1)["wilson_95"][1], 0.793450685623)
        self.assertAlmostEqual(wilson(1, 1)["wilson_95"][0], 0.206549314377)
        self.assertIsNone(wilson(0, 0)["wilson_95"])

    def test_invalid_counts(self):
        for values in ((2, 1), (-1, 1), (0, -1), (True, 1)):
            with self.subTest(values=values), self.assertRaises(ValueError):
                wilson(*values)


class ReferenceTests(unittest.TestCase):
    def test_literal_branch_inputs(self):
        self.assertIn(("branch", "work/D1"), tool_refs("Bash", {"command": "git switch work/D1"}))
        self.assertIn(("branch", "work/D1"), tool_refs("exec_command", {"cmd": "git -C /synthetic/sample checkout -b work/D1"}))

    def test_shell_evaluation_and_unknown_tools_are_skipped(self):
        for value in ("git switch $branch", "echo x; git switch work/D1", "git switch `helper`"):
            self.assertEqual(tool_refs("Bash", {"command": value}), set())
        self.assertEqual(tool_refs("unknown", {"command": "git switch work/D1"}), set())

    def test_plain_branch_output_requires_corresponding_query(self):
        self.assertTrue(branch_query("exec_command", {"cmd": "git -C /synthetic/sample branch --show-current"}))
        self.assertFalse(branch_query("exec_command", {"cmd": "echo branch"}))
        self.assertEqual(tool_refs("exec_command", "work/D1\n", output=True), set())
        self.assertEqual(tool_refs("exec_command", "work/D1\n", output=True, query=True),
                         {("branch", "work/D1"), ("branch_observed", "work/D1")})
        self.assertEqual(tool_refs("exec_command", "Some prose mentions work/D1", output=True, query=True), set())


if __name__ == "__main__":
    unittest.main()
