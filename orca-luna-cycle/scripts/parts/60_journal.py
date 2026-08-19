def receipt_dir(requested: str | None) -> Path:
    if not requested:
        raise HelperError(
            "live wave commands require an explicit absolute --receipt-dir"
        )
    raw = Path(requested).expanduser()
    if not raw.is_absolute():
        raise HelperError(
            "receipt-dir must be an absolute path outside the repository"
        )
    path = raw.resolve()
    try:
        probe = subprocess.run(
            ["git", "-C", str(Path.cwd()), "rev-parse", "--show-toplevel"],
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError:
        probe = None
    if probe is not None and probe.returncode == 0:
        repo_root = Path(probe.stdout.strip()).resolve()
        if path == repo_root or repo_root in path.parents:
            raise HelperError(
                "receipt-dir must not be inside the active Git repository"
            )
    path.mkdir(parents=True, exist_ok=True)
    return path


def save_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=str(path.parent)
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


@contextmanager
def wave_lock(directory: Path):
    with (directory / "wave-state.lock").open("a+", encoding="utf-8") as handle:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def read_wave_state(directory: Path, *, allow_foreign: bool = False) -> dict[str, Any]:
    state = load_json(directory / STATE_FILE)
    if not isinstance(state, dict) or state.get("version") != STATE_VERSION:
        raise HelperError(
            f"invalid or unsupported wave state in {directory / STATE_FILE}"
        )
    if allow_foreign:
        # Read-only accounting over past waves; the dispatching build no longer
        # matters because nothing here touches the wave.
        return state
    expected_helper = state.get("helper_sha256")
    if expected_helper and expected_helper != helper_digest():
        raise HelperError(
            "this wave was dispatched by a different helper build; run the "
            f"archived copy instead: {archived_helper(directory)}"
        )
    if state.get("launch_specs") != LAUNCH_SPECS:
        raise HelperError(
            "wave was journaled under a different launch policy; use the "
            f"archived helper copy: {archived_helper(directory)}"
        )
    return state


def mutate_wave_state(
    directory: Path, update: Callable[[dict[str, Any]], None]
) -> dict[str, Any]:
    with wave_lock(directory):
        state = read_wave_state(directory)
        update(state)
        state["updated_at"] = time.time()
        save_json(directory / STATE_FILE, state)
        return state


def update_worker_state(directory: Path, index: int, **changes: Any) -> dict[str, Any]:
    def update(state: dict[str, Any]) -> None:
        for record in state.get("workers", []):
            if record.get("index") == index:
                record.update(changes)
                return
        raise HelperError(f"wave state has no worker index {index}")

    return mutate_wave_state(directory, update)


def set_wave_phase(directory: Path, phase: str, *, error: str | None = None) -> None:
    def update(state: dict[str, Any]) -> None:
        state["phase"] = phase
        if error is None:
            state.pop("error", None)
        else:
            state["error"] = error

    mutate_wave_state(directory, update)


def cancel_requested(directory: Path) -> bool:
    return (directory / CANCEL_FILE).exists()


def request_cancel(directory: Path) -> None:
    save_json(
        directory / CANCEL_FILE,
        {"requested": True, "requested_at": time.time(), "requesting_pid": os.getpid()},
    )

    def update(state: dict[str, Any]) -> None:
        state["phase"] = "cancelling"
        state["cancel_requested"] = True

    mutate_wave_state(directory, update)


def worker_label(worker: dict[str, Any], index: int) -> str:
    raw = worker.get("displayName") or worker.get("name") or worker["goal"]
    concise = re.sub(r"\s+", " ", str(raw)).strip().rstrip(".")
    prefix = f"{index:02d} {worker['role']} · "
    available = max(12, 72 - len(prefix))
    if len(concise) > available:
        concise = concise[: available - 1].rstrip() + "…"
    return prefix + concise


def manifest_digest(manifest: dict[str, Any]) -> str:
    encoded = compact_json(manifest).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def helper_digest() -> str:
    return hashlib.sha256(Path(__file__).resolve().read_bytes()).hexdigest()


def archived_helper(directory: Path) -> Path:
    return directory / "runtime" / "helper.py"


def archive_helper(directory: Path) -> Path:
    """Freeze the dispatching helper so mid-wave commands survive skill upgrades."""
    destination = archived_helper(directory)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(Path(__file__).resolve(), destination)
    return destination


def ensure_journal(directory: Path) -> None:
    for name in (
        "prompts",
        "worktrees",
        "terminals",
        "reports",
        "reports/incoming",
        "answers",
        "notifications",
        "runtime",
    ):
        (directory / name).mkdir(parents=True, exist_ok=True)


