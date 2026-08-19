def version_key(version: str) -> tuple[int, ...]:
    numbers = [int(item) for item in re.findall(r"[0-9]+", version)]
    return tuple((numbers + [0, 0, 0, 0])[:4])


def first_named(value: Any, name: str) -> Any:
    wanted = normalized_key(name)
    for node in walk(value):
        if not isinstance(node, dict):
            continue
        for key, child in node.items():
            if normalized_key(key) == wanted:
                return child
    return None


def runtime_identity(status: Any) -> dict[str, Any]:
    version = first_named(status, "appVersion")
    runtime_id = first_named(status, "runtimeId")
    capabilities = first_named(status, "capabilities")
    state = first_named(status, "state")
    return {
        "appVersion": version,
        "runtimeId": runtime_id,
        "state": state,
        "capabilities": capabilities if isinstance(capabilities, list) else [],
    }


def command_registry(context: Any) -> dict[str, dict[str, Any]]:
    commands = first_named(context, "commands")
    if not isinstance(commands, list):
        raise HelperError("agent-context did not contain a commands array")
    registry: dict[str, dict[str, Any]] = {}
    for command in commands:
        if not isinstance(command, dict):
            continue
        name = command.get("command")
        if isinstance(name, str):
            registry[name] = command
    return registry


def command_contract_check(
    context: Any,
) -> tuple[list[dict[str, Any]], str]:
    registry = command_registry(context)
    checks: list[dict[str, Any]] = []
    relevant: dict[str, Any] = {}
    for command, required_flags in REQUIRED_ORCA_COMMANDS.items():
        required_flags = set(required_flags)
        spec = registry.get(command)
        flags = set(spec.get("flags", [])) if isinstance(spec, dict) else set()
        missing = sorted(required_flags - flags)
        argument_mode = spec.get("argumentMode") if isinstance(spec, dict) else None
        passed = spec is not None and argument_mode == "parsed" and not missing
        checks.append(
            {
                "name": f"command:{command}",
                "passed": passed,
                "argumentMode": argument_mode,
                "missingFlags": missing,
            }
        )
        if spec is not None:
            relevant[command] = {
                "argumentMode": spec.get("argumentMode"),
                "usage": spec.get("usage"),
                "flags": sorted(flags),
            }
    contract_hash = hashlib.sha256(compact_json(relevant).encode("utf-8")).hexdigest()
    return checks, contract_hash


