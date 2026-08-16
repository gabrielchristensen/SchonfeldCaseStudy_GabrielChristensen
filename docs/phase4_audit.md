# Phase 4 Pre-Commit Audit — Backtest Correctness and Bias Review

Requested before committing the Phase 4 diff: verify the completed
backtest run is actually correct, and re-review the whole pipeline
structure (`ingest → pit → mapping → universe → factor → backtest →
report`) for bias or data-modeling errors, not just "did it run without
crashing." This document is that audit's findings.

**Bottom line**: the backtest ran correctly. No bugs found in the
computation logic (decile construction, transaction costs, turnover,
benchmarks, rank-IC, closed/open gating). One real, previously-flagged
data-quality gap was found and precisely scoped — small, doesn't change
the memo's conclusions, documented below with a recommendation, not
silently fixed. One real gap in this session's own test coverage was
found and closed. 119/119 tests pass.

---

## 1. Backtest computation logic — verified correct

Read and independently re-derived (not just re-read) the core formulas
against `data/processed/backtest_results.pkl`'s actual output:

- **Decile direction**: `rank_signal` is a percentile rank of
  `raw_change` (breadth increase); `assign_deciles` puts decile 9 (top)
  = highest `rank_signal`. `quarter_pnl` longs `decile == top` and shorts
  `decile == 0`. This is the correct direction for the stated hypothesis
  (rising breadth → long, falling breadth → short) — confirmed by
  re-reading the code, not assumed from the docstring.
- **Transaction cost application**: `apply_transaction_costs` multiplies
  an entire quarter's NAV curve (which always starts at exactly 1.0 by
  construction) by a constant `(1 - cost)`. Verified this is
  mathematically equivalent to "pay the cost once at rebalance, let it
  compound with the same daily returns thereafter" — not a bug, a
  disclosed simplification. Cross-checked the actual numbers: at 60-day
  lag, mean turnover (0.766 long + 0.817 short) × 10bps × 2 predicts
  ~1.27pp/year of cost drag; observed annualized return dropped from
  3.92% (0bps) to 2.65% (10bps), a 1.27pp drop. Matches exactly.
