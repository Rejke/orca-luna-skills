def has_material_effects(value: Any) -> bool:
    if value in (None, {}, []):
        return False
    if isinstance(value, dict):
        return any(has_material_effects(child) for child in value.values())
    if isinstance(value, list):
        return any(has_material_effects(child) for child in value)
    return bool(value)


def classify_failure(payload: Any, *, known_effect_id: str | None = None) -> str:
    code = first_named(payload, "code") if payload is not None else None
    effects = first_named(payload, "effects") if payload is not None else None
    residual = (
        first_named(payload, "residualResources") if payload is not None else None
    )
    states = {
        str(child).lower()
        for node in walk(payload)
        if isinstance(node, dict)
        for key, child in node.items()
        if normalized_key(key) in {"state", "status", "outcome"}
        and isinstance(child, str)
    }
    if "outcome_unknown" in states or "unknown" in states:
        return "outcome_unknown"
    if (
        known_effect_id
        or has_material_effects(effects)
        or has_material_effects(residual)
    ):
        return "failed_with_known_effects"
    if isinstance(code, str) or first_named(payload, "ok") is False:
        return "rejected_no_effects"
    return "outcome_unknown"


AMBIGUOUS_START_STATUSES = {
    "creating_worktree",
    "spawning",
    "prompting",
    "worktree_outcome_unknown",
    "terminal_outcome_unknown",
}


def write_prompts(
    directory: Path, workers: list[dict[str, Any]], prompts: list[str]
) -> None:
    for worker, prompt in zip(workers, prompts):
        path = worker_prompt_path(directory, worker["id"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            runtime_prompt(prompt, directory, worker["id"]), encoding="utf-8"
        )


def spawn_pending_workers(directory: Path, workers: list[dict[str, Any]]) -> None:
    # Phase 1: create worktrees and terminals. The agents boot in parallel
    # while this loop moves on, so phase 2's waits mostly return at once.
    for index, worker in enumerate(workers, start=1):
        if cancel_requested(directory):
            return
        record = read_wave_state(directory)["workers"][index - 1]
        if record.get("terminal_handle"):
            continue
        if record.get("start_status") != "pending":
            raise HelperError(
                f"worker {index} is {record.get('start_status')}; "
                "refusing an ambiguous spawn retry"
            )
        spec = LAUNCH_SPECS[worker["launch"]]
        worktree = worker["worktree"]
        if worktree in {"new-child", "new-top-level"}:
            update_worker_state(directory, index, start_status="creating_worktree")
            returncode, receipt, detail = call_orca(
                [
                    "worktree",
                    "create",
                    "--name",
                    worker["name"],
                    "--base-branch",
                    worker["baseBranch"],
                    "--setup",
                    worker.get("setup", "run"),
                    "--json",
                ]
            )
            save_json(
                directory / "worktrees" / f"{worker['id']}.json",
                receipt
                if receipt is not None
                else {"returncode": returncode, "detail": detail},
            )
            worktree_id = find_entity_id(receipt, "worktree") if receipt else None
            if returncode != 0 or not worktree_id:
                classification = classify_failure(receipt, known_effect_id=worktree_id)
                update_worker_state(
                    directory,
                    index,
                    worktree_id=worktree_id,
                    start_status=f"worktree_{classification}",
                    error=f"worktree create exited {returncode}: {detail}",
                )
                raise HelperError(
                    f"worktree create for worker {index} {classification}: "
                    f"{detail}; receipts: {directory}"
                )
            update_worker_state(directory, index, worktree_id=worktree_id)
            selector = f"id:{worktree_id}"
        else:
            selector = worktree
        if cancel_requested(directory):
            return
        update_worker_state(directory, index, start_status="spawning")
        command = spawn_command(spec)
        returncode, receipt, detail = call_orca(
            [
                "terminal",
                "create",
                "--worktree",
                selector,
                "--title",
                record["label"],
                "--command",
                command,
                "--json",
            ]
        )
        save_json(
            directory / "terminals" / f"{worker['id']}.json",
            receipt
            if receipt is not None
            else {"returncode": returncode, "detail": detail},
        )
        handle = find_terminal_handle(receipt) if receipt else None
        if returncode != 0 or not handle:
            classification = classify_failure(receipt, known_effect_id=handle)
            update_worker_state(
                directory,
                index,
                terminal_handle=handle,
                start_status=f"terminal_{classification}",
                error=f"terminal create exited {returncode}: {detail}",
            )
            raise HelperError(
                f"terminal create for worker {index} {classification} at exact "
                f"{spec['model']} {spec['effort']}: {detail}; receipts: {directory}"
            )
        update_worker_state(
            directory,
            index,
            terminal_handle=handle,
            spawn_command=command,
            start_status="booting",
            error=None,
        )

    # Phase 2: wait for each boot, prove the banner, send the prompt pointer.
    for index, worker in enumerate(workers, start=1):
        if cancel_requested(directory):
            return
        record = read_wave_state(directory)["workers"][index - 1]
        if record.get("start_status") != "booting":
            continue
        spec = LAUNCH_SPECS[worker["launch"]]
        handle = record["terminal_handle"]
        wait_code, wait_receipt, wait_detail = call_orca(
            [
                "terminal",
                "wait",
                "--terminal",
                handle,
                "--for",
                "tui-idle",
                "--timeout-ms",
                str(AGENT_BOOT_TIMEOUT_MS),
                "--json",
            ]
        )
        save_json(
            directory / "runtime" / f"boot-{worker['id']}.json",
            wait_receipt
            if wait_receipt is not None
            else {"returncode": wait_code, "detail": wait_detail},
        )
        if wait_code != 0:
            update_worker_state(
                directory, index, start_status="boot_failed", error=wait_detail
            )
            raise HelperError(
                f"worker {index} agent did not reach idle in "
                f"{AGENT_BOOT_TIMEOUT_MS} ms: {wait_detail}; receipts: {directory}"
            )
        # The boot banner is secondary launch evidence; the primary proof is the
        # spawn command this helper recorded itself.
        banner_proof = False
        read_code, read_receipt, _ = call_orca(
            ["terminal", "read", "--terminal", handle, "--limit", "60", "--json"]
        )
        if read_code == 0 and read_receipt is not None:
            save_json(directory / "runtime" / f"banner-{worker['id']}.json", read_receipt)
            banner_proof = spec["model"] in compact_json(read_receipt)
        update_worker_state(directory, index, start_status="prompting")
        pointer = (
            f"Read the file {worker_prompt_path(directory, worker['id'])} "
            "and do exactly what it says."
        )
        send_code, send_receipt, send_detail = call_orca(
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
            directory / "runtime" / f"prompt-send-{worker['id']}.json",
            send_receipt
            if send_receipt is not None
            else {"returncode": send_code, "detail": send_detail},
        )
        if send_code != 0:
            update_worker_state(
                directory,
                index,
                start_status="prompt_send_failed",
                banner_proof=banner_proof,
                error=send_detail,
            )
            raise HelperError(
                f"prompt delivery to worker {index} failed: {send_detail}; "
                f"receipts: {directory}"
            )
        update_worker_state(
            directory,
            index,
            start_status="running",
            banner_proof=banner_proof,
            started_at=time.time(),
            error=None,
        )


