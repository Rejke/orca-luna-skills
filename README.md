# orca-luna-skills

Two agent skills for running supervised multi-model swarms in
[Orca](https://orca.dev)-managed worktrees.

- **orca-luna-cycle** — one GPT-5.6 Sol `xhigh` controller runs up to 10 fresh
  workers. Each role has a fixed model: Luna `max` for implementation (with an
  opt-in fast tier), Claude Fable 5 `high` for frontend UI, Sol `xhigh` for all
  review. Every step is written to receipt files. Workers ping the controller;
  the controller never polls. Waves cannot change code on unreviewed commits.
  A final check confirms the wave completed cleanly.
- **orca-luna-feedback** — feedback notes after each wave. A script fills in
  the facts from the wave's files; the controller adds verdicts and concrete
  changes for the next wave. A note cannot be archived while blanks remain.
  The log feeds maintenance of the cycle skill.

## Install

```bash
npx skills add rejke/orca-luna-skills
```

Or install one skill:

```bash
npx skills add rejke/orca-luna-skills --skill orca-luna-cycle
```

## Requirements

- Orca >= 1.4.184 with the orchestration contract, plus its CLI on PATH
- `codex` CLI (models are checked against `codex debug models` before launch)
- `claude` CLI on PATH only if you use the `fable-high` launch spec
- `uv` on PATH (helpers run via `uv run --no-project`, stdlib-only Python)

## Verify after install

```bash
uv run --no-project <skills>/orca-luna-cycle/scripts/orca_luna_worker.py self-test
uv run --no-project <skills>/orca-luna-cycle/tests/test_helper.py
uv run --no-project <skills>/orca-luna-feedback/scripts/feedback.py self-test
```
