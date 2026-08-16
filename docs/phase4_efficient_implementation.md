# Phase 4 Efficiency Fix — Closing the Real-Run Bottleneck

**Status: implemented and verified.** See "Actual root cause and fix
(found via profiling)" below — Step 1's profiling run revealed the plan
as originally scoped (Steps 2-4 further down) was necessary but not
sufficient; the real dominant cost was something neither this doc's first
draft nor the earlier stale plan anticipated. That's now fixed, tested,
and confirmed end-to-end via `--quick`. 118/118 tests pass (up from 113).

## Context

Phase 4's code (`src/backtest.py`, `src/report.py`) is implemented and all
113 tests pass, but the real end-to-end run (`python -m src.backtest`) has
crashed twice without ever producing `data/processed/backtest_results.pkl`
— once during an earlier session (killed after ~30 minutes of pure CPU
time with no completion in sight), and once when the interactive session
itself crashed mid-implementation of a fix for that.

A plan drafted before this session's crash diagnosed the bottleneck as
"`pit.py` never indexes the panel by `PERIODOFREPORT`" and proposed adding
that indexing from scratch. That diagnosis turned out to be **stale** —
the fix had already been implemented (presumably in the session that
crashed) but the plan describing it hadn't been persisted anywhere durable,
so the next session re-derived the same root cause from the call graph
without checking whether it was already fixed. This doc exists so that
doesn't happen a third time: the finalized plan is committed to the repo,
not just held in a planning tool's ephemeral state.

## What was already fixed (verified against the current code, not assumed)