- **Closed vs. open quarter gating**: `is_open = next_as_of_date >
  as_of_today`. Only `closed` quarters feed `_stitch()` into the headline
  `spread_nav`/`universe_nav`/`ic_series` — verified directly in
  `run_backtest`'s source, not just trusted from the docstring. Explains
  a real pattern in the output that initially looked odd: 45-day lag
  shows 43 closed/0 open quarters, while 60- and 90-day show 42 closed/1
  open — traced to the last quarter's exit date landing on 2026-08-14 for
  the 45-day lag (one day before this run's "today") vs. 2026-08-29/09-28
  for 60/90-day (still in the future). Not a bug — a correct, if
  coincidentally close, boundary evaluation.
- **Rank-IC** (`_rank_ic`): computed over the full scored universe (not
  just the extreme deciles, confirmed by checking what's passed in before
  `assign_deciles` narrows anything), using only price data inside
  `[as_of_date, next_as_of_date]` — no lookahead into data beyond the
  holding period.
- **Benchmarks**: SPY and the internal equal-weight universe both
  correctly built only from `closed` quarters; costs are (disclosedly)
  applied only to the long-short legs, not the benchmarks — a fair
  asymmetry given SPY is a single buy-and-hold position, not a
  quarterly-rebalanced 40-name book.

**Real-data sanity checks**: AAPL/MSFT/SPY prices in
`data/processed/prices.parquet` fall in plausible historical ranges
($20.55-$339.79, $38.32-$538.66, $154.16-$777.88 respectively) — this is
genuine Yahoo Finance data, not a stub. The 4 tickers dropped for missing
price data across the entire run (`SNI`, `CELG`, `TWTR`, `IPG`) are real
acquisitions/delistings (Scripps Networks, Celgene, Twitter) consistent
with the disclosed "current-ticker-only" CUSIP mapping limitation — the
mechanism is working as documented, not silently misbehaving.

## 2. Bias review

- **Lookahead**: re-confirmed the `pit.py` no-lookahead guarantee holds
  through this session's own changes (see §3) via explicit equivalence
  tests against the original boolean-mask path, not just "it still
  passes the existing suite."
- **Survivorship**: S&P 500 universe is point-in-time via
  `sp500_members(as_of_date, ...)`, keyed on the trade date, not today's
  membership list — unchanged, re-confirmed by reading the call site.
- **Passive-manager exclusion**: confirmed still functioning correctly
  despite a data-quality issue found in §4 below (verified the two don't
  intersect).
- **Mega-cap tilt** (Phase 3's disclosed finding): the actual backtest
  results are consistent with this being a real driver of the weak
  long-short spread — see the memo's Results section for the full
  argument. Not re-litigated here since it was already disclosed and the
  new numbers are consistent with it, not contradicting it.
- **Cost/benchmark asymmetry**: disclosed in `report.py`'s own output,
  reconfirmed appropriate (see above).

## 3. This session's own changes — re-verified independently

Beyond the unit tests already added (see prior session summary), ran two
additional targeted checks before trusting this code for the real run:

- **`ticker_to_cusip` threading** (new this session, across
  `universe.py`/`factor.py`/`backtest.py`): had test coverage for
  `prepare_panel` and the `pit.py` fast path, but **not** for this
  threading specifically. Verified directly against real data
  (`factor.breadth_momentum` called with an explicit pre-built
  `ticker_to_cusip` vs. left to build inline) — byte-identical output,
  392 rows. **Added a permanent regression test**
  (`test_breadth_momentum_explicit_ticker_to_cusip_matches_inline_build`
  in `tests/test_factor.py`) so this doesn't silently regress later —
  this was a real gap, now closed.
- **`_load_sp500_history`'s `lru_cache(maxsize=1)`**: checked whether any
  test reuses a fixed (non-`tmp_path`) `history_path` across multiple
  test functions, which would risk serving stale cached content between
  tests. Confirmed every call site uses pytest's `tmp_path` fixture (a
  unique directory per test), so no cross-test staleness risk. Also
  re-confirmed `sp500_members` never mutates the cached, shared
  DataFrame in place.

## 4. New finding: stale panel CIK zero-padding (real, but low materiality)

**Confirmed real**: `data/processed/13f_panel_full.parquet` (the panel
this entire backtest ran against) predates `ingest.py`'s CIK `zfill(10)`
fix — exactly the risk Phase 3's report flagged and recommended
rebuilding the panel to close *before* Phase 4, which was never actually
done. This audit found and precisely scoped the actual impact, which
turns out to be smaller than the Phase 3 report worried about:

- **179,872 of 81,812,809 rows (~0.22%)** have a non-10-digit `CIK`
  string (26 distinct malformed values).
- **Does not affect passive-manager exclusion**: verified none of the 26
  malformed CIKs, zero-padded, match any of the 14 curated passive-manager
  CIKs in `data/reference/passive_manager_ciks.csv`. The concern Phase 3
  specifically raised ("would silently weaken `passive_manager_ciks()`
  exclusion") does not materialize in this panel.
- **Does mildly inflate breadth counts**: 21 of the 26 malformed CIKs
  have their zero-padded form *also* appearing as a separate, correctly-
  padded set of rows elsewhere in the panel — i.e., 21 real institutional
  filers are each being counted as two distinct CIKs in some quarters,
  which would count as +1 extra to `breadth` (distinct-filer-count) for
  whichever CUSIPs that filer holds, in whichever of the 51 quarters (out
  of the ~42-43 used) this shows up. Given breadth values in this
  universe run into the hundreds to thousands per name (median ~1,100
  per Phase 3's own numbers), an occasional +1 for 21 filers is a small
  perturbation, not a first-order driver of the backtest's already-weak
  result.

**Recommendation, not applied here**: rebuild the full panel with the
current `ingest.py` (`python -m src.ingest --full`, several GB / real
time cost) and re-run the backtest to close this gap cleanly, since it's
now a known, quantifiable data-quality issue rather than a hypothetical
risk. Given the measured impact is small and diffuse (not concentrated
in the passive-exclusion mechanism, which was the actual concern), this
is a **disclosed follow-up, not a blocker** for committing the current
diff — the memo's conclusions are very unlikely to change materially
from this specific fix. Flagging it here so it's a recorded, prioritized
decision rather than a silently-carried-forward risk.

## 5. Test suite status

**119/119 tests pass** (118 from before this audit + 1 new
`ticker_to_cusip` threading regression test added in §3). No test was
weakened or removed to make anything pass.

## 6. Recommendation

Safe to commit as-is. Two items worth the user's explicit decision,
neither blocking:

1. Whether to rebuild the panel and re-run to close the CIK zero-padding
   gap (§4) before or after committing — low expected impact on results,
   but real cost (several GB re-download, real time) to do it.
2. Whether to fold this audit's findings into `docs/memo.md` (e.g., a
   short caveat in the Results section about the CIK zero-padding
   footnote) or keep it here as a standalone audit record — this document
   is written to stand alone either way.
