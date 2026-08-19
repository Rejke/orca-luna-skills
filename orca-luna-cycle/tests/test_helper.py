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

    def notify_orca(self, script: dict[str, tuple[int, object, str]]):
        """call_orca stub keyed by subcommand; unknown commands succeed."""

        def fake(arguments: list[str]):
            return script.get(arguments[1], (0, {"ok": True}, ""))

        return patch.object(helper, "call_orca", side_effect=fake)

    def test_notify_verifies_delivery_by_reading_the_banner(self) -> None:
        self.write_incoming(valid_report())
        read_ok = {"ok": True, "result": {"text": "... [ORCA LUNA CYCLE: REPORTS READY] ..."}}
        with (
            patch.dict(os.environ, {"ORCA_TERMINAL_HANDLE": "term_worker"}),
            self.notify_orca(
                {
                    "send": (0, {"ok": True, "result": {"queued": True}}, ""),
                    "read": (0, read_ok, ""),
                }
            ) as orca,
            patch("builtins.print"),
        ):
            result = helper.command_notify_controller(
                Namespace(receipt_dir=str(self.directory))
            )
        self.assertEqual(result, 0)
        commands = [call.args[0][1] for call in orca.call_args_list]
        self.assertEqual(commands, ["wait", "send", "read"])
        send_arguments = orca.call_args_list[1].args[0]
        self.assertIn("term_controller", send_arguments)
        self.assertIn("collect-reports", " ".join(send_arguments))
        self.assertTrue(helper.notification_path(self.directory).exists())
        worker = helper.read_wave_state(self.directory)["workers"][0]
        self.assertEqual(worker["notification_status"], "delivered")

    def test_unverified_wake_retries_once_then_clears_the_marker(self) -> None:
        self.write_incoming(valid_report())
        read_empty = {"ok": True, "result": {"text": "shell prompt only"}}
        with (
            patch.dict(os.environ, {"ORCA_TERMINAL_HANDLE": "term_worker"}),
            self.notify_orca(
                {
                    "send": (0, {"ok": True, "result": {"queued": True}}, ""),
                    "read": (0, read_empty, ""),
                }
            ) as orca,
            patch("builtins.print"),
        ):
            helper.command_notify_controller(Namespace(receipt_dir=str(self.directory)))
        commands = [call.args[0][1] for call in orca.call_args_list]
        self.assertEqual(commands.count("send"), 2)
        self.assertFalse(helper.notification_path(self.directory).exists())
        worker = helper.read_wave_state(self.directory)["workers"][0]
        self.assertEqual(worker["notification_status"], "send_unverified")

    def test_failed_wake_send_clears_the_coalescing_marker(self) -> None:
        self.write_incoming(valid_report())
        failed_send = {"ok": False, "error": {"state": "unknown"}}
        with (
            patch.dict(os.environ, {"ORCA_TERMINAL_HANDLE": "term_worker"}),
            self.notify_orca({"send": (1, failed_send, "boom")}),
            patch("builtins.print"),
        ):
            helper.command_notify_controller(Namespace(receipt_dir=str(self.directory)))
        self.assertFalse(helper.notification_path(self.directory).exists())

    def test_rebind_controller_updates_the_wake_target(self) -> None:
        with (
            patch.dict(os.environ, {"ORCA_TERMINAL_HANDLE": "term_new_controller"}),
            patch("builtins.print"),
        ):
            result = helper.command_rebind_controller(
                Namespace(receipt_dir=str(self.directory))
            )
        self.assertEqual(result, 0)
        state = helper.read_wave_state(self.directory)
        self.assertEqual(
            state["controller_terminal_handle"], "term_new_controller"
        )

    def test_rebind_refuses_a_worker_terminal(self) -> None:
        with (
            patch.dict(os.environ, {"ORCA_TERMINAL_HANDLE": "term_worker"}),
            patch("builtins.print"),
        ):
            with self.assertRaises(helper.HelperError):
                helper.command_rebind_controller(
                    Namespace(receipt_dir=str(self.directory))
                )

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

    def test_collect_replay_keeps_showing_standing_attention(self) -> None:
        self.write_incoming(
            valid_report(taskStatus="blocked", question="Which base branch wins?")
        )
        for _ in (1, 2):
            with patch("builtins.print") as printed:
                helper.command_collect_reports(
                    Namespace(receipt_dir=str(self.directory))
                )
            out = json.loads(printed.call_args.args[0])
            self.assertEqual(out["status"], "action_required")
            self.assertEqual(
                out["attention"][0]["question"], "Which base branch wins?"
            )

    def test_stale_wake_marker_is_taken_over(self) -> None:
        helper.notification_path(self.directory).write_text(
            json.dumps({"createdAt": 1.0}), encoding="utf-8"
        )
        self.write_incoming(valid_report())
        read_ok = {"ok": True, "result": {"text": "[ORCA LUNA CYCLE: REPORTS READY]"}}
        with (
            patch.dict(os.environ, {"ORCA_TERMINAL_HANDLE": "term_worker"}),
            self.notify_orca(
                {
                    "send": (0, {"ok": True, "result": {"queued": True}}, ""),
                    "read": (0, read_ok, ""),
                }
            ) as orca,
            patch("builtins.print"),
        ):
            helper.command_notify_controller(
                Namespace(receipt_dir=str(self.directory))
            )
        self.assertTrue(
            any(c.args[0][:2] == ["terminal", "send"] for c in orca.call_args_list)
        )
        worker = helper.read_wave_state(self.directory)["workers"][0]
        self.assertEqual(worker["notification_status"], "delivered")

    def test_answer_refuses_a_worker_that_is_not_blocked(self) -> None:
        self.write_incoming(valid_report())
        helper.scan_incoming_reports(self.directory)
        answer_file = self.directory / "sol-answer.txt"
        answer_file.write_text("Use main.", encoding="utf-8")
        with self.assertRaises(helper.HelperError) as context:
            helper.command_answer(
                Namespace(
                    receipt_dir=str(self.directory),
                    worker=self.worker_id,
                    file=str(answer_file),
                )
            )
        self.assertIn("not blocked", str(context.exception))

    def test_stop_treats_a_gone_terminal_as_stopped(self) -> None:
        gone = {"ok": False, "error": {"code": "selector_not_found"}}
        with (
            patch.object(helper, "call_orca", return_value=(1, gone, "selector_not_found")),
            patch("builtins.print"),
        ):
            result = helper.reconcile_stop_wave(self.directory)
        self.assertEqual(result["status"], "cancelled")
        state = helper.read_wave_state(self.directory)["workers"][0]
        self.assertEqual(state["stop_status"], "stopped")

    def test_status_marks_idle_worker_without_report_as_suspect(self) -> None:
        idle = {"ok": True}
        with (
            patch.object(helper, "call_orca", return_value=(0, idle, "")),
            patch("builtins.print") as printed,
        ):
            helper.command_status(Namespace(receipt_dir=str(self.directory)))
        out = json.loads(printed.call_args.args[0])
        self.assertEqual(out["suspects"], 1)
        self.assertTrue(out["workers"][0]["suspect"])
        self.write_incoming(valid_report())
        with (
            patch.object(helper, "call_orca", return_value=(0, idle, "")),
            patch("builtins.print") as printed,
        ):
            helper.command_status(Namespace(receipt_dir=str(self.directory)))
        out = json.loads(printed.call_args.args[0])
        self.assertEqual(out["suspects"], 0)

    def test_settled_report_records_a_timestamp(self) -> None:
        self.write_incoming(valid_report())
        helper.scan_incoming_reports(self.directory)
        state = helper.read_wave_state(self.directory)["workers"][0]
        self.assertIsInstance(state.get("settled_at"), float)

    def test_finalize_closes_worker_terminals_when_clean(self) -> None:
        self.write_incoming(valid_report())
        helper.scan_incoming_reports(self.directory)
        helper.save_json(self.directory / "preflight.json", {"status": "passed"})
        closed = {"ok": True}
        with (
            patch.object(helper, "call_orca", return_value=(0, closed, "")) as orca,
            patch.object(
                helper,
                "anchor_check",
                return_value={"status": "verified", "passed": True},
            ),
            patch("builtins.print"),
        ):
            result = helper.command_finalize_wave(
                Namespace(receipt_dir=str(self.directory))
            )
        self.assertEqual(result, 0)
        close_calls = [
            call.args[0]
            for call in orca.call_args_list
            if call.args[0][:2] == ["terminal", "close"]
        ]
        self.assertEqual(len(close_calls), 1)
        state = helper.read_wave_state(self.directory)["workers"][0]
        self.assertEqual(state["stop_status"], "closed")

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
        self.assertIn("KNOWN FAILURE MODES", prompts[0])
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
            "codex --dangerously-bypass-approvals-and-sandbox "
            "-m gpt-5.6-sol -c model_reasoning_effort=xhigh",
        )
        self.assertEqual(
            helper.spawn_command(helper.LAUNCH_SPECS["fable-high"]),
            "claude --dangerously-skip-permissions --model claude-fable-5",
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


class AnchorBaselineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="orca-luna-anchor-")
        self.directory = Path(self.temporary.name)
        self.manifest = {
            "envelope": {},
            "workers": [{"worktree": "current"}],
        }
        self.state = {"workers": [{"mutation": "forbidden"}]}
        self.baseline = {
            "status": "captured",
            "head": "abc123",
            "dirtyState": [" M skills-lock.json", "?? notes.md"],
        }

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write_preflight(self, baseline: dict[str, object] | None) -> None:
        receipt: dict[str, object] = {"status": "passed"}
        if baseline is not None:
            receipt["gitBaseline"] = baseline
        helper.save_json(self.directory / "preflight.json", receipt)

    def test_measured_baseline_preserved_passes_without_manifest_anchor(self) -> None:
        self.write_preflight(self.baseline)
        with patch.object(helper, "git_snapshot", return_value=dict(self.baseline)):
            result = helper.anchor_check(self.directory, self.manifest, self.state)
        self.assertTrue(result["passed"])
        self.assertEqual(result["status"], "verified")

    def test_measured_baseline_detects_dirty_drift(self) -> None:
        self.write_preflight(self.baseline)
        drifted = dict(self.baseline, dirtyState=[" M skills-lock.json"])
        with patch.object(helper, "git_snapshot", return_value=drifted):
            result = helper.anchor_check(self.directory, self.manifest, self.state)
        self.assertFalse(result["passed"])
        self.assertEqual(result["status"], "changed")

    def test_legacy_wave_without_baseline_uses_declared_anchor(self) -> None:
        self.write_preflight(None)
        manifest = {
            "envelope": {"baseAnchor": "abc123", "dirtyState": []},
            "workers": [{"worktree": "current"}],
        }
        snapshot = {"status": "captured", "head": "abc123", "dirtyState": []}
        with patch.object(helper, "git_snapshot", return_value=snapshot):
            result = helper.anchor_check(self.directory, manifest, self.state)
        self.assertTrue(result["passed"])


