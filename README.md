# orca-luna-skills

Two companion agent skills for supervised multi-model swarm orchestration in
[Orca](https://orca.dev)-managed worktrees.

- **orca-luna-cycle** — a GPT-5.6 Sol `xhigh` controller dispatches up to 10
  fresh workers under a pinned launch policy (Luna `max` / Terra `xhigh` /
  Claude Fable 5 `high` for implementation, Sol `xhigh` pinned for all review),
  with a durable receipt journal, push-only wake flow, reviewed-anchor gate,
  and mechanical finalize reconciliation.
- **orca-luna-feedback** — evidence-grounded post-wave retro notes: a helper
  scaffolds mechanical facts from the wave journal, the controller fills
  verdicts and manifest-executable adjustments, and archiving refuses unfilled
  placeholders. Its durable log feeds maintenance of the cycle skill.

## Install

```bash
npx skills add rejke/orca-luna-skills
```

Or install a single skill:

```bash
npx skills add rejke/orca-luna-skills --skill orca-luna-cycle
```

## Requirements

- Orca >= 1.4.184 with the orchestration contract, plus its CLI on PATH
- `codex` CLI (models are verified against `codex debug models` at preflight)
- `claude` CLI on PATH only if you use the `fable-high` launch spec
- `uv` on PATH (helpers run via `uv run --no-project`, stdlib-only Python)

## Verify after install

```bash
uv run --no-project <skills>/orca-luna-cycle/scripts/orca_luna_worker.py self-test
uv run --no-project <skills>/orca-luna-cycle/tests/test_helper.py
uv run --no-project <skills>/orca-luna-feedback/scripts/feedback.py self-test
```
