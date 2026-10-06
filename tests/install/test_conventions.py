"""Synthetic multilingual directives, declared ownership and instruction costs."""

import json
from pathlib import Path
from unittest.mock import patch

from sumbi.install import find_gaps, inventory
from sumbi.install.conventions import load_lexicon
from sumbi.install.inventory import conditional_paths
from sumbi.install.text import ESTIMATE_RULE, estimate_tokens
from sumbi.install.planner import build_plan
from .test_inventory import OfflineTest

# Escaped synthetic text keeps source and reports in English.
KOREAN = {
    "review_gate": "\uc704\ud5d8 \uacbd\ub85c\ub294 \ub2e4\ub978 \uacc4\uc5f4 \ub3c5\ub9bd \ub9ac\ubdf0\ub97c \ubc1b\uc544\uc57c \ud55c\ub2e4.",
    "handoff": "\uc791\uc5c5 \ud6c4 \uc778\uacc4 \ubb38\uc11c\ub97c \uc791\uc131\ud55c\ub2e4.",
    "friction_line": "\ubcf4\uace0\uc11c \ub05d\uc5d0 \ud558\ub124\uc2a4 \ub9c8\ucc30: \ud55c \uc904\uc744 \ub0a8\uae34\ub2e4.",
    "parallel_worktree": "\ubcd1\ub82c \uc791\uc5c5\uc740 \ubcc4\ub3c4 \uc6cc\ud06c\ud2b8\ub9ac\ub97c \uc0ac\uc6a9\ud55c\ub2e4.",
    "plan_approval": "\uad6c\ud604 \uc804 \uacc4\ud68d\uc744 \uc791\uc131\ud558\uace0 \ub300\ud45c \uc2b9\uc778\uc744 \ubc1b\ub294\ub2e4.",
}
CONVENTION_GAPS = {"missing-risk-review", "missing-handoff", "missing-friction-line",
                   "missing-worktree-rule", "missing-plan-approval"}