def codex_model_catalog() -> tuple[dict[str, dict[str, set[str]]], str | None]:
    configured = os.environ.get("ORCA_LUNA_CODEX_COMMAND", "codex").strip()
    command = shlex.split(configured)
    if not command:
        return {}, "ORCA_LUNA_CODEX_COMMAND is empty after parsing"
    if shutil.which(command[0]) is None:
        return {}, f"{command[0]!r} is not on PATH"
    try:
        result = subprocess.run(
            [*command, "debug", "models"],
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        return {}, str(exc)
    if result.returncode != 0:
        return {}, (result.stderr or result.stdout).strip()[-1000:]
    try:
        catalog = json.loads(result.stdout)
    except json.JSONDecodeError:
        return {}, "codex debug models returned non-JSON output"
    models = catalog.get("models", []) if isinstance(catalog, dict) else []
    supported: dict[str, dict[str, set[str]]] = {}
    for item in models:
        if not isinstance(item, dict) or not isinstance(item.get("slug"), str):
            continue
        supported[item["slug"]] = {
            "efforts": {
                level.get("effort")
                for level in item.get("supported_reasoning_levels", [])
                if isinstance(level, dict) and isinstance(level.get("effort"), str)
            },
            "speedTiers": {
                tier
                for tier in item.get("additional_speed_tiers", [])
                if isinstance(tier, str)
            },
        }
    return supported, None


def launch_checks(workers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Verify every launch spec the wave uses; the codex catalog is read once."""
    checks: list[dict[str, Any]] = []
    codex_catalog: dict[str, dict[str, set[str]]] | None = None
    codex_error: str | None = None
    for alias in sorted({worker["launch"] for worker in workers}):
        spec = LAUNCH_SPECS[alias]
        name = f"launch:{alias}"
        if spec["agent"] == "codex":
            if codex_catalog is None and codex_error is None:
                codex_catalog, codex_error = codex_model_catalog()
            if codex_error is not None:
                checks.append({"name": name, "passed": False, "error": codex_error})
                continue
            speed_tier = spec.get("speedTier")
            entry = (codex_catalog or {}).get(spec["model"], {})
            efforts = entry.get("efforts", set())
            speed_tiers = entry.get("speedTiers", set())
            # The check uses spec["model"]/spec["effort"] verbatim — the same
            # strings worker-start sends. A composed id (for example a "[fast]"
            # suffix) is not in the catalog and fails here instead of at launch.
            checks.append(
                {
                    "name": name,
                    "passed": "[" not in spec["model"]
                    and spec["effort"] in efforts
                    and (speed_tier is None or speed_tier in speed_tiers),
                    "model": spec["model"],
                    "effort": spec["effort"],
                    "speedTier": speed_tier,
                    "supportedEfforts": sorted(efforts),
                    "supportedSpeedTiers": sorted(speed_tiers),
                }
            )
        else:
            configured = os.environ.get("ORCA_LUNA_CLAUDE_COMMAND", "claude").strip()
            command = shlex.split(configured)
            executable = shutil.which(command[0]) if command else None
            checks.append(
                {
                    "name": name,
                    "passed": executable is not None,
                    "model": spec["model"],
                    "effort": spec["effort"],
                    **(
                        {"executable": str(Path(executable).resolve())}
                        if executable
                        else {"error": f"{configured!r} is not on PATH"}
                    ),
                }
            )
    return checks


def preflight_manifest(
    manifest: dict[str, Any], workers: list[dict[str, Any]]
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    controller_terminal_handle = os.environ.get("ORCA_TERMINAL_HANDLE", "").strip()
    checks.append(
        {
            "name": "controller-terminal-handle",
            "passed": bool(
                re.fullmatch(r"term_[A-Za-z0-9_-]+", controller_terminal_handle)
            ),
            "terminalHandle": controller_terminal_handle or None,
        }
    )
    status_code, status, status_detail = call_orca(["status", "--json"])
    if status_code != 0 or status is None:
        return {
            "status": "failed",
            "manifestSha256": manifest_digest(manifest),
            "checks": [
                {
                    "name": "orca-status",
                    "passed": False,
                    "error": status_detail,
                }
            ],
        }
    runtime = runtime_identity(status)
    app_version = runtime.get("appVersion")
    runtime_ready = runtime.get("state") == "ready"
    checks.append({"name": "runtime-ready", "passed": runtime_ready})
    checks.append(
        {
            "name": "minimum-orca-version",
            "passed": isinstance(app_version, str)
            and version_key(app_version) >= version_key(MIN_ORCA_VERSION),
            "actual": app_version,
            "minimum": MIN_ORCA_VERSION,
        }
    )

    context_code, context, context_detail = call_orca(["agent-context", "--json"])
    contract_hash = None
    contract_version = None
    if context_code == 0 and context is not None:
        contract_version = (
            context.get("schemaVersion") if isinstance(context, dict) else None
        )
        try:
            command_checks, contract_hash = command_contract_check(context)
        except HelperError as exc:
            checks.append(
                {
                    "name": "agent-context-contract",
                    "passed": False,
                    "error": str(exc),
                }
            )
        else:
            checks.extend(command_checks)
    else:
        checks.append(
            {
                "name": "agent-context",
                "passed": False,
                "error": context_detail,
            }
        )
    checks.extend(launch_checks(workers))
    checks.append(
        {
            "name": "uv-on-path",
            "passed": shutil.which("uv") is not None,
        }
    )


    selectors = sorted(
        {
            worker["worktree"]
            for worker in workers
            if worker["worktree"] not in {"new-child", "new-top-level"}
        }
    )
    needs_current = any(
        worker["worktree"] in {"current", "new-child", "new-top-level"}
        for worker in workers
    )
    resolved_worktrees: dict[str, str] = {}
    if needs_current:
        returncode, payload, detail = call_orca(["worktree", "current", "--json"])
        worktree_id = (
            find_entity_id(payload, "worktree") if payload is not None else None
        )
        passed = returncode == 0 and payload is not None and worktree_id is not None
        checks.append(
            {
                "name": "worktree:current",
                "passed": passed,
                "worktreeId": worktree_id,
                **(
                    {}
                    if passed
                    else {
                        "error": detail
                        or "worktree current receipt omitted the worktree ID"
                    }
                ),
            }
        )
        if worktree_id is not None:
            resolved_worktrees["current"] = worktree_id
    for selector in selectors:
        if selector == "current":
            continue
        returncode, payload, detail = call_orca(
            ["worktree", "show", "--worktree", selector, "--json"]
        )
        worktree_id = (
            find_entity_id(payload, "worktree") if payload is not None else None
        )
        passed = returncode == 0 and payload is not None and worktree_id is not None
        checks.append(
            {
                "name": f"worktree:{selector}",
                "passed": passed,
                "worktreeId": worktree_id,
                **(
                    {}
                    if passed
                    else {
                        "error": detail
                        or "worktree show receipt omitted the worktree ID"
                    }
                ),
            }
        )
        if worktree_id is not None:
            resolved_worktrees[selector] = worktree_id

    mutator_targets: list[str] = []
    unresolved_mutators: list[str] = []
    for worker in workers:
        if worker["mutation"] != "allowed":
            continue
        selector = worker["worktree"]
        if selector in {"new-child", "new-top-level"}:
            mutator_targets.append(f"new:{worker['id']}")
            continue
        target = resolved_worktrees.get(selector)
        if target is None:
            unresolved_mutators.append(worker["id"])
        else:
            mutator_targets.append(target)
    duplicate_mutator_targets = sorted(
        {target for target in mutator_targets if mutator_targets.count(target) > 1}
    )
    checks.append(
        {
            "name": "mutator-worktree-isolation",
            "passed": not unresolved_mutators and not duplicate_mutator_targets,
            "unresolvedWorkers": unresolved_mutators,
            "duplicateWorktreeIds": duplicate_mutator_targets,
        }
    )

    resolved_orca = resolve_orca()
    executable = shutil.which(resolved_orca[0]) or resolved_orca[0]
    passed = all(check.get("passed") is True for check in checks)
    return {
        "status": "passed" if passed else "failed",
        "manifestSha256": manifest_digest(manifest),
        "createdAt": time.time(),
        "launchSpecs": LAUNCH_SPECS,
        "controllerTerminalHandle": controller_terminal_handle,
        "orca": {
            "command": resolved_orca,
            "executable": str(Path(executable).resolve())
            if Path(executable).exists()
            else executable,
            "appVersion": app_version,
            "runtimeId": runtime.get("runtimeId"),
            "contractVersion": contract_version,
            "contractHash": contract_hash,
        },
        "checks": checks,
    }


def snapshot_external_scope(
    directory: Path, workers: list[dict[str, Any]], repo_root: Path | None = None
) -> dict[str, str]:
    """Copy out-of-repo scope files into receipts.

    Plans and reports in /tmp mutate between waves; the receipt copy is the
    version this wave actually reviewed, so later citations resolve.
    """
    root = (repo_root or Path.cwd()).resolve()
    copies: dict[str, str] = {}
    target_dir = directory / "scope"
    for worker in workers:
        for entry in worker.get("scope") or []:
            if not isinstance(entry, str) or not entry.startswith("/"):
                continue
            source = Path(entry)
            try:
                if not source.is_file() or source.resolve().is_relative_to(root):
                    continue
            except OSError:
                continue
            if str(source) in copies:
                continue
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            target_dir.mkdir(parents=True, exist_ok=True)
            target = target_dir / f"{digest[:12]}-{source.name}"
            if not target.exists():
                shutil.copy2(source, target)
            copies[str(source)] = str(target)
    return copies


def command_preflight(args: argparse.Namespace) -> int:
    manifest, workers, prompts = validate_manifest(load_json(args.manifest))
    directory = receipt_dir(args.receipt_dir)
    if (directory / STATE_FILE).exists():
        raise HelperError("cannot preflight over an existing wave state")
    ensure_journal(directory)
    archived = archive_helper(directory)
    save_json(directory / "manifest.json", manifest)
    live_prompts = [
        runtime_prompt(prompt, directory, worker["id"])
        for worker, prompt in zip(workers, prompts)
    ]
    receipt = preflight_manifest(manifest, workers)
    baseline = git_snapshot()
    receipt["gitBaseline"] = baseline
    declared = manifest["envelope"].get("baseAnchor")
    anchor_ok = (
        baseline.get("status") != "captured"
        or declared in (None, "")
        or declared == baseline.get("head")
    )
    receipt["checks"].append(
        {
            "name": "declaredAnchorMatchesWorktree",
            "passed": anchor_ok,
            "detail": None
            if anchor_ok
            else f"envelope.baseAnchor {declared} != measured HEAD "
            f"{baseline.get('head')}",
        }
    )
    if not anchor_ok:
        receipt["status"] = "failed"
    receipt["scopeSnapshot"] = snapshot_external_scope(directory, workers)
    receipt["helper"] = {"sha256": helper_digest(), "archived": str(archived)}
    receipt["workers"] = [
        {
            "id": worker["id"],
            "role": worker["role"],
            "launch": worker["launch"],
            "worktree": worker["worktree"],
            "mutation": worker["mutation"],
            "label": worker_label(worker, index),
            "promptChars": len(prompt),
            "overBudget": len(prompt) > PROMPT_BUDGET_CHARS,
        }
        for index, (worker, prompt) in enumerate(zip(workers, live_prompts), start=1)
    ]
    save_json(directory / "preflight.json", receipt)
    failed = [check["name"] for check in receipt["checks"] if not check.get("passed")]
    oversized = [
        entry["id"] for entry in receipt["workers"] if entry["overBudget"]
    ]
    print(
        compact_json(
            {
                "status": receipt["status"],
                "launches": sorted({worker["launch"] for worker in workers}),
                "runtime": receipt.get("orca"),
                "failedChecks": failed,
                **(
                    {
                        "oversizedPrompts": oversized,
                        "note": f"trim manifest prose; budget is {PROMPT_BUDGET_CHARS} chars per spec",
                    }
                    if oversized
                    else {}
                ),
                "receipts": str(directory),
            }
        )
    )
    return 0 if receipt["status"] == "passed" else 2


def verify_preflight(
    directory: Path,
    manifest: dict[str, Any],
    workers: list[dict[str, Any]],
    prompts: list[str],
) -> dict[str, Any]:
    path = directory / "preflight.json"
    if not path.exists():
        raise HelperError(f"dispatch requires a successful preflight receipt: {path}")
    previous = load_json(path)
    if not isinstance(previous, dict) or previous.get("status") != "passed":
        raise HelperError("preflight receipt is not successful")
    digest = manifest_digest(manifest)
    if previous.get("manifestSha256") != digest:
        raise HelperError("manifest changed after preflight; run preflight again")
    previous_helper = previous.get("helper", {})
    if previous_helper.get("sha256") != helper_digest():
        raise HelperError("helper changed after preflight; run preflight again")
    if not archived_helper(directory).exists():
        archive_helper(directory)
    for prompt in prompts:
        runtime_prompt(prompt, directory, "size-check")

    current = preflight_manifest(manifest, workers)
    save_json(directory / "runtime" / "pre-dispatch.json", current)
    if current.get("status") != "passed":
        failed = [
            check.get("name")
            for check in current.get("checks", [])
            if isinstance(check, dict) and check.get("passed") is not True
        ]
        raise HelperError(f"preflight is no longer valid; failed checks: {failed}")
    previous_orca = previous.get("orca", {})
    current_orca = current.get("orca", {})
    identity_fields = (
        "command",
        "executable",
        "appVersion",
        "runtimeId",
        "contractVersion",
        "contractHash",
    )
    changed = [
        field
        for field in identity_fields
        if previous_orca.get(field) != current_orca.get(field)
    ]
    if changed:
        raise HelperError(
            "Orca identity/contract changed after preflight "
            f"({', '.join(changed)}); run preflight again"
        )
    def worktree_mapping(receipt: dict[str, Any]) -> dict[str, Any]:
        return {
            check["name"]: check.get("worktreeId")
            for check in receipt.get("checks", [])
            if isinstance(check, dict)
            and str(check.get("name", "")).startswith("worktree:")
        }

    if worktree_mapping(previous) != worktree_mapping(current):
        raise HelperError(
            "worktree mapping changed after preflight; run preflight again"
        )
    if previous.get("controllerTerminalHandle") != current.get(
        "controllerTerminalHandle"
    ):
        raise HelperError(
            "controller terminal changed after preflight; run preflight again from "
            "the terminal that will own the wave"
        )
    return current


def command_prompt(args: argparse.Namespace) -> int:
    _, workers, prompts = validate_manifest(load_json(args.manifest))
    if args.worker:
        try:
            index = next(
                i for i, worker in enumerate(workers) if worker["id"] == args.worker
            )
        except StopIteration as exc:
            raise HelperError(f"manifest has no worker {args.worker!r}") from exc
    else:
        index = 0
    print(prompts[index], end="")
    return 0


