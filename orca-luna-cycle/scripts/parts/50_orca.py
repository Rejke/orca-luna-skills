def resolve_orca() -> list[str]:
    configured = os.environ.get("ORCA_CLI_COMMAND", "").strip()
    if configured:
        command = shlex.split(configured)
        if not command:
            raise HelperError("ORCA_CLI_COMMAND is empty after parsing")
        return command
    if os.environ.get("ORCA_DEV_REPO_ROOT"):
        return ["orca-dev"]
    if sys.platform.startswith("linux"):
        return ["orca-ide"]
    return ["orca"]


def call_orca(arguments: list[str]) -> tuple[int, Any | None, str]:
    command = [*resolve_orca(), *arguments]
    try:
        result = subprocess.run(command, text=True, capture_output=True, check=False)
    except OSError as exc:
        raise HelperError(
            f"cannot run selected Orca CLI {command[0]!r}: {exc}"
        ) from exc
    payload: Any | None = None
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        pass
    detail = (result.stderr or result.stdout).strip()[-2000:]
    return result.returncode, payload, detail


def run_orca(arguments: list[str]) -> Any:
    returncode, payload, detail = call_orca(arguments)
    if returncode != 0:
        raise HelperError(f"Orca exited {returncode}: {detail}")
    if payload is None:
        raise HelperError(f"Orca returned non-JSON output: {detail}")
    return payload


def normalized_key(key: str) -> str:
    return re.sub(r"[^a-z0-9]", "", key.lower())


def walk(value: Any):
    yield value
    if isinstance(value, dict):
        for child in value.values():
            yield from walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child)


def find_entity_id(value: Any, entity: str) -> str | None:
    alias = normalized_key(f"{entity}_id")
    for node in walk(value):
        if not isinstance(node, dict):
            continue
        for key, child in node.items():
            if normalized_key(key) == alias and isinstance(child, str):
                return child
        nested = node.get(entity)
        if isinstance(nested, dict) and isinstance(nested.get("id"), str):
            return nested["id"]
        identifier = node.get("id")
        if isinstance(identifier, str) and re.match(
            rf"^{re.escape(entity)}[_-]", identifier
        ):
            return identifier
    return None


def find_terminal_handle(value: Any) -> str | None:
    preferred = {"agentterminalhandle", "terminalhandle"}
    for node in walk(value):
        if not isinstance(node, dict):
            continue
        for key, child in node.items():
            if (
                normalized_key(key) in preferred
                and isinstance(child, str)
                and child.startswith("term_")
            ):
                return child
    for node in walk(value):
        if not isinstance(node, dict):
            continue
        for key in ("handle", "id"):
            handle = node.get(key)
            if isinstance(handle, str) and handle.startswith("term_"):
                return handle
    return None


