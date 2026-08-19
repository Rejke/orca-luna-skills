def terminal_gone(receipt: Any, detail: str) -> bool:
    blob = ((detail or "") + (compact_json(receipt) if receipt is not None else "")).lower()
    return "not_found" in blob or "not found" in blob or "unknown terminal" in blob


def reconcile_stop_wave(directory: Path) -> dict[str, Any]:
    request_cancel(directory)
    errors: list[str] = []
    snapshot = read_wave_state(directory)
    for record in snapshot["workers"]:
        index = record["index"]
        handle = record.get("terminal_handle")
        if record.get("stop_status") == "stopped":
            continue
        if not handle:
            update_worker_state(
                directory,
                index,
                start_status="cancelled"
                if record.get("start_status") in {"pending", "waiting"}
                else record.get("start_status"),
                stop_status="not_created",
            )
            continue
        returncode, receipt, detail = call_orca(
            ["terminal", "close", "--terminal", handle, "--json"]
        )
        save_json(
            directory / "runtime" / f"stop-{record['worker_id']}.json",
            receipt
            if receipt is not None
            else {"returncode": returncode, "detail": detail},
        )
        if returncode == 0 or terminal_gone(receipt, detail):
            update_worker_state(directory, index, stop_status="stopped")
        else:
            errors.append(f"worker {index}: {detail}")
            update_worker_state(
                directory, index, stop_status="stop_failed", error=detail
            )
    latest = read_wave_state(directory)
    unresolved = [
        record["index"]
        for record in latest["workers"]
        if record.get("start_status") in AMBIGUOUS_START_STATUSES
        or record.get("stop_status") == "stop_failed"
    ]
    phase = "cancel_pending" if errors or unresolved else "cancelled"
    set_wave_phase(directory, phase, error="; ".join(errors) if errors else None)
    final = read_wave_state(directory)
    return {
        "status": phase,
        "unresolved": unresolved,
        "workers": wave_records(final),
        "receipts": str(directory),
    }


def continue_wave(directory: Path, *, resumed: bool) -> int:
    manifest = load_json(directory / "manifest.json")
    normalized_manifest, workers, prompts = validate_manifest(manifest)
    state = read_wave_state(directory)
    if state.get("manifest_sha256") != manifest_digest(normalized_manifest):
        raise HelperError(
            "journal manifest changed after dispatch; resume is forbidden"
        )
    if cancel_requested(directory) or state.get("cancel_requested"):
        raise HelperError("wave is cancelled; resume is forbidden")
    set_wave_phase(directory, "writing_prompts")
    write_prompts(directory, workers, prompts)
    set_wave_phase(directory, "spawning_workers")
    spawn_pending_workers(directory, workers)
    if cancel_requested(directory):
        print(compact_json(reconcile_stop_wave(directory)), flush=True)
        return 0
    set_wave_phase(directory, "running")
    final = read_wave_state(directory)
    print(
        compact_json(
            {
                "status": "resumed" if resumed else "dispatched",
                "workers": wave_records(final),
                "receipts": str(directory),
            }
        ),
        flush=True,
    )
    return 0


def initialize_wave_state(
    directory: Path, manifest: dict[str, Any], workers: list[dict[str, Any]]
) -> None:
    now = time.time()
    save_json(
        directory / STATE_FILE,
        {
            "version": STATE_VERSION,
            "manifest_sha256": manifest_digest(manifest),
            "helper_sha256": helper_digest(),
            "mode": manifest["mode"],
            "objective": manifest["objective"],
            "launch_specs": LAUNCH_SPECS,
            "controller_terminal_handle": require_string(
                os.environ.get("ORCA_TERMINAL_HANDLE"), "ORCA_TERMINAL_HANDLE"
            ),
            "phase": "initialized",
            "cancel_requested": False,
            "created_at": now,
            "updated_at": now,
            "workers": [
                {
                    "index": index,
                    "worker_id": worker["id"],
                    "role": worker["role"],
                    "launch": worker["launch"],
                    "mutation": worker["mutation"],
                    "criteria": worker["criteria"],
                    "label": worker_label(worker, index),
                    "worktree_id": None,
                    "terminal_handle": None,
                    "spawn_command": None,
                    "banner_proof": None,
                    "depends_on": worker.get("dependsOn") or [],
                    "start_status": "waiting"
                    if worker.get("dependsOn")
                    else "pending",
                    "report_sha": None,
                    "report_status": "pending",
                    "task_status": None,
                    "verdict": None,
                    "question": None,
                    "answers": 0,
                    "notification_status": "pending",
                    "stop_status": None,
                    "error": None,
                }
                for index, worker in enumerate(workers, start=1)
            ],
        },
    )


def command_dispatch_wave(args: argparse.Namespace) -> int:
    manifest, workers, prompts = validate_manifest(load_json(args.manifest))
    if args.dry_run:
        print(
            compact_json(
                {
                    "status": "valid",
                    "schemaVersion": MANIFEST_VERSION,
                    "mode": manifest["mode"],
                    "workers": [
                        {
                            "index": index,
                            "role": worker["role"],
                            "launch": worker["launch"],
                            "label": worker_label(worker, index),
                            "prompt_chars": len(prompt),
                            "overBudget": len(prompt) > PROMPT_BUDGET_CHARS,
                        }
                        for index, (worker, prompt) in enumerate(
                            zip(workers, prompts), start=1
                        )
                    ],
                }
            )
        )
        return 0

    directory = receipt_dir(args.receipt_dir)
    if (directory / STATE_FILE).exists():
        raise HelperError(f"wave state already exists; use resume-wave: {directory}")
    ensure_journal(directory)
    verify_preflight(directory, manifest, workers, prompts)
    save_json(directory / "manifest.json", manifest)
    initialize_wave_state(directory, manifest, workers)
    print(
        compact_json(
            {
                "status": "starting",
                "launches": {
                    alias: sum(1 for worker in workers if worker["launch"] == alias)
                    for alias in sorted({worker["launch"] for worker in workers})
                },
                "count": len(workers),
                "receipts": str(directory),
            }
        ),
        flush=True,
    )
    try:
        return continue_wave(directory, resumed=False)
    except KeyboardInterrupt:
        print(compact_json(reconcile_stop_wave(directory)), flush=True)
        return 130
    except HelperError as exc:
        if cancel_requested(directory):
            print(compact_json(reconcile_stop_wave(directory)), flush=True)
            return 0
        set_wave_phase(directory, "error", error=str(exc))
        raise


def command_resume_wave(args: argparse.Namespace) -> int:
    directory = receipt_dir(args.receipt_dir)
    state = read_wave_state(directory)
    ambiguous = [
        record["index"]
        for record in state["workers"]
        if record.get("start_status") in AMBIGUOUS_START_STATUSES
    ]
    if ambiguous:
        raise HelperError(
            f"cannot safely resume ambiguous worker indices {ambiguous}; "
            "inspect receipts and stop the wave"
        )
    try:
        return continue_wave(directory, resumed=True)
    except KeyboardInterrupt:
        print(compact_json(reconcile_stop_wave(directory)), flush=True)
        return 130


def command_stop_wave(args: argparse.Namespace) -> int:
    directory = receipt_dir(args.receipt_dir)
    print(compact_json(reconcile_stop_wave(directory)), flush=True)
    return 0


