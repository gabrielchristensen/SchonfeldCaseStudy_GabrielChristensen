# Phase 3 Implementation Report — Ownership Breadth Momentum Factor

## Scope of Phase 3

Per the pipeline design (`ingest → pit → mapping → universe → factor → backtest → report`), Phase 3 covers **`src/factor.py`**: turning Phase 2's point-in-time, universe-restricted breadth primitive (`universe.sp500_universe_breadth()`) into an actual tradeable signal. The factor hypothesis itself — ownership breadth momentum, grounded in Chen–Hong–Kubik (2002) — was decided in the original master-planning session, before Phase 1 began. What Phase 3 had to resolve was everything the original outline explicitly flagged as still open: the exact standardization method, how to handle stocks with no prior-period baseline, and whether a minimum-history requirement was needed.

Status: **complete**. 81/81 tests pass (up from 69 at the start of this phase), end-to-end verified against real adjacent-quarter data (not synthetic fixtures), and followed by a dedicated post-implementation review for correctness bugs and bias — the same discipline applied to Phases 1 and 2.

---

## Design decisions, confirmed via discussion before implementation

Three genuinely open questions were resolved with the user before any code was written:

1. **Standardization: both percentile rank and z-score, rank as primary.** Rank is bounded and robust to outliers by construction — a single index-addition event with an artificial breadth jump can't distort the whole cross-section — and rank order is directly the decile-sort key Phase 4 needs. Z-score is kept as a secondary robustness-check column, explicitly **not winsorized**, and documented as more outlier-sensitive than rank rather than silently assumed safe.
2. **No prior-period breadth (most commonly: newly added to the S&P 500): drop, don't impute.** Treating a missing baseline as 0 would score every index addition as a maximal positive outlier — that's index-reconstitution noise, not genuine ownership momentum, and would contaminate the top decile with a confound unrelated to the factor hypothesis.
3. **No minimum-history requirement** beyond having both the current and prior quarter present — matches the literal QoQ-momentum definition; requiring more history is a separate robustness question for a later phase, not a correctness requirement now.

---

## Point-in-time design: both periods evaluated as of the same `as_of_date`

This is the central design decision in `breadth_change()`, and the one most directly relevant to the case study's point-in-time evaluation criterion.

`breadth_momentum(panel, period_of_report, as_of_date, mapping, ...)` computes `breadth(period_of_report)` **and** `breadth(prior_quarter)`, both evaluated as of the *same* `as_of_date` — not a frozen snapshot of the prior quarter as of its own, earlier formation date. This is deliberate, not an oversight: by the current formation date, everything about the prior quarter is already fully public regardless of how complete it was back at its own formation date, so using the freshest available data for both terms of the difference is the correct point-in-time choice, not lookahead. It would only be lookahead if `as_of_date` predated when that information became public, which it never does here — `as_of_date` is always the caller's current decision date for both terms.

Mechanically this required **zero new point-in-time logic**: `breadth_change()` composes `universe.sp500_universe_breadth()` (already point-in-time correct from Phase 2) unchanged, called twice with the same `as_of_date` and two different `period_of_report` values. Both calls also restrict to `sp500_members(as_of_date)` — the same universe set both times, since that function only depends on `as_of_date` — which is exactly the right behavior: a name that has since dropped out of the S&P 500 as of the current formation date shouldn't be in the active signal at all today, regardless of its historical 13F breadth.

