#!/usr/bin/env python3
"""Offline regression tests for the Orca Luna Cycle helper (v2 dispatch)."""

from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

HELPER_PATH = Path(__file__).resolve().parents[1] / "scripts" / "orca_luna_worker.py"
SPEC = importlib.util.spec_from_file_location("orca_luna_worker", HELPER_PATH)
if SPEC is None or SPEC.loader is None:  # pragma: no cover
    raise RuntimeError(f"cannot load helper from {HELPER_PATH}")
helper = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(helper)


def valid_report(**extra: object) -> dict[str, object]:
    report: dict[str, object] = {
        "reportSchemaVersion": 1,
        "taskStatus": "done",
        "summary": "Mapped the bounded surface.",
        "evidence": ["README.md:1 fact"],
        "findings": [],
        "risks": [],
        "checks": ["git status --short -> unchanged"],
        "filesModified": [],
        "shards": [],
    }
    report.update(extra)
    return report


class CollectTests(unittest.TestCase):
    def setUp(self) -> None:
        self.environment = patch.dict(
            os.environ, {"ORCA_TERMINAL_HANDLE": "term_controller"}
        )
        self.environment.start()
        self.temporary = tempfile.TemporaryDirectory(prefix="orca-luna-test-")
        self.directory = Path(self.temporary.name)
        manifest, self.workers, self.prompts = helper.validate_manifest(
            helper.load_json(
                HELPER_PATH.parents[1] / "references" / "manifest-v2.example.json"
            )
        )
        helper.ensure_journal(self.directory)
        helper.save_json(self.directory / "manifest.json", manifest)
        helper.initialize_wave_state(self.directory, manifest, self.workers)
        self.worker_id = self.workers[0]["id"]
        helper.update_worker_state(
            self.directory,
            1,
            terminal_handle="term_worker",
            spawn_command=helper.spawn_command(
                helper.LAUNCH_SPECS[self.workers[0]["launch"]]
            ),
            start_status="running",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()
        self.environment.stop()

    def write_incoming(self, report: dict[str, object]) -> Path:
        path = helper.worker_report_path(self.directory, self.worker_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report), encoding="utf-8")
        return path

    def collect(self) -> int:
        with patch("builtins.print"):
            return helper.command_collect_reports(
                Namespace(receipt_dir=str(self.directory))
            )

    def test_done_report_settles_the_worker(self) -> None:
        self.write_incoming(valid_report())
        messages, actions = helper.scan_incoming_reports(self.directory)
        self.assertEqual(actions, 0)
        self.assertEqual(messages[0]["taskStatus"], "done")
        state = helper.read_wave_state(self.directory)["workers"][0]
        self.assertEqual(state["report_status"], "valid")
        self.assertEqual(state["task_status"], "done")
        self.assertEqual(state["start_status"], "completed")
        stored = helper.load_json(
            self.directory / "reports" / f"{self.worker_id}.json"
        )
        self.assertTrue(stored["accepted"])
        self.assertTrue(helper.wave_settled(helper.read_wave_state(self.directory)))

    def test_unchanged_report_is_processed_once(self) -> None:
        self.write_incoming(valid_report())
        helper.scan_incoming_reports(self.directory)
        messages, actions = helper.scan_incoming_reports(self.directory)
        self.assertEqual(messages, [])
        self.assertEqual(actions, 0)

    def test_blocked_report_needs_answer(self) -> None:
        self.write_incoming(
            valid_report(taskStatus="blocked", question="Which base branch wins?")
        )
        messages, actions = helper.scan_incoming_reports(self.directory)
        self.assertEqual(actions, 1)
        self.assertEqual(messages[0]["question"], "Which base branch wins?")
        state = helper.read_wave_state(self.directory)["workers"][0]
        self.assertEqual(state["start_status"], "blocked")
        self.assertFalse(helper.wave_settled(helper.read_wave_state(self.directory)))

    def test_blocked_without_question_is_invalid(self) -> None:
        self.write_incoming(valid_report(taskStatus="blocked"))
        messages, actions = helper.scan_incoming_reports(self.directory)
        self.assertEqual(actions, 1)
        self.assertTrue(messages[0]["reportErrors"])

    def test_final_report_replaces_a_blocked_one(self) -> None:
        self.write_incoming(
            valid_report(taskStatus="blocked", question="Which base branch wins?")
        )
        helper.scan_incoming_reports(self.directory)
        self.write_incoming(valid_report(summary="Finished after the answer."))
        messages, actions = helper.scan_incoming_reports(self.directory)
        self.assertEqual(actions, 0)
        state = helper.read_wave_state(self.directory)["workers"][0]
        self.assertEqual(state["task_status"], "done")
        self.assertIsNone(state["question"])

    def test_change_after_done_never_replaces_the_accepted_report(self) -> None:
        self.write_incoming(valid_report())
        helper.scan_incoming_reports(self.directory)
        self.write_incoming(valid_report(summary="Rewritten after completion."))
        messages, actions = helper.scan_incoming_reports(self.directory)
        self.assertEqual(actions, 1)
        self.assertTrue(messages[0].get("changedAfterDone"))
        stored = helper.load_json(
            self.directory / "reports" / f"{self.worker_id}.json"
        )
        self.assertEqual(stored["report"]["summary"], "Mapped the bounded surface.")

    def test_valid_rewrite_repairs_an_invalid_report(self) -> None:
        path = self.write_incoming(valid_report())
        path.write_text("not json", encoding="utf-8")
        messages, actions = helper.scan_incoming_reports(self.directory)
        self.assertEqual(actions, 1)
        self.assertEqual(
            helper.read_wave_state(self.directory)["workers"][0]["report_status"],
            "invalid",
        )
        self.write_incoming(valid_report())
        messages, actions = helper.scan_incoming_reports(self.directory)
        self.assertEqual(actions, 0)
        state = helper.read_wave_state(self.directory)["workers"][0]
        self.assertEqual(state["report_status"], "valid")

    def test_report_summary_is_clamped(self) -> None:
        self.write_incoming(valid_report(summary="x" * 10_000))
        messages, _ = helper.scan_incoming_reports(self.directory)
        self.assertLessEqual(
            len(messages[0]["summary"]), helper.MAX_BODY_OUTPUT_CHARS
        )
        self.assertTrue(messages[0]["truncated"])

    def test_collect_clears_the_wake_marker(self) -> None:
        helper.notification_path(self.directory).write_text("{}", encoding="utf-8")
        self.write_incoming(valid_report())
        self.assertEqual(self.collect(), 0)
        self.assertFalse(helper.notification_path(self.directory).exists())

    def test_notify_requires_the_report_file(self) -> None:
        with patch.dict(os.environ, {"ORCA_TERMINAL_HANDLE": "term_worker"}):
            with self.assertRaises(helper.HelperError) as context:
                helper.command_notify_controller(
                    Namespace(receipt_dir=str(self.directory))
                )
        self.assertIn("write your report", str(context.exception))

    def test_notify_queues_one_wake_and_names_collect(self) -> None:
        self.write_incoming(valid_report())
        queued = {"ok": True, "result": {"queued": True}}
        with (
            patch.dict(os.environ, {"ORCA_TERMINAL_HANDLE": "term_worker"}),
            patch.object(
                helper, "call_orca", return_value=(0, queued, "")
            ) as orca,
            patch("builtins.print"),
        ):
            result = helper.command_notify_controller(
                Namespace(receipt_dir=str(self.directory))
            )
        self.assertEqual(result, 0)
        send_arguments = orca.call_args.args[0]
        self.assertEqual(send_arguments[:2], ["terminal", "send"])
        self.assertIn("term_controller", send_arguments)
        self.assertIn("collect-reports", " ".join(send_arguments))
        self.assertTrue(helper.notification_path(self.directory).exists())
        worker = helper.read_wave_state(self.directory)["workers"][0]
        self.assertEqual(worker["notification_status"], "queued")

    def test_failed_wake_send_clears_the_coalescing_marker(self) -> None:
        self.write_incoming(valid_report())
        failed_send = {"ok": False, "error": {"state": "unknown"}}
        with (
            patch.dict(os.environ, {"ORCA_TERMINAL_HANDLE": "term_worker"}),
            patch.object(helper, "call_orca", return_value=(1, failed_send, "boom")),
            patch("builtins.print"),
        ):
            helper.command_notify_controller(Namespace(receipt_dir=str(self.directory)))
        self.assertFalse(helper.notification_path(self.directory).exists())

    def test_answer_reengages_the_same_terminal(self) -> None:
        self.write_incoming(
            valid_report(taskStatus="blocked", question="Which base branch wins?")
        )
        helper.scan_incoming_reports(self.directory)
        answer_file = self.directory / "sol-answer.txt"
        answer_file.write_text("Use main.", encoding="utf-8")
        sent = {"ok": True, "result": {"sent": True}}
        with (
            patch.object(helper, "call_orca", return_value=(0, sent, "")) as orca,
            patch("builtins.print"),
        ):
            result = helper.command_answer(
                Namespace(
                    receipt_dir=str(self.directory),
                    worker=self.worker_id,
                    file=str(answer_file),
                )
            )
        self.assertEqual(result, 0)
        send_arguments = orca.call_args.args[0]
        self.assertIn("term_worker", send_arguments)
        self.assertIn(str(answer_file), " ".join(send_arguments))
        state = helper.read_wave_state(self.directory)["workers"][0]
        self.assertEqual(state["start_status"], "running")
        self.assertIsNone(state["question"])
        self.assertEqual(state["answers"], 1)
        self.assertEqual(state["notification_status"], "pending")

    def test_runtime_prompt_names_report_file_and_wake(self) -> None:
        prompt = helper.runtime_prompt("ROLE: SCOUT\n", self.directory, self.worker_id)
        self.assertIn(
            str(helper.worker_report_path(self.directory, self.worker_id)), prompt
        )
        self.assertIn("notify-controller", prompt)
        self.assertIn("runtime/helper.py", prompt)
        self.assertNotIn("--wait", prompt)

    def test_foreign_helper_build_is_refused_with_archived_path(self) -> None:
        helper.mutate_wave_state(
            self.directory,
            lambda state: state.update({"helper_sha256": "0" * 64}),
        )
        with self.assertRaises(helper.HelperError) as context:
            helper.read_wave_state(self.directory)
        self.assertIn("runtime/helper.py", str(context.exception))


class LearnedRuleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manifest = helper.load_json(
            HELPER_PATH.parents[1] / "references" / "manifest-v2.example.json"
        )

    def test_known_failure_modes_render_into_every_prompt(self) -> None:
        manifest = {
            **self.manifest,
            "envelope": {
                **self.manifest["envelope"],
                "knownFailureModes": [
                    "[producer-proxy] Trace producer -> proxy -> consumer for each URL."
                ],
            },
        }
        _, _, prompts = helper.validate_manifest(manifest)
        self.assertIn("KNOWN FAILURE MODES RELEVANT TO THIS SCOPE", prompts[0])
        self.assertIn("[producer-proxy]", prompts[0])

    def test_known_failure_modes_caps_are_enforced(self) -> None:
        for bad in (
            ["r" * 241],
            ["rule"] * 7,
            ["r" * 220 for _ in range(5)],
        ):
            with self.assertRaises(helper.HelperError):
                helper.validate_manifest(
                    {
                        **self.manifest,
                        "envelope": {
                            **self.manifest["envelope"],
                            "knownFailureModes": bad,
                        },
                    }
                )

    def test_findings_render_readably_not_as_raw_json(self) -> None:
        manifest = {
            **self.manifest,
            "mode": "implementation",
            "envelope": {
                **self.manifest["envelope"],
                "reviewOverride": "repair wave for review findings",
            },
            "workers": [
                {
                    key: value
                    for key, value in {
                        **self.manifest["workers"][0],
                        "id": "fixer",
                        "role": "fixer",
                        "mutation": "allowed",
                        "findings": [
                            {
                                "severity": "high",
                                "title": "Hidden lease deadlocks headless launch",
                                "evidence": "SyncOutcome retains a non-reentrant lease.",
                            },
                            "plain string finding",
                        ],
                    }.items()
                    if key != "launch"
                }
            ],
        }
        _, _, prompts = helper.validate_manifest(manifest)
        self.assertIn(
            "- [high] Hidden lease deadlocks headless launch", prompts[0]
        )
        self.assertIn("  SyncOutcome retains a non-reentrant lease.", prompts[0])
        self.assertIn("- plain string finding", prompts[0])
        self.assertNotIn('"severity"', prompts[0].split("REPORT")[0])

    def test_prompt_feedback_validates_in_reports(self) -> None:
        good = valid_report(
            promptFeedback=[
                {
                    "failureClass": "progress accounting",
                    "rule": "Define progress units and emit them in monotonic order.",
                    "severity": "high",
                    "scopes": ["frontend"],
                }
            ],
            ruleFeedback=[{"id": "per-object-fsync", "status": "violated"}],
        )
        self.assertEqual(helper.validate_report(good, "scout"), [])
        for bad_entry in (
            [{"failureClass": "x", "rule": "y", "severity": "urgent", "scopes": ["a"]}],
            [{"failureClass": "x", "rule": "y" * 300, "severity": "high", "scopes": ["a"]}],
            [{"failureClass": "x", "rule": "y", "severity": "high", "scopes": []}],
            [{}] * 4,
        ):
            bad = valid_report(promptFeedback=bad_entry)
            self.assertNotEqual(helper.validate_report(bad, "scout"), [])
        bad_rule_feedback = valid_report(ruleFeedback=[{"id": "x", "status": "kept"}])
        self.assertNotEqual(helper.validate_report(bad_rule_feedback, "scout"), [])


class LaunchPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manifest = helper.load_json(
            HELPER_PATH.parents[1] / "references" / "manifest-v2.example.json"
        )

    def test_review_roles_are_pinned_to_sol_xhigh(self) -> None:
        manifest = {
            **self.manifest,
            "workers": [{**self.manifest["workers"][0], "role": "reviewer"}],
        }
        _, workers, _ = helper.validate_manifest(manifest)
        self.assertEqual(workers[0]["launch"], "sol-xhigh")
        with self.assertRaises(helper.HelperError):
            helper.validate_manifest(
                {
                    **self.manifest,
                    "workers": [
                        {
                            **self.manifest["workers"][0],
                            "role": "reviewer",
                            "launch": "luna-max",
                        }
                    ],
                }
            )

    def test_implementation_defaults_to_luna_max(self) -> None:
        _, workers, _ = helper.validate_manifest(self.manifest)
        self.assertEqual(workers[0]["launch"], "luna-max")

    def test_spawn_commands_carry_exact_model_and_effort(self) -> None:
        self.assertEqual(
            helper.spawn_command(helper.LAUNCH_SPECS["sol-xhigh"]),
            "codex -m gpt-5.6-sol -c model_reasoning_effort=xhigh",
        )
        self.assertEqual(
            helper.spawn_command(helper.LAUNCH_SPECS["fable-high"]),
            "claude --model claude-fable-5",
        )
        self.assertIn(
            "service_tier=priority",
            helper.spawn_command(helper.LAUNCH_SPECS["luna-fast"]),
        )


