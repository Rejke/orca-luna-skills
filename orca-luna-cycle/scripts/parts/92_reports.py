MAX_REPORT_FILE_BYTES = 262_144


def read_incoming_report(path: Path) -> tuple[Any, str | None]:
    try:
        if path.stat().st_size > MAX_REPORT_FILE_BYTES:
            return None, f"report file exceeds {MAX_REPORT_FILE_BYTES} bytes"
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return None, str(exc)
    try:
        return json.loads(text), None
    except json.JSONDecodeError as exc:
        return None, f"invalid JSON: {exc}"


def validate_report(report: Any, role: str) -> list[str]:
    if not isinstance(report, dict):
        return ["missing structured report payload"]
    errors: list[str] = []
    if report.get("reportSchemaVersion") != REPORT_VERSION:
        errors.append(f"reportSchemaVersion must be {REPORT_VERSION}")
    if report.get("taskStatus") not in {"done", "failed", "blocked"}:
        errors.append("taskStatus must be done, failed, or blocked")
    summary = report.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        errors.append("summary must be a non-empty string")
    for field in ("evidence", "risks", "checks", "filesModified"):
        value = report.get(field)
        if not isinstance(value, list) or not all(
            isinstance(item, str) for item in value
        ):
            errors.append(f"{field} must be an array of strings")
    findings = report.get("findings")
    if not isinstance(findings, list) or not all(
        isinstance(item, dict) for item in findings
    ):
        errors.append("findings must be an array of objects")
    elif any(
        item.get("severity") not in {"critical", "high", "medium", "low"}
        or not isinstance(item.get("title"), str)
        or not isinstance(item.get("evidence"), str)
        for item in findings
    ):
        errors.append("each finding requires severity, title, and evidence")
    for field in ROLE_REPORT_FIELDS.get(role, {}):
        if field not in report:
            errors.append(f"missing role field: {field}")
    if role in REVIEW_ROLES and report.get("verdict") not in {
        "PASS",
        "FAIL",
        "UNKNOWN",
        "BLOCKED",
    }:
        errors.append("verdict must be PASS, FAIL, UNKNOWN, or BLOCKED")
    if (
        role in REVIEW_ROLES
        and report.get("taskStatus") == "blocked"
        and report.get("verdict") != "BLOCKED"
    ):
        errors.append("blocked reviews must use verdict BLOCKED")
    if role == "scout" and not isinstance(report.get("shards"), list):
        errors.append("shards must be an array")
    if role in {"implementer", "fixer"} and not (
        report.get("commit") is None or isinstance(report.get("commit"), str)
    ):
        errors.append("commit must be a string or null")
    if role == "integrator":
        for field in ("integrated", "conflicts"):
            value = report.get(field)
            if not isinstance(value, list) or not all(
                isinstance(item, str) for item in value
            ):
                errors.append(f"{field} must be an array of strings")
        if not isinstance(report.get("anchor"), str):
            errors.append("anchor must be a string")
    if role in REVIEW_ROLES and not isinstance(report.get("criteria"), dict):
        errors.append("criteria must be an object")
    if role == "antislop":
        if not isinstance(report.get("cuts"), list):
            errors.append("cuts must be an array")
        if (
            not isinstance(report.get("leanness"), int)
            or not 1 <= report["leanness"] <= 10
        ):
            errors.append("leanness must be an integer from 1 to 10")
    if role == "fixer" and not isinstance(report.get("fixed"), list):
        errors.append("fixed must be an array")
    question = report.get("question")
    if question is not None and (
        not isinstance(question, str)
        or not question.strip()
        or len(question) > 500
    ):
        errors.append("question must be a non-empty string of at most 500 characters")
    if report.get("taskStatus") == "blocked" and not question:
        errors.append("a blocked report needs a question")
    prompt_feedback = report.get("promptFeedback")
    if prompt_feedback is not None:
        if not isinstance(prompt_feedback, list) or len(prompt_feedback) > 3:
            errors.append("promptFeedback must be an array of at most 3 entries")
        else:
            for entry in prompt_feedback:
                if not isinstance(entry, dict):
                    errors.append("each promptFeedback entry must be an object")
                    break
                rule = entry.get("rule")
                if (
                    not isinstance(entry.get("failureClass"), str)
                    or not entry["failureClass"].strip()
                    or not isinstance(rule, str)
                    or not rule.strip()
                    or len(rule) > 200
                    or entry.get("severity")
                    not in {"critical", "high", "medium", "low"}
                    or not isinstance(entry.get("scopes"), list)
                    or not entry["scopes"]
                    or not all(
                        isinstance(scope, str) and scope.strip()
                        for scope in entry["scopes"]
                    )
                    or entry.get("gap", "prompt")
                    not in {"prompt", "decomposition", "judgment", "test", "tooling"}
                ):
                    errors.append(
                        "each promptFeedback entry needs failureClass, a rule of at "
                        "most 200 chars, severity, non-empty scopes, and a valid gap"
                    )
                    break
    rule_feedback = report.get("ruleFeedback")
    if rule_feedback is not None:
        if not isinstance(rule_feedback, list) or not all(
            isinstance(entry, dict)
            and isinstance(entry.get("id"), str)
            and entry.get("status") in {"violated", "helped", "retire"}
            for entry in rule_feedback
        ):
            errors.append(
                "each ruleFeedback entry needs id and status violated, helped, or retire"
            )
    return errors


