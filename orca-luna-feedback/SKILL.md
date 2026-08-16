---
name: orca-luna-feedback
description: >-
  Write evidence-grounded post-wave feedback for orca-luna-cycle. A local
  helper scaffolds mechanical facts from the wave's receipt journal; Sol fills
  the verdict, per-worker keep/change deltas, manifest-executable next-wave
  adjustments, and skill gaps, then archives the note to this skill's durable
  log. Use after finalize-wave, on "wave feedback" or "retro" requests, or to
  read accumulated feedback before maintaining orca-luna-cycle. Not a
  substitute for the Sol gate.
---

# Orca Luna Feedback

Feedback is judgment over receipts, not vibes. The helper owns fact extraction;
Sol owns every opinion. A note that could have been written without reading the
journal is worthless — delete it rather than archive it.

## Hard rules

- Scaffold only after `finalize-wave` wrote `final.json`; the helper refuses
  otherwise. It never calls Orca and never touches the journal beyond adding
  `feedback.md`.
- Verdict first: one sentence a reader can act on without any other context.
- Every claim cites its source — a receipt file, a report field, or path:line.
  An uncited claim does not survive archiving; cut it.
- Keep orchestration mechanics and content quality separate. A healthy swarm
  with a FAIL review is mechanics PASS + content FAIL, never a blur.
- Per-worker: one keep and one change, tied to that worker's report. Delete the
  section for a worker with nothing material; "solid work" is slop.
- Next-wave adjustments must be manifest-executable: shard boundary, AC
  wording, launch choice, repair budget, worktree layout. "Communicate better"
  is not a change.
- Judge the subagent prompts themselves: read the rendered task specs in
  `tasks/` and say what in the goal/scope/AC/context wording helped or misled
  each worker. Prompt feedback must be prompt-executable — a concrete rewording
  or scope cut, cited against `tasks/<worker>.json`.
- Skill gaps are for orca-luna-cycle maintenance: contract friction, helper
  errors, invariants that fought the task — each with the exact receipt path.
- Quantify only what the journal holds (workers, verdicts, findings by
  severity, repair rounds). Never invent a number.

## Workflow

Scaffold the facts (refuses to overwrite an existing note):

```text
uv run --no-project <skill>/scripts/feedback.py scaffold \
  --receipt-dir /tmp/<wave>-receipts
```

Edit `feedback.md` in the receipt dir: replace every `<fill: ...>` placeholder
with judgment grounded in `final.json` and `reports/`, or delete optional
sections that have nothing material.

Archive — fails while any placeholder remains, then copies the note into this
skill's `log/` because receipt dirs are ephemeral:

```text
uv run --no-project <skill>/scripts/feedback.py archive \
  --receipt-dir /tmp/<wave>-receipts
```

Before maintaining orca-luna-cycle, read the accumulated log:

```text
uv run --no-project <skill>/scripts/feedback.py log --tail 5
```

After maintaining this skill, run:

```text
uv run --no-project <skill>/scripts/feedback.py self-test
```