class ContractTests(unittest.TestCase):
    def test_no_orchestration_commands_remain(self) -> None:
        self.assertFalse(
            any(
                name.startswith("orchestration")
                for name in helper.REQUIRED_ORCA_COMMANDS
            )
        )

    def test_contract_requires_parsed_argv(self) -> None:
        commands = [
            {
                "command": name,
                "argumentMode": "parsed",
                "usage": name,
                "flags": sorted(flags),
            }
            for name, flags in helper.REQUIRED_ORCA_COMMANDS.items()
        ]
        commands[0]["argumentMode"] = "legacy-shell"
        checks, _ = helper.command_contract_check({"commands": commands})
        check = next(
            item
            for item in checks
            if item["name"] == f"command:{commands[0]['command']}"
        )
        self.assertFalse(check["passed"])

    def test_structured_error_without_effects_is_not_ambiguous(self) -> None:
        payload = {
            "ok": False,
            "error": {"code": "selector_not_found", "message": "missing"},
            "effects": {},
            "residualResources": [],
        }
        self.assertEqual(helper.classify_failure(payload), "rejected_no_effects")

    def test_exact_launch_proven_compares_spawn_commands(self) -> None:
        record = {
            "launch": "sol-xhigh",
            "spawn_command": helper.spawn_command(helper.LAUNCH_SPECS["sol-xhigh"]),
        }
        self.assertTrue(helper.exact_launch_proven(record))
        record["spawn_command"] = "codex -m gpt-5.6-luna"
        self.assertFalse(helper.exact_launch_proven(record))


if __name__ == "__main__":
    unittest.main()