class ConventionTests(OfflineTest):
    def write(self, root, path, text):
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    def test_korean_directive_for_each_convention(self):
        for key, directive in KOREAN.items():
            with self.subTest(convention=key):
                root = self.copy_fixture("empty")
                self.write(root, "AGENTS.md", directive + "\n")
                report = inventory(root)
                self.assertEqual(report["conventions"][key], {"status": "present",
                    "evidence": [{"path": "AGENTS.md", "kind": "directive"}]})
                self.assertNotIn("convention-language-unsupported", {w["kind"] for w in report["warnings"]})
                self.assertNotIn(directive, json.dumps(report, ensure_ascii=False))

    def test_korean_roadmap_examples_and_denials_do_not_count(self):
        prefixes = ("\ub85c\ub4dc\ub9f5: ", "\uc608\uc2dc: ", "\uc544\uc774\ub514\uc5b4: ")
        for prefix in prefixes:
            with self.subTest(prefix=prefix):
                root = self.copy_fixture("empty")
                self.write(root, "AGENTS.md", "\n".join(prefix + text for text in KOREAN.values()))
                self.assertTrue(all(c["status"] == "absent" for c in inventory(root)["conventions"].values()))
        root = self.copy_fixture("empty")
        self.write(root, "AGENTS.md", "\n".join(text + " \ud544\uc694 \uc5c6\uc74c." for text in KOREAN.values()))
        self.assertTrue(all(c["status"] == "absent" for c in inventory(root)["conventions"].values()))

    def test_uncovered_script_is_unknown_and_not_a_gap(self):
        root = self.copy_fixture("empty")
        text = "\u0420\u0430\u0431\u043e\u0442\u0430 \u0438 \u043f\u0440\u0430\u0432\u0438\u043b\u0430.\n" * 10
        self.write(root, "AGENTS.md", text)
        report = inventory(root)
        self.assertTrue(all(c == {"status": "unknown", "evidence": []} for c in report["conventions"].values()))
        self.assertIn({"kind": "convention-language-unsupported", "scripts": ["Cyrillic"]}, report["warnings"])
        self.assertFalse(CONVENTION_GAPS & {g["id"] for g in find_gaps(report)})
        self.assertEqual(build_plan(root, select=["review-gates", "handoff", "friction-line",
            "parallel-worktrees", "plan-and-approval"]).changes, [])
        self.assertNotIn(text.strip(), json.dumps(report, ensure_ascii=False))

    def test_add_language_using_only_lexicon_data(self):
        root = self.copy_fixture("empty")
        text = "\u0420\u0430\u0431\u043e\u0442\u0430 \u0438 \u043f\u0440\u0430\u0432\u0438\u043b\u0430.\n"
        self.write(root, "AGENTS.md", text)
        lexicon = load_lexicon()
        self.assertEqual(lexicon["version"], 1)
        lexicon["languages"]["synthetic"] = {"scripts": ["Cyrillic"], "exclude_lines": [],
                                             "directives": {"handoff": [text.strip()]}}
        with patch("sumbi.install.conventions.load_lexicon", return_value=lexicon):
            report = inventory(root)
        self.assertEqual(report["conventions"]["handoff"]["status"], "present")
        self.assertEqual(report["conventions"]["review_gate"]["status"], "absent")
        self.assertNotIn("convention-language-unsupported", {w["kind"] for w in report["warnings"]})

    def test_supported_directive_overrides_unknown(self):
        root = self.copy_fixture("empty")
        self.write(root, "AGENTS.md", "\u042f" * 300 + "\nLeave a handoff document.\n")
        report = inventory(root)
        self.assertEqual(report["conventions"]["handoff"]["status"], "present")
        self.assertEqual(report["conventions"]["review_gate"]["status"], "unknown")

    def test_code_and_metadata_do_not_vote_on_language(self):
        root = self.copy_fixture("empty")
        self.write(root, "AGENTS.md", "---\ndescription: " + "\u042f" * 200 + "\n---\n"
                   "```\n" + "\u042f" * 200 + "\n```\nLeave a handoff document.\n")
        report = inventory(root)
        self.assertEqual(report["conventions"]["review_gate"]["status"], "absent")

    def test_declared_paths_supply_evidence_without_reading_their_contents(self):
        root = self.copy_fixture("empty")
        self.write(root, "docs/process.md", "Synthetic owner guidance.\n")
        self.write(root, ".sumbi/config.toml", "[conventions]\n" + "\n".join(
            key + ' = ["docs/process.md"]' for key in KOREAN))
        report = inventory(root)
        self.assertEqual(report["convention_search_paths"], [])
        for convention in report["conventions"].values():
            self.assertEqual(convention, {"status": "present", "evidence": [{"path": "docs/process.md", "kind": "declared"}]})
        self.assertFalse(CONVENTION_GAPS & {g["id"] for g in find_gaps(report)})

    def test_missing_declaration_warns_without_presence(self):
        root = self.copy_fixture("empty")
        self.write(root, ".sumbi/config.toml", '[conventions]\nhandoff = ["docs/missing.md"]\n')
        report = inventory(root)
        self.assertEqual(report["conventions"]["handoff"], {"status": "absent", "evidence": []})
        self.assertIn({"kind": "convention-declaration-missing", "convention": "handoff", "path": "docs/missing.md"}, report["warnings"])
        self.assertIn("missing-handoff", {g["id"] for g in find_gaps(report)})

    def test_declarations_override_unknown_but_missing_paths_do_not(self):
        root = self.copy_fixture("empty")
        self.write(root, "AGENTS.md", "\u042f" * 200)
        self.write(root, ".sumbi/config.toml", '[conventions]\nhandoff = ["AGENTS.md", "missing.md"]\nreview_gate = ["missing.md"]\n')
        report = inventory(root)
        self.assertEqual(report["conventions"]["handoff"]["status"], "present")
        self.assertEqual(report["conventions"]["review_gate"]["status"], "unknown")

    def test_invalid_declarations_are_rejected_with_sanitized_errors(self):
        from sumbi.install.errors import InstallError
        root = self.copy_fixture("empty")
        for declaration in ('handoff = ["../outside.md"]', 'handoff = ["/outside.md"]',
                            'handoff = "docs/process.md"', 'unsupported = []', 'handoff = [1]'):
            with self.subTest(declaration=declaration):
                self.write(root, ".sumbi/config.toml", "[conventions]\n" + declaration)
                with self.assertRaises(InstallError) as caught:
                    inventory(root)
                self.assertNotIn(declaration, str(caught.exception))

    def test_conditional_rule_frontmatter_forms(self):
        for value in ('["src/**"]', '"src/**"', "\n  - src/**", "\n- src/**", "|\n  src/**", '>\n  src/**'):
            with self.subTest(value=value):
                self.assertTrue(conditional_paths("---\npaths: " + value + "\n---\nBody.\n"))
        for value in ('', '[]', '[ ]', 'null', '~', '""', "''", '# comment', '\n  # comment', '[] # comment'):
            with self.subTest(value=value):
                self.assertFalse(conditional_paths("---\npaths: " + value + "\n---\nBody.\n"))
        self.assertFalse(conditional_paths('Body.\npaths: ["src/**"]\n'))
        self.assertFalse(conditional_paths('---\nother:\n  paths: ["src/**"]\n---\n'))

    def test_conditional_rules_are_separate_from_always_loaded_budget(self):
        root = self.copy_fixture("empty")
        self.write(root, "CLAUDE.md", "Root.\n")
        unconditional = "Leave a handoff document.\n"
        conditional = '---\npaths:\n  - "src/**"\n---\n' + "\uac00" * 1000
        self.write(root, ".claude/rules/always.md", unconditional)
        self.write(root, ".claude/rules/scoped.md", conditional)
        report = inventory(root, budget=100)
        cost = report["cost"]
        self.assertEqual(cost["instructions"]["claude"]["paths"], [".claude/rules/always.md", "CLAUDE.md"])
        self.assertEqual(cost["instructions"]["claude"]["estimated_tokens"], estimate_tokens("Root.\n" + unconditional))
        self.assertEqual(cost["conditional_instructions"]["claude"], {"paths": [".claude/rules/scoped.md"],
            "characters": len(conditional), "estimated_tokens": estimate_tokens(conditional)})
        self.assertNotIn("instruction-budget", {g["id"] for g in find_gaps(report)})
        self.assertEqual(report["conventions"]["handoff"]["status"], "present")
        self.write(root, "CLAUDE.md", "@.claude/rules/scoped.md\n")
        self.assertNotIn(".claude/rules/scoped.md", inventory(root)["cost"]["instructions"]["claude"]["paths"])

    def test_script_aware_estimates_and_rule_label(self):
        text = "abcde\uac00\u6f22\u3042\u30a2"
        self.assertEqual(estimate_tokens(text), 6)
        self.assertEqual(estimate_tokens("\u1100\u3131\U00020000\uff76"), 4)
        self.assertEqual(estimate_tokens("\u30fc\u3099"), 2)
        self.assertEqual(estimate_tokens("abc\r\n"), estimate_tokens("abc\n"))
        root = self.copy_fixture("empty")
        self.write(root, "AGENTS.md", text)
        self.write(root, ".agents/skills/synthetic/SKILL.md", '---\ndescription: "' + text + '"\n---\n')
        report = inventory(root)
        self.assertEqual(report["cost"]["estimate_rule"], ESTIMATE_RULE)
        self.assertEqual(report["cost"]["instructions"]["codex"]["estimated_tokens"], 6)
        self.assertEqual(report["cost"]["skill_descriptions"][0]["estimated_tokens"], 6)

    def test_conditional_load_rule_data_matches_classification(self):
        from sumbi.install.placement import load_rules
        data = load_rules()
        self.assertEqual(data["revision"], 2)
        rule = next(row["rules"] for row in data["agents"] if row["agent"] == "claude-code")
        self.assertEqual(rule["conditional"], "nonempty-paths-frontmatter")
        self.assertEqual(rule["activation"], "matching-file-read")

    def test_largest_scope_is_chosen_by_tokens_not_characters(self):
        root = self.copy_fixture("empty")
        self.write(root, "a/AGENTS.md", "a" * 100)
        self.write(root, "b/AGENTS.md", "\uac00" * 30)
        cost = inventory(root)["cost"]["instructions"]["codex"]
        self.assertEqual(cost["paths"], ["b/AGENTS.md"])
        self.assertEqual(cost["estimated_tokens"], 30)
