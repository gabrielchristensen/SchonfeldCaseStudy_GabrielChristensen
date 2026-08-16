# Phase 4 Status — Where This Project Stands

Written just before kicking off the real full-history backtest run, so
that if this session crashes mid-run (it has crashed twice before), the
next session can pick up from a durable record instead of re-deriving
context from scratch. See also `docs/phase4_efficient_implementation.md`
for the detailed writeup of the performance fix referenced below.

## The pipeline, and where Phase 4 sits in it

`ingest → pit → mapping → universe → factor → backtest → report`, one
module per stage. Phases 1-3 (ingest through factor — the ownership
breadth momentum signal) are implemented, tested, and committed
(`git log`: Phase 1 through `bbc90b8`/`88e3d38`/`211e523`). **Phase 4**
(`src/backtest.py`, `src/report.py`) is implemented and tested but **not
yet committed** — everything below is still working-tree changes.

`docs/memo.md` (the actual 3-5 page case-study deliverable) has its
Factor Hypothesis, Scope Decisions, and Methodology sections fully
drafted. **Results and Next Steps are still `TODO`** — they're gated on
the real backtest numbers, which don't exist yet (`data/processed/
backtest_results.pkl` has never been produced by a completed run).

## What's uncommitted right now

```
 M docs/memo.md            226 lines added (Hypothesis/Scope/Methodology draft)
 M requirements.txt        +yfinance==1.6.0
 M src/backtest.py         Phase 4 backtest engine, ~725 lines (new file's worth)
 M src/factor.py           +8/-  ticker_to_cusip threading (perf fix, see below)
 M src/mapping.py          ~71 lines (pre-existing Phase 2 hardening, see git log context)
 M src/pit.py              +65/-  period_groups fast path + attrs-clear fix
 M src/report.py           +243  HTML report generator, new
 M src/universe.py         +72/- sp500_members caching + ticker_to_cusip threading
 M tests/test_mapping.py   +18
 M tests/test_pit.py       +64  period_groups equivalence/regression tests
?? docs/phase4_efficient_implementation.md   new, detailed perf-fix writeup
?? tests/test_backtest.py                    new, backtest.py test suite
?? tests/test_report.py                      new, report.py test suite
```

**118/118 tests pass.** Nothing here is partial/half-implemented — the
backtest engine (lag×cost sensitivity grid, decile long/short portfolios,
SPY + internal-universe benchmarks, transaction costs, rank-IC, turnover)
and the HTML report generator are both complete and covered by tests.

## What was actually blocking the real run, and what's fixed

The real end-to-end run (`python -m src.backtest`, full 3-lag × 4-cost
grid) had crashed twice before this session — no completed run has ever
produced `data/processed/backtest_results.pkl`. This session profiled the
real cause (rather than continuing to guess from reading source) and
fixed it:

1. Pandas 3.0.5's default Arrow-backed string dtype made per-quarter panel
   lookups route through a slow `pyarrow` take kernel. Fixed by converting
   `CIK`/`ACCESSION_NUMBER`/`CUSIP` to `category` dtype once
   (`backtest.prepare_panel()`).
2. Pandas deep-copies a large `.attrs` dict (`period_groups`, the
   point-in-time lookup index) on every downstream pandas operation.
   Fixed by clearing `.attrs` on the already-scoped slice returned from
   `pit._period_slice` once it's served its purpose.
3. (Smaller, also fixed) `universe.sp500_members` was re-reading/re-parsing
   `sp500_history.csv` and rebuilding a ticker→CUSIP lookup from scratch
   on every one of ~300+ calls a full grid run makes — now cached/threaded
   once per run.

**Combined measured effect**: 336.7s → 10.9s for the same 8 real quarters
(~31x), memory flat at ~9.3GB peak (no new OOM risk). Verified end-to-end
with `python -m src.backtest --quick` (single lag/cost combination, short
recent window): completed in 27.5s total, 458 tickers, 1.2M price rows,
7 closed quarters + 1 open quarter — the correct shape.

Full details, including the profiler output and the equivalence checks
proving these are pure performance changes (byte-identical output to the
original slow path on real data), are in
`docs/phase4_efficient_implementation.md`.

## What's next, in order

1. ~~Run the real full-history backtest~~ — **done.** Completed
   successfully on the first try after the perf fixes: window
   2015-09-30 to 2026-03-31, 515 tickers, all 12 lag×cost combinations,
   only 4 tickers dropped for missing price data across the whole run.
   `data/processed/backtest_results.pkl` exists.
2. ~~Generate the HTML deliverable~~ — **done.**
   `results/backtest_report.html` (380KB, self-contained).
3. ~~Fill in `docs/memo.md`'s Results/Next Steps~~ — **done**, with real
   numbers. Headline finding, stated honestly in the memo: the long-short
   spread (60d/10bps primary) is weak and underperforms both benchmarks
   on every risk-adjusted metric (Sharpe 0.15 vs. SPY's 0.84, max
   drawdown -55.4% vs. SPY's -33.7%); rank-IC is weakly positive but not
   statistically significant at any lag; lag sensitivity is non-monotonic
   (inconclusive on the front-loaded-decay mechanism); cost sensitivity
   is monotonic and severe (~80%/quarter turnover eats most of the edge
   by 25bps). Next Steps in the memo: reduce turnover (highest-priority,
   least ambiguous finding), test a scale-neutralized signal variant to
   isolate the mega-cap-tilt confound from genuine signal, re-run the lag
   sweep at finer granularity before concluding anything about decay.
4. **Still open**: review and commit the large uncommitted diff (ask the
   user first, given the size — nothing has been committed yet this
   phase, across factor.py/pit.py/universe.py/backtest.py/report.py/
   memo.md/requirements.txt and 4 test files).

## If resuming after a crash

Read this file and `docs/phase4_efficient_implementation.md` first. Check
`data/processed/backtest_results.pkl` — if it exists and is newer than
the last known crash point, the full run may have already completed;
verify with `pickle.load` before re-running (re-running is expensive:
real network calls to Yahoo Finance for hundreds of tickers plus the full
compute grid). `data/processed/prices.parquet` is a resumable cache
(`fetch_prices` skips tickers already fetched), so even a re-run doesn't
start from zero.