`src/pit.py:_period_slice` has a `panel.attrs["period_groups"]` fast path:
`backtest.py`'s `main()` builds `panel.attrs["period_groups"] =
panel.groupby("PERIODOFREPORT", sort=False).indices` once, up front, and
`_period_slice` uses it for an O(k) `.iloc[positions]` lookup instead of
an O(n) boolean-mask scan over the full ~82M-row panel — falling back to
the boolean mask only when `.attrs` isn't set (which is why every existing
test, all built on plain DataFrames, still passes unmodified either way).

Confirmed this is actually wired into the real call path, not just present
in isolation: traced `backtest.quarter_pnl → factor.breadth_momentum →
factor.breadth_change → universe.sp500_universe_breadth ×2 →
pit.breadth → pit.as_of_snapshot → pit._period_slice` and checked that
`panel` is passed by reference, unchanged, at every level — never sliced
or copied before `_period_slice` sees it. `panel.attrs` therefore survives
intact through all ~306 lookups (3 lag values × ~51 quarters × 2 calls per
quarter) in a real grid run.

**Rejected as a next step**: rebuilding this via
`panel.set_index("PERIODOFREPORT", drop=False).sort_index()` (the original
plan's literal proposal) would sort/copy the full ~9GB frame — strictly
worse than the one-time single-column `groupby(...).indices` pass already
in place, and reintroduces real OOM risk on a 15GB-RAM machine. The
correct response to that OOM risk is not implementing it, not
`inplace=True` (which doesn't meaningfully reduce a sort's peak memory).

## What's actually still open

1. **Zero test coverage for the fast path that's actually live.**
   `tests/test_pit.py` and `tests/test_backtest.py` contain no reference
   to `.attrs`/`period_groups` at all — the code path every real run
   exercises is untested.
2. **`universe.sp500_members` re-reads and re-parses `sp500_history.csv`
   from disk, and rebuilds `ticker_to_cusip` from `mapping`, on every one
   of ~306 calls per run** — confirmed by reading `src/universe.py`
   directly. Both inputs are immutable within a single run.
3. No `--quick` dev-mode flag exists yet for fast iteration on a small
   window/single lag-cost combination.

## Actual root cause and fix (found via profiling)

Step 1 below was run for real: `cProfile` over 8 real quarters
(2022-2023, one lag value) against the actual full panel. Result:
`sp500_members` was **not** the dominant cost (its ~92s/full-grid share
was real but minor). Two other things were, both invisible from reading
the source alone:

1. **Pandas 3's default string dtype is Arrow-backed** (`future.infer_string`
   defaults to `True` in pandas 3.0.5, the version this project pins).
   `CIK`/`ACCESSION_NUMBER`/`CUSIP` load as Arrow-backed `str` columns, and
   `.iloc[positions]` on those routes through `pyarrow.compute.take`,
   which turned out to dominate runtime: **242s of the original 336.7s**
   (8 quarters) was inside that one kernel. The `period_groups` fix
   correctly cut the *row count* selected per call (O(k) not O(n)) but
   never touched this *per-element* cost, which turned out to be the
   bigger factor. **Fix**: convert `CIK`/`ACCESSION_NUMBER`/`CUSIP` to
   `category` dtype once, in a new `backtest.prepare_panel()`. Category
   codes are plain numpy integers, so `.take` becomes a cheap numpy
   operation, and it's *more* memory-compact than either representation
   here (bounded cardinality: far fewer distinct CIKs/CUSIPs/accession
   numbers than 81.8M rows) — measured peak RSS was flat (~9.3-9.4GB)
   before and after, not higher.
2. **pandas deep-copies `.attrs` via `__finalize__` on every derived
   object.** `period_groups` covers the whole panel (~650MB of position
   arrays); once `_period_slice` returns a slice carrying that same
   `.attrs`, every subsequent op on it (`.sort_values`, `.groupby`,
   `.drop_duplicates`, ...) re-deep-copies the full dict for no reason —
   it's already served its purpose by the time `_period_slice` returns.
   After the category-dtype fix alone, this was **79% of remaining
   runtime** (27.6s of 34.9s). **Fix**: `pit._period_slice` now clears
   `.attrs` on the slice it returns (only `.attrs` on the *derived*
   object — the original `panel.attrs["period_groups"]` is untouched and
   still reused by later calls).

**Combined, validated result**: 336.7s → 10.9s for the same 8 quarters
(**~31x**), peak RSS unchanged (~9.3GB), and a direct
`pd.testing.assert_frame_equal` check confirmed the fast path (category
dtype + period_groups + attrs-cleared) produces byte-identical output to
the original plain-dtype boolean-mask path on real data.

Confirmed with a real end-to-end `python -m src.backtest --quick` run
(single lag/cost combination, short recent window): completed in **27.5s
total** (panel load, price fetch for 458 tickers/1.2M price rows, factor
grid, pickle) — see Verification below.

Both fixes are implemented in `src/pit.py` (`_period_slice`) and
`src/backtest.py` (`prepare_panel()`, called from `main()`), with
dedicated regression tests in `tests/test_pit.py` and
`tests/test_backtest.py` proving equivalence against the original
boolean-mask/plain-dtype path on real-shaped data, not just "tests still
pass."

## Plan (as originally scoped, before the profiling result above)

**Step 1 — Profile before changing anything**
Short `cProfile` pass over 3-4 quarters, one lag value, against the real
full panel. With `period_groups` confirmed already wired in, expect
`sp500_members` (disk I/O + rebuild) to be the new dominant cost. If the
profile instead still points at `pit.py`, stop and re-diagnose (e.g.
check whether `.attrs` is somehow lost in the real run despite the source
trace above) before proceeding.

*(What actually happened: the profile did still point at `pit.py`, but
for a completely different reason than the original "no indexing" theory
— see the section above. Re-diagnosed per this step's own instruction
rather than assumed away.)*

**Step 2 — Add the missing test coverage** (replaces rebuilding the index)
— **done.** `tests/test_pit.py` gained 3 tests: a synthetic panel with
duplicate `PERIODOFREPORT` values and a same-day filing tie exercises
`_period_slice`/`as_of_snapshot` both with and without
`panel.attrs["period_groups"]` set and asserts identical output; a
period-absent-from-groups case; and a direct regression guard that the
returned slice's own `.attrs` comes back empty (the fix for finding #2
above). `tests/test_backtest.py` gained 2 tests for `prepare_panel()`
(category-dtype conversion + `period_groups` construction, and an
equivalence check of `pit.breadth()` output against an unprepared panel).

**Step 3 — Cache `sp500_members`'s repeated I/O and rebuild work** — **done.**
In `src/universe.py`:
- Private helper that reads/parses `sp500_history.csv`, wrapped in
  `functools.lru_cache(maxsize=1)` keyed only on `history_path` (hashable
  `Path`/string). `sp500_members` calls this instead of re-reading the
  file itself.
- Optional pre-built `ticker_to_cusip: pd.Series | None` parameter added
  to `sp500_members`, threaded through `sp500_universe_breadth`,
  `breadth_change`, `breadth_momentum`; built once by `backtest.py`'s
  `main()` and passed down, defaulting to an inline build when omitted so
  existing callers/tests are unaffected.

  Deliberately **not** a bare `lru_cache` on the whole lookup: `mapping`
  is a `pd.DataFrame`, which is unhashable, so caching a function that
  takes it as an argument directly raises `TypeError` at call time.
  Splitting "cache the hashable-keyed file parse" from "thread the
  DataFrame-derived lookup explicitly" avoids that trap entirely.

Files touched: `src/universe.py`, `src/factor.py` (parameter threading
only), `src/backtest.py` (`main()` builds `ticker_to_cusip` once, threaded
through `detect_start_period`/`run_backtest`/`quarter_pnl`).

**Step 4 — `--quick` dev-mode flag** — **done.**
`src/backtest.py`'s CLI gained `--quick`: overrides `lag_days_grid`/
`cost_bps_grid` to `[PRIMARY_LAG_DAYS]`/`[PRIMARY_COST_BPS]`, defaults to
a short recent window (last ~8 quarters) if `--start-period` isn't given
(skips `detect_start_period` entirely in that case), defaults `--out` to
`DEFAULT_QUICK_OUT` (`data/processed/backtest_results_quick.pkl`) unless
`--out` is explicitly given, and always stamps `meta["quick_mode"]`.

## Verification

1. `pytest` — **118/118 pass** (up from 113; 5 new tests, all existing
   tests unmodified).
2. Profiled after the category-dtype + attrs fixes: `sp500_members` is
   confirmed no longer a meaningful share of runtime (~92s out of what
   would be ~4900s+ across the full grid, vs. the now-eliminated
   pyarrow-take/attrs-deepcopy costs that dominated before).
3. `python -m src.backtest --quick --panel data/processed/13f_panel_full.parquet`
   run for real: **27.5s total** (panel load + prep, 458-ticker price
   fetch/1.2M rows, single-combination grid, pickle write), producing 7
   closed quarters + 1 open quarter as expected. Confirms the whole
   pipeline end-to-end, not just the pit/factor sub-step.
4. **Do not auto-chain into the real full run.** Get explicit
   confirmation before starting `python -m src.backtest` (full grid, full
   window) in the background — this exact project has already had one
   background run killed on explicit request and one suspicious injected
   message trying to auto-restart it. The real run only starts on a
   direct, freshly given go-ahead, monitored via a backgrounded process +
   log-tailing monitor watching memory and error signatures.
5. Once results exist: `python -m src.report` for the HTML deliverable,
   then fill in `docs/memo.md`'s Results/Next Steps with real numbers
   (separate follow-on task).
