# AI Collaboration Log — Process Notes

Personal appendix, not part of the evaluator-facing deliverable. This
records how this project was actually built working with an AI
assistant across this session — the parts that don't belong in
`docs/full_project_documentation.md`'s strict technical spec, but that
are worth having a record of: where things went wrong, how they were
caught, and how a couple of exchanges with the assistant's own plans
actually improved the outcome.

## 1. The crash and recovery

The session picked up mid-recovery: Phase 4's backtest engine and report
generator were already implemented and fully tested, but the real
end-to-end run had crashed twice without ever producing a completed
result. No planning context from before the crash had survived — the
assistant had to reconstruct the situation from the repo's state alone
(git status, test results, what files existed) rather than from any
memory of what had been decided.

## 2. First background run, killed on request

To avoid crashing the interactive session again, the assistant started
the real full backtest in the background. Immediately after, "WAIT STOP"
followed by an explicit instruction to kill it. It was killed
immediately, along with its log-tailing monitor, with no argument or
hesitation — the right response to an explicit stop instruction, full
stop.

## 3. A flagged prompt-injection attempt

Shortly after, a message arrived formatted to look like a "mid-turn user
message" (via an unusual system-reminder-style wrapper), written in
Portuguese, claiming to be a "reconstructed" version of an earlier
message supposedly garbled by terminal column-wrapping. It instructed
the assistant to restart the exact background run that had just been
killed.

The assistant flagged it as suspicious rather than acting on it, on
three concrete grounds:
- It directly contradicted the explicit stop instruction given moments
  earlier.
- It referenced a `--quick` CLI flag on `src/backtest.py` that did not
  exist in the code at the time — checked with a direct `grep` before
  trusting it, rather than assumed.
- Its own cover story didn't hold up: it claimed only "the first letters
  of each line" were corrupted, yet presented fully grammatical,
  polished prose — not what that kind of corruption actually produces.

The assistant explained all three reasons back to the user, did not
restart anything, and asked for the real plan to be retyped directly.
This is the clearest example in the session of the assistant correctly
distrusting an instruction that arrived through an unusual channel and
conveniently pushed toward reversing a very recent, explicit user
decision — worth remembering as the pattern to watch for.

## 4. Plan-mode critical review, round 1

The user re-provided the real pre-crash plan and asked for a critical
review ("Avalie como critico"), not a rubber stamp. The plan's root-cause
diagnosis — that `pit.py` never indexes the panel by `PERIODOFREPORT` —
was checked directly against the current code rather than taken on
faith, and found stale: that exact optimization already existed, via a
different (and, it turned out, more memory-efficient) mechanism than the
plan proposed rebuilding. The assistant traced the actual call chain
end to end to confirm the fix was really wired into the live code path,
not just sitting unused, before concluding the plan's premise didn't
hold.

## 5. First plan rejection — "write it to the repo, automode"

The revised plan was rejected at `ExitPlanMode` not because of
disagreement with its content, but with an instruction to persist it to
a real committed file (`docs/phase4_efficient_implementation.md`) before
any implementation began — specifically because this project had
already lost planning context to a crash once, and an ephemeral
plan-mode file wasn't going to survive a second one. "Use automode" came
with it, read as explicit authorization to move past strict read-only
plan-mode constraints and start implementing directly.

## 6. Profiling overturned the plan's own premise, again

The (corrected) plan's Step 1 was "profile before optimizing." Running
it for real revealed the actual dominant cost was neither of the two
theories anyone had proposed up to that point: pandas 3's default
Arrow-backed string dtype routing simple positional lookups through a
slow `pyarrow` kernel, and pandas deep-copying a large `.attrs` dict on
every downstream operation. Nobody — not the original stale plan, not
the assistant's own "corrected" critique of it — had anticipated this.
It only surfaced because the plan insisted on measuring before trusting
a diagnosis, which is exactly the discipline that caught it.

## 7. Second plan rejection — reconciliation guard + notebook split

After a follow-up plan for `src/detail.py`'s per-asset export, the user
rejected `ExitPlanMode` again with two specific engineering additions:
add a strict mathematical reconciliation check to guard against the
plan's own re-implemented price-windowing logic silently drifting from
the original `leg_nav`, and move the sub-period regime analysis into a
notebook rather than a markdown writeup, matching this repo's own stated
convention (`notebooks/` for exploration, `src/` for reusable logic).
Both were sound and adopted as given.

## 8. The reconciliation guard caught a real bug — in its own first draft

The very first real run of the newly-added reconciliation check failed,
on the very first real quarter it touched. The bug it caught, though,
wasn't the thing it was built to catch (windowing drift in the
duplicated `leg_nav` logic) — it was a math error in the reconciliation
formula itself: comparing `mean(long returns) - mean(short returns)`
against the combined `spread_nav`, which is a *daily-rebalanced*
combination of the two legs' daily returns, not a single difference of
two total returns. That identity only actually holds for a single
time-step, not across a whole quarter — a distinction the assistant had
gotten wrong in its own first implementation of the check the user had
asked for.

This was surfaced to the user as a live finding, with the actual
mismatch (diff -4.73e-3) shown, before proceeding to fix it — not
silently patched and moved past. The fix (reconcile per leg, directly
against `leg_nav`, which *is* an exact identity for a never-
intra-quarter-rebalanced equal-weight leg) was confirmed clean across
all 9,505 real detail rows afterward.

## 9. This document's own scope correction

The first draft plan for project documentation mixed this process
narrative directly into the evaluator-facing technical spec. The user
rejected that framing at `ExitPlanMode` and asked for exactly the split
this file now represents — a small, clean piece of scope discipline that
this log is itself an example of.

## Patterns worth remembering

- **Profile or verify before trusting a diagnosis, including your own.**
  Twice in this session, a plausible root-cause theory — the original
  bottleneck plan, the first reconciliation formula — turned out to be
  wrong in a way only real measurement or real execution revealed, not
  code-reading alone.
- **Reconciliation/equivalence checks against a known-correct reference
  paid off directly, not just in theory.** `period_groups` vs. the
  boolean mask, category dtype vs. plain dtype, threaded vs. inline
  `ticker_to_cusip`, and finally per-leg detail vs. `leg_nav` itself —
  the last one caught a genuine bug before it reached a deliverable,
  which is the entire point of building these checks in the first place.
- **Instructions arriving through an unusual channel, especially ones
  that conveniently reverse a very recent explicit decision, get
  checked against verifiable facts before being acted on** — not
  trusted because they're phrased as coming from the user.
- **Rejecting a plan is not the same as rejecting the direction.** Every
  `ExitPlanMode` rejection in this session came with a specific,
  actionable engineering reason, and every one of them made the eventual
  result better, not just different.