def scan_incoming_reports(directory: Path) -> tuple[list[dict[str, Any]], int]:
    """Process new or changed report files; return (messages, actionable count)."""
    state = read_wave_state(directory)
    messages: list[dict[str, Any]] = []
    actions = 0
    for record in state["workers"]:
        worker_id = record["worker_id"]
        index = record["index"]
        path = worker_report_path(directory, worker_id)
        if not path.exists():
            continue
        try:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError as exc:
            messages.append({"workerId": worker_id, "error": str(exc)})
            actions += 1
            continue
        if digest == record.get("report_sha"):
            continue
        report, read_error = read_incoming_report(path)
        message: dict[str, Any] = {"workerId": worker_id, "reportFile": str(path)}
        if read_error or not isinstance(report, dict):
            update_worker_state(
                directory,
                index,
                report_sha=digest,
                report_status="invalid",
                error=read_error or "report is not a JSON object",
            )
            message.update(
                {
                    "reportStatus": "invalid",
                    "error": read_error or "report is not a JSON object",
                }
            )
            actions += 1
            messages.append(message)
            continue
        settled_before = (
            record.get("task_status") in {"done", "failed"}
            and record.get("report_status") == "valid"
        )
        if settled_before:
            # A finished worker's file changed. Never replace the accepted
            # report; surface the change for Sol instead.
            update_worker_state(directory, index, report_sha=digest)
            message.update({"changedAfterDone": True})
            actions += 1
            messages.append(message)
            continue
        errors = validate_report(report, record.get("role", ""))
        raw_task_status = report.get("taskStatus")
        task_status = raw_task_status if isinstance(raw_task_status, str) else None
        raw_verdict = report.get("verdict")
        verdict = raw_verdict if isinstance(raw_verdict, str) else None
        raw_question = report.get("question")
        question = raw_question if isinstance(raw_question, str) else None
        raw_summary = report.get("summary")
        summary = raw_summary if isinstance(raw_summary, str) else ""
        stored = directory / "reports" / f"{worker_id}.json"
        save_json(
            stored,
            {
                "accepted": not errors,
                "validationErrors": errors,
                "report": report,
            },
        )
        settled_stamp = (
            {"settled_at": time.time()}
            if not errors
            and task_status in {"done", "failed"}
            and not record.get("settled_at")
            else {}
        )
        update_worker_state(
            directory,
            index,
            report_sha=digest,
            report_status="valid" if not errors else "invalid",
            task_status=task_status,
            verdict=verdict,
            question=question,
            start_status="completed"
            if task_status in {"done", "failed"}
            else ("blocked" if task_status == "blocked" else record.get("start_status")),
            error=None,
            **settled_stamp,
        )
        message.update(
            {
                "taskStatus": task_status,
                "verdict": verdict,
                "question": question,
                "summary": summary[:MAX_BODY_OUTPUT_CHARS],
                "truncated": len(summary) > MAX_BODY_OUTPUT_CHARS,
                "reportErrors": errors,
                "report": str(stored),
            }
        )
        if (
            errors
            or task_status in {"blocked", "failed"}
            or question
            or verdict in {"FAIL", "UNKNOWN", "BLOCKED"}
        ):
            actions += 1
        messages.append({k: v for k, v in message.items() if v is not None})
    return messages, actions


def command_collect_reports(args: argparse.Namespace) -> int:
    """Read every new or changed report file; never wait for future ones."""
    directory = receipt_dir(args.receipt_dir)
    read_wave_state(directory)
    messages, actions = scan_incoming_reports(directory)
    # Close the publication race: a worker may have written its file after the
    # scan while the wake marker still existed.
    clear_notification(directory)
    late_messages, late_actions = scan_incoming_reports(directory)
    messages.extend(late_messages)
    actions += late_actions
    latest = read_wave_state(directory)
    settled = wave_settled(latest)
    # Attention comes from durable state, not from message newness: a collect
    # that crashed after journaling must show the same items on replay.
    attention = [
        {
            "workerId": worker["worker_id"],
            **({"question": worker["question"]} if worker.get("question") else {}),
            **(
                {"reportStatus": "invalid"}
                if worker.get("report_status") == "invalid"
                else {}
            ),
            **(
                {"taskStatus": worker["task_status"]}
                if worker.get("task_status") in {"blocked", "failed"}
                else {}
            ),
            **(
                {"verdict": worker["verdict"]}
                if worker.get("verdict") in {"FAIL", "UNKNOWN", "BLOCKED"}
                else {}
            ),
        }
        for worker in latest.get("workers", [])
        if worker.get("question")
        or worker.get("report_status") == "invalid"
        or worker.get("task_status") in {"blocked", "failed"}
        or worker.get("verdict") in {"FAIL", "UNKNOWN", "BLOCKED"}
    ]
    status = (
        "wave_settled"
        if settled
        else ("action_required" if attention else "idle_push_mode")
    )
    print(
        compact_json(
            {
                "status": status,
                "messages": messages,
                "attention": attention,
                "workers": wave_records(latest),
                "next": (
                    "run finalize-wave"
                    if settled
                    else (
                        "handle each attention item (answer questions with "
                        "the answer command), then return to idle"
                        if attention
                        else "return to idle until the next wake"
                    )
                ),
                "receipts": str(directory),
            }
        )
    )
    return 0


