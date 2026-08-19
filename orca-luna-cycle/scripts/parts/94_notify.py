STALE_WAKE_SECONDS = 600


def claim_controller_notification(
    directory: Path, worker: dict[str, Any], controller_handle: str
) -> bool:
    path = notification_path(directory)
    payload = (
        compact_json(
            {
                "workerId": worker["worker_id"],
                "workerTerminalHandle": worker.get("terminal_handle"),
                "controllerTerminalHandle": controller_handle,
                "createdAt": time.time(),
            }
        )
        + "\n"
    )
    for attempt in (1, 2):
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            # A marker without a delivered wake (claim crashed before send)
            # must not silence every later completion; take over a stale one.
            if attempt == 2:
                return False
            try:
                created = json.loads(path.read_text(encoding="utf-8")).get(
                    "createdAt", 0
                )
            except (OSError, ValueError):
                created = 0
            if time.time() - created < STALE_WAKE_SECONDS:
                return False
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            continue
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        return True
    return False


def command_notify_controller(args: argparse.Namespace) -> int:
    """Worker-only, once per report: queue one coalesced wake prompt for Sol."""
    directory = receipt_dir(args.receipt_dir)
    state = read_wave_state(directory)
    caller = require_string(
        os.environ.get("ORCA_TERMINAL_HANDLE"), "ORCA_TERMINAL_HANDLE"
    )
    record = next(
        (
            worker
            for worker in state.get("workers", [])
            if worker.get("terminal_handle") == caller
        ),
        None,
    )
    if record is None:
        raise HelperError(
            "notify-controller caller is not a current worker terminal in this wave"
        )
    report_file = worker_report_path(directory, record["worker_id"])
    if not report_file.exists():
        raise HelperError(
            f"write your report to {report_file} before running notify-controller"
        )
    if record.get("notification_status") in {"queued", "coalesced"}:
        print(
            compact_json(
                {
                    "status": "already_notified",
                    "workerId": record["worker_id"],
                    "next": "stop; never send a duplicate wake",
                }
            )
        )
        return 0

    controller_handle = require_string(
        state.get("controller_terminal_handle"), "wave.controller_terminal_handle"
    )
    if not claim_controller_notification(directory, record, controller_handle):
        update_worker_state(directory, record["index"], notification_status="coalesced")
        print(
            compact_json(
                {
                    "status": "coalesced",
                    "workerId": record["worker_id"],
                    "next": "stop; another worker already queued the controller wake",
                }
            )
        )
        return 0

    collect_command = shlex.join(
        [
            "uv",
            "run",
            "--no-project",
            str(Path(__file__).resolve()),
            "collect-reports",
            "--receipt-dir",
            str(directory),
        ]
    )
    text = (
        "[ORCA LUNA CYCLE: REPORTS READY]\n"
        "A worker wrote its report file. Run this once, then follow its "
        "next field:\n"
        f"{collect_command}"
    )
    attempts: list[dict[str, Any]] = []
    status = "outcome_unknown"
    for attempt in (1, 2):
        # A wake sent into a mid-turn TUI drowns in the controller's own
        # input; wait for idle first, then send anyway — busy is a delay,
        # not a veto.
        call_orca(
            [
                "terminal",
                "wait",
                "--terminal",
                controller_handle,
                "--for",
                "tui-idle",
                "--timeout-ms",
                "8000",
                "--json",
            ]
        )
        returncode, receipt, detail = call_orca(
            [
                "terminal",
                "send",
                "--terminal",
                controller_handle,
                "--text",
                text,
                "--enter",
                "--json",
            ]
        )
        attempts.append(
            receipt
            if receipt is not None
            else {"returncode": returncode, "detail": detail}
        )
        if returncode != 0:
            status = classify_failure(receipt)
            break
        # Exit code 0 proves Orca accepted the send, not that the controller
        # saw it. Read the controller tail for the banner as delivery proof.
        read_code, read_receipt, _ = call_orca(
            [
                "terminal",
                "read",
                "--terminal",
                controller_handle,
                "--limit",
                "40",
                "--json",
            ]
        )
        blob = compact_json(read_receipt) if read_receipt is not None else ""
        if read_code == 0 and "REPORTS READY" in blob:
            status = "delivered"
            break
        status = "send_unverified"
    notification_receipt = (
        directory / "notifications" / f"{time.time_ns()}-{record['worker_id']}.json"
    )
    save_json(notification_receipt, {"status": status, "attempts": attempts})
    if status != "delivered":
        # A retained marker after a failed or unverified send would coalesce
        # every later wake into a wake that may never arrive and silently stall
        # the wave; a duplicate wake is harmless because collect is idempotent.
        clear_notification(directory)
    update_worker_state(directory, record["index"], notification_status=status)
    print(
        compact_json(
            {
                "status": status,
                "workerId": record["worker_id"],
                "controllerTerminalHandle": controller_handle,
                "receipt": str(notification_receipt),
                "next": "stop; the wake attempt is exactly-once and must not "
                "be retried",
                **(
                    {}
                    if status == "delivered"
                    else {
                        "error": "the controller terminal did not show the "
                        "wake banner; the report file itself is durable and "
                        "collect-reports will find it"
                    }
                ),
            }
        )
    )
    return 0


def command_rebind_controller(args: argparse.Namespace) -> int:
    """Run from the (new) controller terminal after a controller restart:
    point queued wakes at the terminal that is actually listening."""
    directory = receipt_dir(args.receipt_dir)
    handle = require_string(
        os.environ.get("ORCA_TERMINAL_HANDLE"), "ORCA_TERMINAL_HANDLE"
    )
    if not re.fullmatch(r"term_[A-Za-z0-9_-]+", handle):
        raise HelperError(f"ORCA_TERMINAL_HANDLE looks invalid: {handle!r}")
    state = read_wave_state(directory)
    if any(
        worker.get("terminal_handle") == handle
        for worker in state.get("workers", [])
    ):
        raise HelperError(
            "this terminal belongs to a worker; run rebind-controller from "
            "the controller terminal"
        )
    previous = state.get("controller_terminal_handle")
    mutate_wave_state(
        directory,
        lambda current: current.update({"controller_terminal_handle": handle}),
    )
    print(
        compact_json(
            {
                "status": "rebound",
                "previous": previous,
                "controllerTerminalHandle": handle,
                "next": "run collect-reports once; wakes sent before the "
                "rebind may have landed in the old terminal",
            }
        )
    )
    return 0