class WorkerFailureModeTests(unittest.TestCase):
    def base_manifest(self) -> dict:
        return json.loads(
            json.dumps(
                helper.load_json(
                    HELPER_PATH.parents[1]
                    / "references"
                    / "manifest-v2.example.json"
                )
            )
        )

    def test_worker_rules_replace_envelope_rules_in_the_prompt(self) -> None:
        manifest = self.base_manifest()
        manifest["envelope"]["knownFailureModes"] = ["[global] envelope rule"]
        manifest["workers"][0]["knownFailureModes"] = ["[local] worker rule"]
        _, workers, prompts = helper.validate_manifest(manifest)
        self.assertIn("[local] worker rule", prompts[0])
        self.assertNotIn("[global] envelope rule", prompts[0])
        for prompt in prompts[1:]:
            self.assertIn("[global] envelope rule", prompt)

    def test_worker_rules_share_the_size_caps(self) -> None:
        manifest = self.base_manifest()
        manifest["workers"][0]["knownFailureModes"] = ["x" * 241]
        with self.assertRaises(helper.HelperError):
            helper.validate_manifest(manifest)


class DependsOnTests(unittest.TestCase):
    def manifest(self, workers: list[dict]) -> dict:
        return {
            "schemaVersion": 2,
            "mode": "implementation",
            "objective": "Depends-on validation.",
            "envelope": {
                "goal": "A user sees the probe finish.",
                "acceptanceCriteria": {"AC1": "probe has a deadline."},
                "reviewedAnchor": "abc123",
                "baseAnchor": "abc123",
                "repairBudget": 1,
            },
            "defaults": {"worktree": "current"},
            "workers": workers,
        }

    def worker(self, wid: str, role: str = "implementer", **extra) -> dict:
        mutation = "allowed" if role in {"implementer", "fixer"} else "forbidden"
        return {
            "id": wid,
            "role": role,
            "goal": f"{wid} goal",
            "criteria": ["AC1"],
            "scope": ["src/a.ts"],
            "mutation": mutation,
            **extra,
        }

    def test_sequenced_mutators_may_share_current(self) -> None:
        manifest = self.manifest(
            [
                self.worker("impl"),
                self.worker("fix", role="fixer", dependsOn=["impl"]),
            ]
        )
        _, workers, _ = helper.validate_manifest(manifest)
        self.assertEqual(workers[1]["dependsOn"], ["impl"])

    def test_parallel_shared_mutators_are_rejected(self) -> None:
        manifest = self.manifest(
            [self.worker("a"), self.worker("b")]
        )
        with self.assertRaises(helper.HelperError) as ctx:
            helper.validate_manifest(manifest)
        self.assertIn("dependsOn ordering", str(ctx.exception))

    def test_unknown_dependency_is_rejected(self) -> None:
        manifest = self.manifest([self.worker("a", dependsOn=["ghost"])])
        with self.assertRaises(helper.HelperError):
            helper.validate_manifest(manifest)

    def test_cycle_is_rejected(self) -> None:
        manifest = self.manifest(
            [
                self.worker("a", dependsOn=["b"]),
                {**self.worker("b", role="reviewer"), "dependsOn": ["a"]},
            ]
        )
        with self.assertRaises(helper.HelperError) as ctx:
            helper.validate_manifest(manifest)
        self.assertIn("cycle", str(ctx.exception))

    def test_dependent_worker_initializes_as_waiting(self) -> None:
        manifest = self.manifest(
            [
                self.worker("impl"),
                self.worker("fix", role="fixer", dependsOn=["impl"]),
            ]
        )
        normalized, workers, _ = helper.validate_manifest(manifest)
        with tempfile.TemporaryDirectory(prefix="orca-luna-dep-") as name,              patch.dict(os.environ, {"ORCA_TERMINAL_HANDLE": "term_controller"}):
            directory = Path(name)
            helper.ensure_journal(directory)
            helper.save_json(directory / "manifest.json", normalized)
            helper.initialize_wave_state(directory, normalized, workers)
            records = helper.read_wave_state(directory)["workers"]
            self.assertEqual(records[0]["start_status"], "pending")
            self.assertEqual(records[1]["start_status"], "waiting")
            self.assertEqual(records[1]["depends_on"], ["impl"])

    def dependent_wave(self, name: str):
        manifest = self.manifest(
            [
                self.worker("impl"),
                self.worker("fix", role="fixer", dependsOn=["impl"]),
            ]
        )
        normalized, workers, prompts = helper.validate_manifest(manifest)
        directory = Path(name)
        helper.ensure_journal(directory)
        helper.save_json(directory / "manifest.json", normalized)
        helper.initialize_wave_state(directory, normalized, workers)
        helper.write_prompts(directory, workers, prompts)
        helper.update_worker_state(
            directory,
            1,
            terminal_handle="term_impl",
            spawn_command=helper.spawn_command(helper.LAUNCH_SPECS["luna-max"]),
            start_status="running",
        )
        return directory

    def test_collect_spawns_the_dependent_after_a_done_report(self) -> None:
        with tempfile.TemporaryDirectory(prefix="orca-luna-dep-") as name,              patch.dict(os.environ, {"ORCA_TERMINAL_HANDLE": "term_controller"}):
            directory = self.dependent_wave(name)
            report_path = helper.worker_report_path(directory, "impl")
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(
                json.dumps(valid_report(commit=None, shards=None)), encoding="utf-8"
            )

            def fake(arguments):
                command = arguments[1]
                if command == "create":
                    return (
                        0,
                        {"result": {"terminal": {"handle": "term_fix"}}},
                        "",
                    )
                if command == "read":
                    return (0, {"result": {"text": "gpt-5.6-luna banner"}}, "")
                return (0, {"ok": True}, "")

            with patch.object(helper, "call_orca", side_effect=fake),                  patch("builtins.print"):
                helper.command_collect_reports(
                    Namespace(receipt_dir=str(directory))
                )
            records = helper.read_wave_state(directory)["workers"]
            self.assertEqual(records[1]["terminal_handle"], "term_fix")
            self.assertEqual(records[1]["start_status"], "running")

    def test_failed_dependency_cascades(self) -> None:
        with tempfile.TemporaryDirectory(prefix="orca-luna-dep-") as name,              patch.dict(os.environ, {"ORCA_TERMINAL_HANDLE": "term_controller"}):
            directory = self.dependent_wave(name)
            report_path = helper.worker_report_path(directory, "impl")
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(
                json.dumps(
                    valid_report(taskStatus="failed", commit=None, shards=None)
                ),
                encoding="utf-8",
            )
            with patch.object(
                helper, "call_orca", return_value=(0, {"ok": True}, "")
            ), patch("builtins.print"):
                helper.command_collect_reports(
                    Namespace(receipt_dir=str(directory))
                )
            records = helper.read_wave_state(directory)["workers"]
            self.assertEqual(records[1]["start_status"], "dep_failed")
            self.assertEqual(records[1]["task_status"], "failed")
            self.assertTrue(
                helper.wave_settled(helper.read_wave_state(directory))
            )