def command_status(args: argparse.Namespace) -> int:
    """Diagnose worker liveness on demand; changes nothing."""
    directory = receipt_dir(args.receipt_dir)
    state = read_wave_state(directory)
    rows: list[dict[str, Any]] = []
    suspects = 0
    for record in state["workers"]:
        report_path = worker_report_path(directory, record["worker_id"])
        new_report = False
        if report_path.exists():
            try:
                digest = hashlib.sha256(report_path.read_bytes()).hexdigest()
                new_report = digest != record.get("report_sha")
            except OSError:
                new_report = False
        entry: dict[str, Any] = {
            "workerId": record["worker_id"],
            "startStatus": record.get("start_status"),
            "taskStatus": record.get("task_status"),
            "newReport": new_report,
        }
        handle = record.get("terminal_handle")
        if handle and record.get("start_status") in {"running", "blocked"}:
            returncode, receipt, detail = call_orca(
                [
                    "terminal",
                    "wait",
                    "--terminal",
                    handle,
                    "--for",
                    "tui-idle",
                    "--timeout-ms",
                    "1500",
                    "--json",
                ]
            )
            if returncode == 0:
                terminal = "idle"
            else:
                blob = (detail or "") + (
                    compact_json(receipt) if receipt is not None else ""
                )
                terminal = "busy" if "timeout" in blob.lower() else "unreachable"
            entry["terminal"] = terminal
            if (
                terminal in {"idle", "unreachable"}
                and not new_report
                and record.get("start_status") == "running"
            ):
                entry["suspect"] = True
                suspects += 1
        rows.append(entry)
    print(
        compact_json(
            {
                "status": "ok",
                "workers": rows,
                "suspects": suspects,
                "next": (
                    "a suspect sits idle without a report: re-engage its terminal "
                    "with terminal send, or stop the wave"
                    if suspects
                    else "no action needed"
                ),
            }
        )
    )
    return 0


def command_answer(args: argparse.Namespace) -> int:
    """Send Sol's answer into the blocked worker's own terminal session."""
    directory = receipt_dir(args.receipt_dir)
    state = read_wave_state(directory)
    record = next(
        (
            worker
            for worker in state["workers"]
            if worker["worker_id"] == args.worker
        ),
        None,
    )
    if record is None:
        raise HelperError(f"no worker {args.worker!r} in this wave")
    handle = record.get("terminal_handle")
    if not handle:
        raise HelperError(f"worker {args.worker} has no terminal")
    if record.get("task_status") != "blocked" and not record.get("question"):
        raise HelperError(
            f"worker {args.worker} is not blocked on a question; "
            "answer refuses to re-engage it"
        )
    answer_file = Path(args.file).expanduser()
    if not answer_file.is_absolute() or not answer_file.exists():
        raise HelperError("answer --file must be an existing absolute path")
    pointer = (
        f"Your question was answered. Read the file {answer_file} and continue "
        "the same task. When finished, write the final report to your report "
        "file and run the wake command again."
    )
    returncode, receipt, detail = call_orca(
        [
            "terminal",
            "send",
            "--terminal",
            handle,
            "--text",
            pointer,
            "--enter",
            "--json",
        ]
    )
    save_json(
        directory / "answers" / f"{time.time_ns()}-{record['worker_id']}.json",
        receipt
        if receipt is not None
        else {"returncode": returncode, "detail": detail},
    )
    if returncode != 0:
        raise HelperError(f"answer delivery failed: {detail}")
    update_worker_state(
        directory,
        record["index"],
        start_status="running",
        task_status=None,
        question=None,
        answers=record.get("answers", 0) + 1,
        notification_status="pending",
    )
    print(
        compact_json(
            {
                "status": "answered",
                "workerId": record["worker_id"],
                "next": "return to idle; the worker wakes you with its final report",
            }
        )
    )
    return 0


def wave_settled(state: dict[str, Any]) -> bool:
    workers = state.get("workers", [])
    return bool(workers) and all(
        worker.get("task_status") in {"done", "failed"}
        and worker.get("report_status") == "valid"
        for worker in workers
    )


def notification_path(directory: Path) -> Path:
    return directory / NOTIFICATION_FILE


def clear_notification(directory: Path) -> bool:
    try:
        notification_path(directory).unlink()
    except FileNotFoundError:
        return False
    return True


