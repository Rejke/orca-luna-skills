#!/usr/bin/env python3
"""Offline regression tests for the Orca Luna Cycle helper."""

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


def delivery(payload: dict[str, object], **message: object) -> dict[str, object]:
    return {
        "result": {
            "deliveryId": "delivery_test",
            "messages": [
                {
                    "id": "msg_test",
                    "type": "worker_done",
                    "body": "Did the work. Found the result. Nothing remains.",
                    "payload": json.dumps(payload),
                    **message,
                }
            ],
        }
    }


class DeliveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.environment = patch.dict(
            os.environ, {"ORCA_TERMINAL_HANDLE": "term_controller"}
        )
        self.environment.start()
        self.temporary = tempfile.TemporaryDirectory(prefix="orca-luna-test-")
        self.directory = Path(self.temporary.name)
        manifest, workers, _ = helper.validate_manifest(
            helper.load_json(
                HELPER_PATH.parents[1] / "references" / "manifest-v2.example.json"
            )
        )
        helper.ensure_journal(self.directory)
        helper.save_json(self.directory / "manifest.json", manifest)
        helper.initialize_wave_state(self.directory, manifest, workers)
        helper.update_worker_state(
            self.directory,
            1,
            task_id="task_expected",
            dispatch_id="ctx_expected",
            terminal_handle="term_worker",
            start_status="running",
        )
        helper.mutate_wave_state(
            self.directory,
            lambda state: state.update({"run_id": "run_test", "run_status": "ready"}),
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()
        self.environment.stop()

    def test_worker_done_identity_comes_from_string_payload(self) -> None:
        payload = valid_report(
            taskId="task_expected",
            dispatchId="ctx_expected",
            outcome="succeeded",
        )
        release = {"ok": True, "result": {"state": "released"}}
        with patch.object(helper, "call_orca", return_value=(0, release, "")):
            result = helper.process_delivery(self.directory, delivery(payload), "test")

        message = result["messages"][0]
        self.assertEqual(message["taskId"], "task_expected")
        self.assertEqual(message["dispatchId"], "ctx_expected")
        self.assertEqual(message["outcome"], "succeeded")
        self.assertTrue(message["accepted"])
        self.assertEqual(message["reportErrors"], [])
        state = helper.read_wave_state(self.directory)["workers"][0]
        self.assertEqual(state["start_status"], "completed")
        self.assertEqual(state["release_status"], "released")

    def test_identity_conflict_never_releases(self) -> None:
        payload = valid_report(
            taskId="task_expected",
            dispatchId="ctx_expected",
            outcome="succeeded",
        )
        with patch.object(helper, "call_orca") as orca:
            result = helper.process_delivery(
                self.directory,
                delivery(payload, taskId="task_wrong"),
                "test",
            )

        message = result["messages"][0]
        self.assertFalse(message["accepted"])
        self.assertEqual(message["rejectionCode"], "identity_conflict")
        orca.assert_not_called()

    def test_report_validation_is_independent_from_identity(self) -> None:
        payload = valid_report(
            taskId="task_unknown",
            dispatchId="ctx_unknown",
            outcome="succeeded",
        )
        result = helper.process_delivery(self.directory, delivery(payload), "test")
        message = result["messages"][0]
        self.assertFalse(message["accepted"])
        self.assertEqual(message["rejectionCode"], "unknown_dispatch")
        self.assertEqual(message["reportErrors"], [])

    def test_unknown_message_type_is_preserved(self) -> None:
        receipt = {
            "result": {
                "deliveryId": "delivery_mixed",
                "messages": [
                    {
                        "id": "msg_guidance",
                        "type": "guidance_v2",
                        "body": "Keep the bounded scope.",
                        "payload": "{}",
                    }
                ],
            }
        }
        result = helper.process_delivery(self.directory, receipt, "test")
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["messages"][0]["type"], "guidance_v2")

    def test_push_drain_never_uses_wait_or_timeout(self) -> None:
        empty = {"result": {"messages": []}}
        with (
            patch.object(helper, "run_orca", return_value=empty) as orca,
            patch("builtins.print"),
        ):
            result = helper.command_drain_deliveries(
                Namespace(receipt_dir=str(self.directory), ack=None)
            )

        self.assertEqual(result, 0)
        arguments = orca.call_args.args[0]
        self.assertNotIn("--wait", arguments)
        self.assertNotIn("--timeout-ms", arguments)

    def test_worker_completion_queues_one_controller_wake(self) -> None:
        tasks = {"result": {"tasks": [{"id": "task_expected", "status": "completed"}]}}
        queued = {"ok": True, "result": {"queued": True}}
        with (
            patch.dict(os.environ, {"ORCA_TERMINAL_HANDLE": "term_worker"}),
            patch.object(
                helper,
                "call_orca",
                side_effect=[(0, tasks, ""), (0, queued, "")],
            ) as orca,
            patch("builtins.print"),
        ):
            result = helper.command_notify_controller(
                Namespace(receipt_dir=str(self.directory))
            )

        self.assertEqual(result, 0)
        send_arguments = orca.call_args_list[1].args[0]
        self.assertEqual(send_arguments[:2], ["terminal", "send"])
        self.assertIn("term_controller", send_arguments)
        self.assertNotIn("--wait", send_arguments)
        self.assertTrue((self.directory / helper.NOTIFICATION_FILE).exists())
        worker = helper.read_wave_state(self.directory)["workers"][0]
        self.assertEqual(worker["notification_status"], "queued")

    def test_live_prompt_bundles_wake_after_worker_done(self) -> None:
        prompt = helper.runtime_prompt("ROLE: IMPLEMENTER\n", self.directory)
        self.assertIn("notify-controller", prompt)
        self.assertIn("append the following with &&", prompt)
        self.assertNotIn("--wait", prompt)
        self.assertIn(str(self.directory / "runtime" / "helper.py"), prompt)

    def test_foreign_helper_build_is_refused_with_archived_path(self) -> None:
        helper.mutate_wave_state(
            self.directory,
            lambda state: state.update({"helper_sha256": "0" * 64}),
        )
        with self.assertRaises(helper.HelperError) as context:
            helper.read_wave_state(self.directory)
        self.assertIn("runtime/helper.py", str(context.exception))

    def test_duplicate_completion_never_mutates_accepted_state(self) -> None:
        payload = valid_report(
            taskId="task_expected", dispatchId="ctx_expected", outcome="succeeded"
        )
        release = {"ok": True, "result": {"state": "released"}}
        with patch.object(helper, "call_orca", return_value=(0, release, "")):
            helper.process_delivery(self.directory, delivery(payload), "test")
        conflicting = valid_report(
            taskId="task_expected",
            dispatchId="ctx_expected",
            outcome="failed",
            summary="Conflicting duplicate.",
        )
        with patch.object(helper, "call_orca") as orca:
            result = helper.process_delivery(
                self.directory, delivery(conflicting), "test"
            )
        message = result["messages"][0]
        self.assertTrue(message.get("duplicate"))
        orca.assert_not_called()
        state = helper.read_wave_state(self.directory)["workers"][0]
        self.assertEqual(state["lifecycle_status"], "succeeded")
        report = helper.load_json(self.directory / "reports" / "plan-map.json")
        self.assertEqual(report["report"]["summary"], "Mapped the bounded surface.")

    def test_valid_duplicate_repairs_invalid_journaled_report(self) -> None:
        probe = {
            "taskId": "task_expected",
            "dispatchId": "ctx_expected",
            "outcome": "succeeded",
            "reportSchemaVersion": 1,
        }
        release = {"ok": True, "result": {"state": "released"}}
        with patch.object(helper, "call_orca", return_value=(0, release, "")):
            helper.process_delivery(self.directory, delivery(probe), "test")
        state = helper.read_wave_state(self.directory)["workers"][0]
        self.assertEqual(state["report_status"], "invalid")
        real = valid_report(
            taskId="task_expected", dispatchId="ctx_expected", outcome="succeeded"
        )
        with patch.object(helper, "call_orca") as orca:
            result = helper.process_delivery(self.directory, delivery(real), "test")
        message = result["messages"][0]
        self.assertTrue(message.get("repairedReport"))
        orca.assert_not_called()
        state = helper.read_wave_state(self.directory)["workers"][0]
        self.assertEqual(state["report_status"], "valid")
        self.assertEqual(state["lifecycle_status"], "succeeded")
        report = helper.load_json(self.directory / "reports" / "plan-map.json")
        self.assertEqual(report["report"]["summary"], "Mapped the bounded surface.")

    def test_status_message_report_is_journaled_and_flagged(self) -> None:
        receipt = {
            "result": {
                "deliveryId": "delivery_status",
                "messages": [
                    {
                        "id": "msg_status",
                        "type": "status",
                        "body": "Interim.",
                        "payload": json.dumps(
                            valid_report(
                                taskId="task_expected", dispatchId="ctx_expected"
                            )
                        ),
                    }
                ],
            }
        }
        result = helper.process_delivery(self.directory, receipt, "test")
        message = result["messages"][0]
        self.assertTrue(message.get("misdirectedReport"))
        self.assertIn(".status-", message["reportPath"])
        self.assertTrue(Path(message["reportPath"]).exists())
        self.assertEqual(helper.delivery_actions(result), [message])
        state = helper.read_wave_state(self.directory)["workers"][0]
        self.assertIsNone(state["completion_accepted"])

    def test_report_path_file_is_ingested(self) -> None:
        report_file = self.directory / "side-report.json"
        report_file.write_text(json.dumps(valid_report()), encoding="utf-8")
        payload = {
            "taskId": "task_expected",
            "dispatchId": "ctx_expected",
            "outcome": "succeeded",
            "reportPath": str(report_file),
        }
        release = {"ok": True, "result": {"state": "released"}}
        with patch.object(helper, "call_orca", return_value=(0, release, "")):
            result = helper.process_delivery(self.directory, delivery(payload), "test")
        message = result["messages"][0]
        self.assertTrue(message["accepted"])
        self.assertEqual(message["reportErrors"], [])
        state = helper.read_wave_state(self.directory)["workers"][0]
        self.assertEqual(state["report_status"], "valid")

    def test_report_summary_is_clamped(self) -> None:
        payload = valid_report(
            taskId="task_expected",
            dispatchId="ctx_expected",
            outcome="succeeded",
            summary="x" * 10_000,
        )
        release = {"ok": True, "result": {"state": "released"}}
        with patch.object(helper, "call_orca", return_value=(0, release, "")):
            result = helper.process_delivery(self.directory, delivery(payload), "test")
        message = result["messages"][0]
        self.assertLessEqual(len(message["summary"]), helper.MAX_BODY_OUTPUT_CHARS)
        self.assertTrue(message["truncated"])

    def test_failed_wake_send_clears_the_coalescing_marker(self) -> None:
        tasks = {"result": {"tasks": [{"id": "task_expected", "status": "completed"}]}}
        failed_send = {"ok": False, "error": {"state": "unknown"}}
        with (
            patch.dict(os.environ, {"ORCA_TERMINAL_HANDLE": "term_worker"}),
            patch.object(
                helper,
                "call_orca",
                side_effect=[(0, tasks, ""), (1, failed_send, "send failed")],
            ),
            patch("builtins.print"),
        ):
            helper.command_notify_controller(Namespace(receipt_dir=str(self.directory)))
        self.assertFalse((self.directory / helper.NOTIFICATION_FILE).exists())


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

    def test_fable_high_launches_claude_agent(self) -> None:
        manifest = {
            **self.manifest,
            "workers": [{**self.manifest["workers"][0], "launch": "fable-high"}],
        }
        _, workers, _ = helper.validate_manifest(manifest)
        args = helper.worker_start_args(workers[0], "task_1", "label", "run_1")
        self.assertEqual(args[args.index("--agent") + 1], "claude")
        self.assertEqual(args[args.index("--model") + 1], "claude-fable-5")
        self.assertEqual(args[args.index("--effort") + 1], "high")

    def test_luna_fast_keeps_the_base_model_and_uses_fast_flag(self) -> None:
        manifest = {
            **self.manifest,
            "workers": [{**self.manifest["workers"][0], "launch": "luna-fast"}],
        }
        _, workers, _ = helper.validate_manifest(manifest)
        args = helper.worker_start_args(workers[0], "task_1", "label", "run_1")
        self.assertEqual(args[args.index("--model") + 1], "gpt-5.6-luna")
        self.assertEqual(args[args.index("--effort") + 1], "max")
        self.assertIn("--fast", args)

    def test_luna_fast_preflight_checks_effort_and_speed_tier(self) -> None:
        worker = {"launch": "luna-fast"}
        catalog = {
            "gpt-5.6-luna": {
                "efforts": {"low", "max"},
                "speedTiers": {"fast"},
            }
        }
        with patch.object(helper, "codex_model_catalog", return_value=(catalog, None)):
            check = helper.launch_checks([worker])[0]
        self.assertTrue(check["passed"])
        self.assertEqual(check["model"], "gpt-5.6-luna")
        self.assertEqual(check["speedTier"], "fast")

        catalog["gpt-5.6-luna"]["speedTiers"] = set()
        with patch.object(helper, "codex_model_catalog", return_value=(catalog, None)):
            check = helper.launch_checks([worker])[0]
        self.assertFalse(check["passed"])


class ContractTests(unittest.TestCase):
    def test_used_run_flags_are_in_preflight_contract(self) -> None:
        for command in (
            "orchestration task-create",
            "orchestration worker-start",
            "orchestration check",
        ):
            self.assertIn("run", helper.REQUIRED_ORCA_COMMANDS[command])

    def test_controller_check_contract_has_no_polling_flags(self) -> None:
        flags = helper.REQUIRED_ORCA_COMMANDS["orchestration check"]
        self.assertEqual(flags, {"run", "ack", "json"})

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


if __name__ == "__main__":
    unittest.main()
