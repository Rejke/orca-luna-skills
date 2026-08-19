def exact_launch_proven(record: dict[str, Any]) -> bool:
    """The helper spawned the agent itself, so the recorded spawn command is
    the primary launch proof; the boot banner is secondary evidence only."""
    spec = LAUNCH_SPECS.get(record.get("launch"))
    if spec is None:
        return False
    return record.get("spawn_command") == spawn_command(spec)


def git_snapshot() -> dict[str, Any]:
    try:
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            text=True,
            capture_output=True,
            check=False,
        )
        status = subprocess.run(
            ["git", "status", "--short"],
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        return {"status": "error", "error": str(exc)}
    if head.returncode != 0 or status.returncode != 0:
        return {
            "status": "error",
            "error": (
                head.stderr or status.stderr or head.stdout or status.stdout
            ).strip(),
        }
    return {
        "status": "captured",
        "head": head.stdout.strip(),
        "dirtyState": [line for line in status.stdout.splitlines() if line],
    }


def anchor_check(
    directory: Path, manifest: dict[str, Any], state: dict[str, Any]
) -> dict[str, Any]:
    workers = state.get("workers", [])
    helper_can_verify = (
        bool(workers)
        and all(worker.get("mutation") == "forbidden" for worker in workers)
        and all(
            normalized.get("worktree") == "current"
            for normalized in manifest.get("workers", [])
        )
    )
    if not helper_can_verify:
        return {
            "status": "controller_required",
            "passed": True,
            "reason": "mutating or non-current worktrees require Sol to verify the integration anchor",
        }
    snapshot = git_snapshot()
    if snapshot.get("status") != "captured":
        return {**snapshot, "passed": False}
    baseline = None
    try:
        loaded = load_json(directory / "preflight.json")
        if isinstance(loaded, dict):
            baseline = loaded.get("gitBaseline")
    except Exception:
        baseline = None
    if isinstance(baseline, dict) and baseline.get("status") == "captured":
        # Preflight measured the baseline itself; preservation is a byte-exact
        # measured-to-measured comparison. Manifest anchors are cross-checked
        # at preflight and are not re-litigated here.
        preserved = (
            snapshot.get("head") == baseline.get("head")
            and snapshot.get("dirtyState") == baseline.get("dirtyState")
        )
        return {
            **snapshot,
            "status": "verified" if preserved else "changed",
            "passed": preserved,
            "baseline": {
                "head": baseline.get("head"),
                "dirtyState": baseline.get("dirtyState"),
            },
        }
    # Legacy wave without a measured baseline: fall back to the declared anchor.
    envelope = manifest["envelope"]
    expected_head = envelope.get("baseAnchor")
    expected_dirty = envelope.get("dirtyState", [])
    head_ok = expected_head in (None, "") or snapshot.get("head") == expected_head
    dirty_ok = snapshot.get("dirtyState") == expected_dirty
    return {
        **snapshot,
        "status": "verified" if head_ok and dirty_ok else "changed",
        "passed": head_ok and dirty_ok,
        "expectedHead": expected_head,
        "expectedDirtyState": expected_dirty,
    }


def command_finalize_wave(args: argparse.Namespace) -> int:
    directory = receipt_dir(args.receipt_dir)
    manifest, _, _ = validate_manifest(load_json(directory / "manifest.json"))
    state = read_wave_state(directory)
    if state.get("manifest_sha256") != manifest_digest(manifest):
        raise HelperError(
            "journal manifest changed after dispatch; finalization is unsafe"
        )
    workers = state.get("workers", [])
    unresolved = [
        worker["worker_id"]
        for worker in workers
        if worker.get("start_status") in AMBIGUOUS_START_STATUSES
    ]
    checks = {
        "preflightPassed": load_json(directory / "preflight.json").get("status")
        == "passed",
        "allWorkersSpawned": all(worker.get("terminal_handle") for worker in workers),
        "allSettled": all(
            worker.get("task_status") in {"done", "failed"} for worker in workers
        ),
        "allReportsValid": all(
            worker.get("report_status") == "valid" for worker in workers
        ),
        "noOpenQuestions": all(not worker.get("question") for worker in workers),
        "noPendingControllerWake": not notification_path(directory).exists(),
        "exactLaunchProven": all(exact_launch_proven(worker) for worker in workers),
        "noAmbiguousEffects": not unresolved,
    }
    anchor = anchor_check(directory, manifest, state)
    checks["anchorPreservedOrDelegated"] = anchor.get("passed") is True
    created_worktrees = [
        {
            "workerId": record.get("worker_id"),
            "worktreeId": record.get("worktree_id"),
            "selector": spec.get("worktree"),
        }
        for record, spec in zip(workers, manifest.get("workers", []))
        if spec.get("worktree") in {"new-child", "new-top-level"}
    ]
    mechanical_ok = all(checks.values())
    if mechanical_ok:
        for record in workers:
            handle = record.get("terminal_handle")
            if not handle or record.get("stop_status") in {"stopped", "closed"}:
                continue
            returncode, receipt, detail = call_orca(
                ["terminal", "close", "--terminal", handle, "--json"]
            )
            save_json(
                directory / "runtime" / f"close-{record['worker_id']}.json",
                receipt
                if receipt is not None
                else {"returncode": returncode, "detail": detail},
            )
            update_worker_state(
                directory,
                record["index"],
                stop_status="closed"
                if returncode == 0 or terminal_gone(receipt, detail)
                else "close_failed",
            )
    set_wave_phase(directory, "finalized" if mechanical_ok else "finalize_incomplete")
    state = read_wave_state(directory)
    final = {
        "finalSchemaVersion": 2,
        "runId": directory.name,
        "mode": manifest["mode"],
        "launchSpecs": LAUNCH_SPECS,
        "orchestrationHealth": "PASS" if mechanical_ok else "FAIL",
        "mechanicalStatus": "ready_for_sol_gate" if mechanical_ok else "incomplete",
        "checks": checks,
        "unresolved": unresolved,
        "anchor": anchor,
        "contentVerdicts": [
            {
                "workerId": worker["worker_id"],
                "role": worker["role"],
                "verdict": worker.get("verdict"),
            }
            for worker in workers
            if worker.get("verdict") is not None
        ],
        "workers": wave_records(state),
        "createdWorktrees": created_worktrees,
        "timing": {
            "waveSeconds": round(time.time() - state["created_at"]),
            "workers": [
                {
                    "workerId": worker["worker_id"],
                    "seconds": round(worker["settled_at"] - worker["started_at"])
                    if worker.get("settled_at") and worker.get("started_at")
                    else None,
                }
                for worker in workers
            ],
        },
        "note": "Content verdicts remain inputs to the Sol gate; audit FAIL does not mean orchestration failed. Created worktrees must be integrated and removed before the next wave.",
        "createdAt": time.time(),
    }
    try:  # usage accounting is best-effort and never blocks finalization
        usage = wave_usage(directory, state)
        save_json(directory / "usage.json", usage)
        append_usage_log(usage)
        final["usage"] = {
            "byLaunch": usage["byLaunch"],
            "receipt": str(directory / "usage.json"),
        }
    except Exception as exc:
        final["usage"] = {"error": str(exc)}
    save_json(directory / "final.json", final)
    print(
        compact_json(
            {
                "orchestrationHealth": final["orchestrationHealth"],
                "mechanicalStatus": final["mechanicalStatus"],
                "checks": checks,
                "unresolved": unresolved,
                "contentVerdicts": final["contentVerdicts"],
                "createdWorktrees": created_worktrees,
                "finalReceipt": str(directory / "final.json"),
            }
        )
    )
    return 0 if mechanical_ok else 2


