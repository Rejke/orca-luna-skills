def wave_records(state: dict[str, Any]) -> list[dict[str, Any]]:
    keys = (
        "index",
        "worker_id",
        "role",
        "launch",
        "mutation",
        "label",
        "worktree_id",
        "terminal_handle",
        "spawn_command",
        "banner_proof",
        "start_status",
        "report_status",
        "task_status",
        "verdict",
        "question",
        "answers",
        "notification_status",
        "stop_status",
        "error",
    )
    return [
        {key: record[key] for key in keys if record.get(key) is not None}
        for record in state.get("workers", [])
    ]


# --- Token usage accounting -------------------------------------------------
# Sources of truth: codex rollout files (cumulative token_count events) and
# claude project transcripts (per-request usage blocks). A worker's session is
# the one whose cwd matches the worker terminal's worktree path and whose
# start timestamp falls inside the worker's spawn window.

CODEX_SESSION_ROOTS = (
    Path.home() / ".local/share/orca/codex-runtime-home/home/sessions",
    Path.home() / ".codex" / "sessions",
)
CLAUDE_PROJECTS_ROOT = Path.home() / ".claude" / "projects"
USAGE_LOG = Path.home() / ".codex" / "skills" / "orca-luna-cycle" / "log" / "usage.jsonl"
USAGE_SLACK_SECONDS = 120.0


def parse_iso_timestamp(value: Any) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.datetime.fromisoformat(
            value.replace("Z", "+00:00")
        ).timestamp()
    except ValueError:
        return None


def worktree_id_path(worktree_id: Any) -> str | None:
    """Filesystem path an Orca terminal worktreeId encodes after '::'."""
    if not isinstance(worktree_id, str) or "::" not in worktree_id:
        return None
    raw = worktree_id.split("::", 1)[1].replace("\\", "/")
    while raw.startswith("//"):
        raw = raw[1:]
    if raw.startswith("/wsl.localhost/"):
        parts = raw.split("/", 3)
        raw = "/" + parts[3] if len(parts) > 3 and parts[3] else ""
    return raw or None


def worker_terminal_cwd(
    directory: Path, worker_id: Any
) -> tuple[str | None, float | None]:
    path = directory / "terminals" / f"{worker_id}.json"
    try:
        receipt = load_json(path)
        created = path.stat().st_mtime
    except Exception:
        return None, None
    return worktree_id_path(first_named(receipt, "worktreeId")), created


def codex_session_head(path: Path) -> dict[str, Any] | None:
    try:
        with path.open(encoding="utf-8") as stream:
            head = json.loads(stream.readline())
    except (OSError, json.JSONDecodeError):
        return None
    if head.get("type") != "session_meta":
        return None
    payload = head.get("payload") or {}
    started = parse_iso_timestamp(payload.get("timestamp"))
    if started is None:
        return None
    return {"path": path, "cwd": payload.get("cwd"), "startedAt": started}


def codex_session_index(
    roots: Iterable[Path], not_before: float
) -> list[dict[str, Any]]:
    sessions: list[dict[str, Any]] = []
    seen: set[str] = set()
    for root in roots:
        if not root.is_dir():
            continue
        for path in root.glob("*/*/*/rollout-*.jsonl"):
            try:
                resolved = str(path.resolve())
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if resolved in seen or mtime < not_before:
                continue
            seen.add(resolved)
            head = codex_session_head(path)
            if head:
                sessions.append(head)
    sessions.sort(key=lambda item: item["startedAt"])
    return sessions