class BundleTests(unittest.TestCase):
    def test_bundle_matches_parts(self) -> None:
        scripts = HELPER_PATH.parent
        pieces: list[str] = []
        for part in sorted((scripts / "parts").glob("*.py")):
            text = part.read_text(encoding="utf-8")
            if not text.endswith("\n"):
                text += "\n"
            if pieces:
                pieces.append(f"# ==== part: {part.name} ====\n")
            pieces.append(text)
        self.assertEqual(
            "".join(pieces),
            HELPER_PATH.read_text(encoding="utf-8"),
            "bundle differs from parts/; run scripts/build_helper.py",
        )


class PlanReviewManifestTests(unittest.TestCase):
    def test_generated_manifest_validates_with_fixed_acs(self) -> None:
        with tempfile.TemporaryDirectory(prefix="orca-luna-planrev-") as name:
            plan = Path(name) / "plan.md"
            plan.write_text("# Plan\nDo the thing.\n", encoding="utf-8")
            prior = Path(name) / "prior.json"
            prior.write_text("{}", encoding="utf-8")
            printed: list[str] = []
            with patch("builtins.print", side_effect=lambda *a, **k: printed.append(a[0])):
                code = helper.command_plan_review_manifest(
                    Namespace(
                        plan=str(plan),
                        prior=[str(prior)],
                        prior_plan=None,
                        hint=["check the migration mapping"],
                        worker_id="plan_reviewer",
                    )
                )
            self.assertEqual(code, 0)
            manifest = json.loads(printed[0])
            _, workers, _ = helper.validate_manifest(manifest)
            worker = workers[0]
            self.assertEqual(worker["role"], "planreviewer")
            self.assertEqual(worker["launch"], "sol-xhigh")
            self.assertIn(str(plan.resolve()), worker["scope"])
            self.assertEqual(worker["criteria"], ["AC1", "AC2"])
            self.assertEqual(
                manifest["envelope"]["acceptanceCriteria"]["AC2"],
                helper.PLAN_REVIEW_AC_PRIORS,
            )
            self.assertEqual(
                manifest["envelope"]["goal"], helper.PLAN_REVIEW_MISSION
            )
            self.assertEqual(
                worker["context"],
                "Hints, not acceptance: check the migration mapping",
            )

    def test_no_priors_drops_the_prior_criterion(self) -> None:
        with tempfile.TemporaryDirectory(prefix="orca-luna-planrev-") as name:
            plan = Path(name) / "plan.md"
            plan.write_text("# Plan\n", encoding="utf-8")
            printed: list[str] = []
            with patch("builtins.print", side_effect=lambda *a, **k: printed.append(a[0])):
                helper.command_plan_review_manifest(
                    Namespace(
                        plan=str(plan),
                        prior=None,
                        prior_plan=None,
                        hint=None,
                        worker_id="plan_reviewer",
                    )
                )
            manifest = json.loads(printed[0])
            self.assertEqual(
                list(manifest["envelope"]["acceptanceCriteria"]), ["AC1"]
            )

    def test_prior_plan_requires_prior_report_and_lands_in_scope(self) -> None:
        with tempfile.TemporaryDirectory(prefix="orca-luna-planrev-") as name:
            plan = Path(name) / "plan.md"
            plan.write_text("# Plan v2\n", encoding="utf-8")
            old_plan = Path(name) / "old-plan.md"
            old_plan.write_text("# Plan v1\n", encoding="utf-8")
            prior = Path(name) / "prior.json"
            prior.write_text("{}", encoding="utf-8")
            with self.assertRaises(helper.HelperError):
                helper.command_plan_review_manifest(
                    Namespace(
                        plan=str(plan),
                        prior=None,
                        prior_plan=[str(old_plan)],
                        hint=None,
                        worker_id="plan_reviewer",
                    )
                )
            printed: list[str] = []
            with patch("builtins.print", side_effect=lambda *a, **k: printed.append(a[0])):
                helper.command_plan_review_manifest(
                    Namespace(
                        plan=str(plan),
                        prior=[str(prior)],
                        prior_plan=[str(old_plan)],
                        hint=None,
                        worker_id="plan_reviewer",
                    )
                )
            manifest = json.loads(printed[0])
            scope = manifest["workers"][0]["scope"]
            self.assertEqual(
                scope,
                [
                    str(plan.resolve()),
                    str(old_plan.resolve()),
                    str(prior.resolve()),
                ],
            )
            self.assertIn("sha256", manifest["objective"])

    def test_snapshot_copies_only_external_scope_files(self) -> None:
        with tempfile.TemporaryDirectory(prefix="orca-luna-snap-") as name:
            root = Path(name)
            repo = root / "repo"
            (repo / "src").mkdir(parents=True)
            repo_file = repo / "src" / "main.ts"
            repo_file.write_text("code", encoding="utf-8")
            external = root / "plan.md"
            external.write_text("# Plan\n", encoding="utf-8")
            receipts = root / "receipts"
            receipts.mkdir()
            workers = [
                {"scope": [str(external), str(repo_file), "packages/x.ts", 7]}
            ]
            copies = helper.snapshot_external_scope(
                receipts, workers, repo_root=repo
            )
            self.assertEqual(list(copies), [str(external)])
            copied = Path(copies[str(external)])
            self.assertTrue(copied.is_file())
            self.assertEqual(copied.read_text(encoding="utf-8"), "# Plan\n")
            self.assertTrue(copied.name.endswith("-plan.md"))


class UsageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="orca-luna-usage-")
        self.root = Path(self.temporary.name)
        self.directory = self.root / "receipts"
        (self.directory / "terminals").mkdir(parents=True)
        self.worktree = "/home/user/project"
        self.now = 1_800_000_000.0
        helper.save_json(
            self.directory / "terminals" / "w1.json",
            {
                "result": {
                    "terminal": {
                        "handle": "term_w1",
                        "worktreeId": "id-1::\\\\wsl.localhost\\Debian\\home\\user\\project",
                    }
                }
            },
        )
        os.utime(
            self.directory / "terminals" / "w1.json", (self.now - 50, self.now - 50)
        )
        self.state = {
            "created_at": self.now - 60,
            "workers": [
                {
                    "index": 1,
                    "worker_id": "w1",
                    "role": "implementer",
                    "launch": "luna-max",
                    "started_at": self.now - 40,
                    "settled_at": self.now + 100,
                }
            ],
        }

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def iso(self, stamp: float) -> str:
        import datetime as dt

        return (
            dt.datetime.fromtimestamp(stamp, tz=dt.timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )

    def write_rollout(self, cwd: str, started: float) -> Path:
        day = self.root / "codex-sessions" / "2026" / "08" / "19"
        day.mkdir(parents=True, exist_ok=True)
        path = day / f"rollout-x-{started:.0f}-{abs(hash(cwd)) % 10_000}.jsonl"
        lines = [
            {
                "type": "session_meta",
                "payload": {"cwd": cwd, "timestamp": self.iso(started)},
            },
            {
                "type": "turn_context",
                "payload": {
                    "model": "gpt-5.6-luna",
                    "collaboration_mode": {
                        "settings": {"reasoning_effort": "max"}
                    },
                },
            },
            {
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "total_token_usage": {
                            "input_tokens": 1000,
                            "cached_input_tokens": 600,
                            "cache_write_input_tokens": 0,
                            "output_tokens": 200,
                            "reasoning_output_tokens": 150,
                            "total_tokens": 1200,
                        }
                    },
                    "rate_limits": {
                        "plan_type": "pro",
                        "primary": {"used_percent": 42.5},
                    },
                },
            },
        ]
        path.write_text(
            "\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8"
        )
        os.utime(path, (started, started))
        return path

    def test_worktree_id_path_converts_wsl_unc(self) -> None:
        self.assertEqual(
            helper.worktree_id_path(
                "id::\\\\wsl.localhost\\Debian\\home\\user\\project"
            ),
            "/home/user/project",
        )
        self.assertEqual(helper.worktree_id_path("id::/home/user/x"), "/home/user/x")
        self.assertIsNone(helper.worktree_id_path("no-separator"))

    def test_codex_session_matched_by_cwd_and_window(self) -> None:
        self.write_rollout(self.worktree, self.now - 45)
        self.write_rollout("/other/project", self.now - 45)
        usage = helper.wave_usage(
            self.directory,
            self.state,
            codex_roots=[self.root / "codex-sessions"],
            claude_root=self.root / "claude-missing",
            now=self.now + 200,
        )
        row = usage["workers"][0]
        self.assertEqual(row["match"], "exact")
        self.assertEqual(row["tokens"]["total"], 1200)
        self.assertEqual(row["tokens"]["output"], 200)
        self.assertEqual(row["model"], "gpt-5.6-luna")
        self.assertEqual(row["effort"], "max")
        self.assertEqual(row["serviceTier"], "standard")
        self.assertEqual(row["quota"]["endPercent"], 42.5)
        self.assertEqual(row["wallSeconds"], 140)
        self.assertEqual(usage["byLaunch"]["luna-max"]["totalTokens"], 1200)

    def test_session_outside_window_stays_unmatched(self) -> None:
        self.write_rollout(self.worktree, self.now - 3000)
        usage = helper.wave_usage(
            self.directory,
            self.state,
            codex_roots=[self.root / "codex-sessions"],
            claude_root=self.root / "claude-missing",
            now=self.now + 200,
        )
        self.assertEqual(usage["workers"][0]["match"], "none")
        self.assertIsNone(usage["workers"][0]["tokens"])

    def test_claude_session_sums_request_usage(self) -> None:
        state = {
            "created_at": self.now - 60,
            "workers": [
                {
                    "index": 1,
                    "worker_id": "w1",
                    "role": "implementer",
                    "launch": "fable-high",
                    "started_at": self.now - 40,
                    "settled_at": self.now + 100,
                }
            ],
        }
        project = self.root / "claude-projects" / "-home-user-project"
        project.mkdir(parents=True)
        lines = [
            {
                "type": "user",
                "cwd": self.worktree,
                "timestamp": self.iso(self.now - 30),
            },
            {
                "type": "assistant",
                "timestamp": self.iso(self.now - 20),
                "message": {
                    "model": "claude-fable-5",
                    "usage": {
                        "input_tokens": 100,
                        "cache_read_input_tokens": 50,
                        "cache_creation_input_tokens": 10,
                        "output_tokens": 20,
                    },
                },
            },
            {
                "type": "assistant",
                "timestamp": self.iso(self.now - 10),
                "message": {
                    "model": "claude-fable-5",
                    "usage": {
                        "input_tokens": 200,
                        "cache_read_input_tokens": 150,
                        "cache_creation_input_tokens": 0,
                        "output_tokens": 40,
                    },
                },
            },
        ]
        (project / "session.jsonl").write_text(
            "\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8"
        )
        os.utime(project / "session.jsonl", (self.now, self.now))
        usage = helper.wave_usage(
            self.directory,
            state,
            codex_roots=[self.root / "codex-missing"],
            claude_root=self.root / "claude-projects",
            now=self.now + 200,
        )
        row = usage["workers"][0]
        self.assertEqual(row["match"], "exact")
        self.assertEqual(row["tokens"]["total"], 570)
        self.assertEqual(row["tokens"]["output"], 60)
        self.assertEqual(row["model"], "claude-fable-5")

    def test_usage_log_appends_each_worker_once(self) -> None:
        self.write_rollout(self.worktree, self.now - 45)
        usage = helper.wave_usage(
            self.directory,
            self.state,
            codex_roots=[self.root / "codex-sessions"],
            claude_root=self.root / "claude-missing",
            now=self.now + 200,
        )
        log_path = self.root / "log" / "usage.jsonl"
        self.assertEqual(helper.append_usage_log(usage, log_path), 1)
        self.assertEqual(helper.append_usage_log(usage, log_path), 0)
        entry = json.loads(log_path.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(entry["workerId"], "w1")
        self.assertEqual(entry["runId"], "receipts")


if __name__ == "__main__":
    unittest.main()