**This design decision is directly proven by a dedicated test**, not just asserted in a docstring: `test_breadth_change_uses_same_as_of_date_for_both_periods_not_frozen_snapshot` constructs a prior-quarter filing that arrives late (well after that quarter's own ~45-day deadline) and checks two things — with `as_of_date` set *after* the late filing, `breadth_prior` correctly includes it; with `as_of_date` set *before* it, `breadth_prior` correctly excludes it. This is the closest thing in the test suite to a direct lookahead-bias regression guard for Phase 3's own logic.

---

## Module design — three small composable functions

Mirrors the pattern established in `universe.py` (small, independently-testable pieces composing into one contract function):

```python
def prior_quarter_end(period_of_report) -> pd.Timestamp:
    """Uses pandas Period arithmetic ((pd.Period(x, freq="Q") - 1).end_time),
    not a fixed 3-month offset, to be robust regardless of date semantics."""

def breadth_change(panel, period_of_report, as_of_date, mapping, *,
                    passive_ciks=None, history_path=universe.DEFAULT_HISTORY_PATH) -> pd.DataFrame:
    """Un-standardized: [CUSIP, breadth, breadth_prior, raw_change].
    Calls sp500_universe_breadth() twice, inner-joins on CUSIP -- the join
    itself implements "drop, don't impute" for free, no special-case code."""

def standardize_cross_section(df, *, column="raw_change") -> pd.DataFrame:
    """Adds rank_signal (primary) and zscore_signal (secondary, not
    winsorized). Guards std == 0 or NaN by returning NaN, not raising
    or producing inf."""

def breadth_momentum(panel, period_of_report, as_of_date, mapping, *,
                      passive_ciks=None, history_path=universe.DEFAULT_HISTORY_PATH) -> pd.DataFrame:
    """Composes breadth_change() + standardize_cross_section() -- the full
    per-formation-date contract Phase 4 consumes."""
```

No CLI, unlike `ingest.py`/`mapping.py`/`universe.py` — `factor.py` has no offline/network build step; it's pure computation over already-built artifacts, called as a library function by `backtest.py` (Phase 4), which owns looping over formation dates across the backtest window.

**Accepted minor inefficiency, disclosed not hidden**: `breadth_momentum` calls `sp500_universe_breadth` twice, so `sp500_members()` (a small CSV read + set comprehension) runs twice per formation date instead of once. Cheap enough in absolute terms — not a real bottleneck even across a full ~52-quarter backtest loop — that avoiding it isn't worth threading a precomputed universe through the call chain.

---

## Real methodology issue caught during end-to-end verification (not a code bug)

The first end-to-end check, run against the 3-dataset validation sample panel (`period_of_report='2026-03-31'`, `as_of_date='2026-05-29'`, prior quarter `2025-12-31`), produced a suspicious result: `breadth_prior` around 150–200 for names whose current `breadth` was 5,000–6,000+ — an implausible ~30x jump across every single name in the universe, not just a few outliers.

Root cause, traced rather than assumed: the sample panel's three datasets (`2013q2`, `2015q1`, `01mar2026-31may2026`) don't include `01dec2025-28feb2026_form13f.zip` — the dataset covering the **on-time filing window** for period `2025-12-31`. The sample panel only had late-arriving stragglers for the prior quarter, not real coverage, because it was deliberately built in Phase 1 to prove the ingestion pipeline works across both SEC naming eras with a minimal 3-file download, not to support quarter-over-quarter comparisons.

**Confirmed and resolved** by building a proper two-adjacent-quarter panel from the already-cached `01dec2025-28feb2026_form13f.zip` (no download needed — it was sitting in `data/raw/13f/` from Phase 1's earlier `--full` run) merged with the existing `01mar2026-31may2026` dataset. Re-running against that panel gave sane results:

- **429 rows** — exactly matching Phase 2's universe size for that date, zero dropped for missing prior-period breadth (expected: the S&P 500 is stable quarter to quarter, so essentially every current member also has prior-quarter data).
- `raw_change` ranging **-356 to +431** (mean -4, std ~97) — sane magnitudes relative to breadth counts in the thousands.
- Zero `NaN`s anywhere; `rank_signal` spanning `[0.0023, 1.0]` as expected for 429 names.

**Finding carried forward to Phase 4**: any real backtest needs a panel with proper adjacent-quarter coverage. The existing full historical panel (`data/processed/13f_panel_full.parquet`, 806MB, all ~55 datasets) has this property already — but it predates the Phase 2 COVERPAGE addendum and CIK `zfill` fix, so it likely still has the CIK zero-padding inconsistency that would silently weaken `passive_manager_ciks()` exclusion. **Rebuilding the full panel with current `ingest.py` should be a Phase 4 prerequisite**, not assumed still valid from Phase 1.

---

## Post-implementation review: errors and bias

Following the same discipline applied after Phase 2, a dedicated review pass checked `factor.py` line by line for correctness bugs, then re-examined the design for lookahead, survivorship, and other bias risks.

### No correctness bugs found

Verified this isn't just "tests pass" but that the tests would actually **catch** realistic bugs, by mentally mutating the implementation against each assertion:

- A sign flip in `raw_change` (`breadth_prior - breadth` instead of `breadth - breadth_prior`) would fail `test_breadth_change_computes_correct_raw_change`.
- Switching the inner join to a left join (silently imputing instead of dropping no-prior-breadth names) would fail `test_breadth_change_drops_cusip_with_no_prior_period_breadth`.
- Freezing the prior period's `as_of_date` instead of reusing the current one would fail the late-filing half of `test_breadth_change_uses_same_as_of_date_for_both_periods_not_frozen_snapshot`.
- Flipping `rank()`'s sort direction would fail `test_breadth_momentum_composes_change_and_standardization`'s ordering assertion.

Also traced the merge/groupby chain for the exact bug classes found in Phase 2's `mapping.py` (duplicate-row fan-out, dtype corruption on concat/merge): `pit.breadth()`'s `groupby("CUSIP")` output is guaranteed CUSIP-unique going into `breadth_change()`'s merge, so there's no fan-out risk, and `breadth`/`breadth_prior` are always non-null `int64` (guaranteed by the inner join), so `raw_change` can never silently become `NaN` or drift to an unexpected dtype.

### One real, disclosed-not-fixed methodological property

Checked empirically whether using **raw** (unscaled) breadth change mechanically favors mega-caps in the extreme deciles, given baseline breadth varies enormously across the S&P 500 (hundreds to 6,000+ filers per name). On the real two-adjacent-quarter panel:

- **Correlation of 0.33–0.37** between `breadth_prior` (a size proxy) and both `|raw_change|` and rank-extremity.
- Decile breakdown: decile 0 (biggest breadth *decrease* — the short leg of a long-short portfolio) has median `breadth_prior` of **2,392**, more than double the overall median of **1,136**.

This is essentially guaranteed by counting-statistics variance scaling with baseline level (a count with a larger baseline mechanically has more absolute room to swing) — not an implementation error. It was **not fixed**: the raw-change formula was an explicit decision from the original factor-design session, not something to unilaterally reverse now, and a percentage-based alternative has its own worse problem (a low-baseline name going from 5 to 10 holders would read as a "100% increase," almost certainly noise rather than signal). Documented in `factor.py`'s module docstring and flagged here for the memo's honest "noise vs. signal" assessment — this means the short leg in particular is likely tilted toward large-cap names within the S&P 500, not a clean signal across the full cap spectrum, and that should be stated plainly rather than discovered by a reviewer.

---

## Test coverage

12 tests (new file `tests/test_factor.py`), bringing the repo total to 81:

| Area | Tests | What's verified |
|---|---|---|
| `prior_quarter_end` | 4 (parametrized) | All four quarter-boundary transitions (Mar→Dec-prior-year, Jun→Mar, Sep→Jun, Dec→Sep) |
| `breadth_change` | 3 | Correct `raw_change` for a name present both periods; a name with no prior-period breadth is dropped, not imputed; the point-in-time "same `as_of_date` for both terms" design, proven both directions (late filing included when `as_of_date` is after it, excluded when before) |
| `standardize_cross_section` | 4 | `rank_signal` against a known ordering; `zscore_signal` against hand-computed values; a constant-value (`std == 0`) cross-section returns `NaN`, not `inf`; a single-row cross-section returns `NaN` for z-score without raising |
| `breadth_momentum` | 1 | Integration test tying the full composition together, including the correct sign/direction of `rank_signal` relative to `raw_change` |

One fixture-construction mistake surfaced while writing these tests — the same recurring pattern from earlier phases: giving one CIK two different accession numbers for the same reporting period, which `pit._winning_accessions` (correctly) reads as an amendment pair, dropping one of them. Not a `factor.py` or `pit.py` bug; fixed by consolidating each filer's holdings under one accession per period, matching how a real 13F filing actually works (every CUSIP a manager holds reported under a single accession number).

---

## What Phase 3 deliberately does *not* do

- **No minimum-history requirement beyond one prior quarter** — a stock only needs `t` and `t-1` present; deeper history requirements (e.g., 4+ consecutive quarters) are a robustness question deferred to a later phase, not implemented here, per the explicit design decision.
- **No winsorization of `zscore_signal`** — disclosed as more outlier-sensitive than `rank_signal` rather than patched, since `rank_signal` is the primary signal Phase 4 will actually use for sorts.
- **No scale-neutralization of the raw breadth-change magnitude** — the mega-cap tilt documented above is left as-is, not corrected with a percentage-based or size-neutralized variant, matching the earlier factor-design decision.
- **No formation-date scheduling or loop** — `factor.py` computes the signal for exactly one caller-supplied `(period_of_report, as_of_date)` pair; iterating over the full backtest window's formation dates is explicitly Phase 4's responsibility (`backtest.py`).

## Recommended next step

Phase 4 (`src/backtest.py`): formation-date scheduling (quarter-end + ~65-75 day lag buffer), quarterly rebalancing into long-short decile portfolios sorted on `rank_signal`, price/return data via `yfinance` (primary) with `stooq` as a fallback, transaction-cost assumptions, and performance statistics. Before any of that: rebuild the full historical panel with current `ingest.py` (flagged above) so the real backtest runs on properly zfilled CIKs and full adjacent-quarter coverage, not the 3-dataset validation sample or the pre-Phase-2 full panel.