def read_codex_session(path: Path) -> dict[str, Any]:
    model = effort = plan = None
    totals: dict[str, Any] | None = None
    quota_start = quota_end = None
    try:
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                payload = entry.get("payload") or {}
                kind = entry.get("type")
                if kind == "turn_context":
                    model = payload.get("model") or model
                    settings = (
                        payload.get("collaboration_mode") or {}
                    ).get("settings") or {}
                    effort = (
                        settings.get("reasoning_effort")
                        or payload.get("effort")
                        or effort
                    )
                elif kind == "event_msg" and payload.get("type") == "token_count":
                    info = payload.get("info") or {}
                    totals = info.get("total_token_usage") or totals
                    limits = payload.get("rate_limits") or {}
                    plan = limits.get("plan_type") or plan
                    percent = (limits.get("primary") or {}).get("used_percent")
                    if isinstance(percent, (int, float)):
                        quota_start = percent if quota_start is None else quota_start
                        quota_end = percent
    except OSError:
        pass
    tokens = None
    if totals:
        tokens = {
            "input": totals.get("input_tokens"),
            "cachedInput": totals.get("cached_input_tokens"),
            "cacheWriteInput": totals.get("cache_write_input_tokens"),
            "output": totals.get("output_tokens"),
            "reasoningOutput": totals.get("reasoning_output_tokens"),
            "total": totals.get("total_tokens"),
        }
    quota = None
    if quota_end is not None or plan:
        quota = {
            "startPercent": quota_start,
            "endPercent": quota_end,
            "planType": plan,
        }
    return {"model": model, "effort": effort, "tokens": tokens, "quota": quota}


def read_claude_session(path: Path) -> dict[str, Any] | None:
    started = cwd = model = None
    sums = {"input": 0, "cachedInput": 0, "cacheWriteInput": 0, "output": 0}
    requests = 0
    try:
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                stamp = parse_iso_timestamp(entry.get("timestamp"))
                if stamp is not None and (started is None or stamp < started):
                    started = stamp
                cwd = entry.get("cwd") or cwd
                if entry.get("type") != "assistant":
                    continue
                message = entry.get("message") or {}
                usage = message.get("usage") or {}
                if not usage:
                    continue
                requests += 1
                model = message.get("model") or model
                sums["input"] += usage.get("input_tokens") or 0
                sums["cachedInput"] += usage.get("cache_read_input_tokens") or 0
                sums["cacheWriteInput"] += (
                    usage.get("cache_creation_input_tokens") or 0
                )
                sums["output"] += usage.get("output_tokens") or 0
    except OSError:
        return None
    if started is None:
        return None
    tokens = None
    if requests:
        tokens = dict(sums, reasoningOutput=None, total=sum(sums.values()))
    return {"startedAt": started, "cwd": cwd, "model": model, "tokens": tokens}


def claude_session_index(
    root: Path, cwd: str | None, not_before: float
) -> list[dict[str, Any]]:
    if not cwd or not root.is_dir():
        return []
    slug_dir = root / re.sub(r"[^A-Za-z0-9-]", "-", cwd)
    folders = (
        [slug_dir]
        if slug_dir.is_dir()
        else [child for child in root.iterdir() if child.is_dir()]
    )
    sessions: list[dict[str, Any]] = []
    for folder in folders:
        for path in folder.glob("*.jsonl"):
            try:
                if path.stat().st_mtime < not_before:
                    continue
            except OSError:
                continue
            summary = read_claude_session(path)
            if summary and summary.get("cwd") == cwd:
                summary["path"] = path
                sessions.append(summary)
    sessions.sort(key=lambda item: item["startedAt"])
    return sessions


def usage_by_launch(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    buckets: dict[str, dict[str, Any]] = {}
    for row in rows:
        bucket = buckets.setdefault(
            str(row.get("launch")),
            {
                "workers": 0,
                "matched": 0,
                "totalTokens": 0,
                "outputTokens": 0,
                "wallSeconds": 0,
            },
        )
        bucket["workers"] += 1
        tokens = row.get("tokens")
        if tokens:
            bucket["matched"] += 1
            bucket["totalTokens"] += tokens.get("total") or 0
            bucket["outputTokens"] += tokens.get("output") or 0
        if row.get("wallSeconds"):
            bucket["wallSeconds"] += row["wallSeconds"]
    return buckets


def wave_usage(
    directory: Path,
    state: dict[str, Any],
    *,
    codex_roots: Iterable[Path] = CODEX_SESSION_ROOTS,
    claude_root: Path = CLAUDE_PROJECTS_ROOT,
    now: float | None = None,
) -> dict[str, Any]:
    now = time.time() if now is None else now
    wave_start = state.get("created_at") or now
    codex_sessions = codex_session_index(
        codex_roots, wave_start - USAGE_SLACK_SECONDS
    )
    claimed: set[str] = set()
    rows: list[dict[str, Any]] = []
    for record in state.get("workers", []):
        launch = record.get("launch")
        spec = LAUNCH_SPECS.get(launch) or {}
        worker_id = record.get("worker_id")
        cwd, terminal_created = worker_terminal_cwd(directory, worker_id)
        window_start = (terminal_created or wave_start) - USAGE_SLACK_SECONDS
        window_end = record.get("settled_at") or now
        row: dict[str, Any] = {
            "workerId": worker_id,
            "role": record.get("role"),
            "launch": launch,
            "agent": spec.get("agent"),
            "serviceTier": "priority"
            if spec.get("speedTier") == "fast"
            else "standard",
            "wallSeconds": round(record["settled_at"] - record["started_at"])
            if record.get("settled_at") and record.get("started_at")
            else None,
            "session": None,
            "match": "none",
            "model": None,
            "effort": None,
            "tokens": None,
            "quota": None,
        }
        if spec.get("agent") == "codex":
            candidates = [
                item
                for item in codex_sessions
                if str(item["path"]) not in claimed
                and item.get("cwd") == cwd
                and window_start <= item["startedAt"] <= window_end
            ]
            if candidates:
                chosen = candidates[0]
                claimed.add(str(chosen["path"]))
                detail = read_codex_session(chosen["path"])
                row.update(
                    session=str(chosen["path"]),
                    match="exact" if len(candidates) == 1 else "ordered",
                    model=detail["model"],
                    effort=detail["effort"],
                    tokens=detail["tokens"],
                    quota=detail["quota"],
                )
        elif spec.get("agent") == "claude":
            candidates = [
                item
                for item in claude_session_index(claude_root, cwd, window_start)
                if str(item["path"]) not in claimed
                and window_start <= item["startedAt"] <= window_end
            ]
            if candidates:
                chosen = candidates[0]
                claimed.add(str(chosen["path"]))
                row.update(
                    session=str(chosen["path"]),
                    match="exact" if len(candidates) == 1 else "ordered",
                    model=chosen.get("model"),
                    tokens=chosen.get("tokens"),
                )
        rows.append(row)
    return {
        "usageSchemaVersion": 1,
        "runId": directory.name,
        "workers": rows,
        "byLaunch": usage_by_launch(rows),
        "createdAt": now,
    }


def append_usage_log(usage: dict[str, Any], log_path: Path = USAGE_LOG) -> int:
    """Append one analytics line per worker; a (runId, workerId) pair appears once."""
    existing: set[tuple[str, str]] = set()
    if log_path.exists():
        for line in log_path.read_text(encoding="utf-8").splitlines():
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            existing.add((str(entry.get("runId")), str(entry.get("workerId"))))
    log_path.parent.mkdir(parents=True, exist_ok=True)
    added = 0
    with log_path.open("a", encoding="utf-8") as stream:
        for row in usage["workers"]:
            key = (str(usage.get("runId")), str(row.get("workerId")))
            if key in existing:
                continue
            stream.write(compact_json({"runId": usage.get("runId"), **row}) + "\n")
            added += 1
    return added


def command_usage(args: argparse.Namespace) -> int:
    directory = receipt_dir(args.receipt_dir)
    state = read_wave_state(directory, allow_foreign=True)
    usage = wave_usage(directory, state)
    save_json(directory / "usage.json", usage)
    added = append_usage_log(usage)
    print(
        compact_json(
            {
                "status": "ok",
                "byLaunch": usage["byLaunch"],
                "unmatched": [
                    row["workerId"]
                    for row in usage["workers"]
                    if not row.get("tokens")
                ],
                "receipt": str(directory / "usage.json"),
                "log": str(USAGE_LOG),
                "logged": added,
            }
        )
    )
    return 0


