# Full Project Documentation — 13F Positioning Factor & Backtester

This is the comprehensive technical reference for this project: how the
system is built, how to run it, the exact math behind every number it
produces, and the full build history phase by phase. `docs/memo.md`
remains the polished, page-limited case-study deliverable; this document
is the deeper reference behind it — written for a technical defense
where any part of the system may need to be explained or extended. The
six phase-specific reports (`docs/phase1_report.md` through
`docs/phase4_audit.md`) remain the most detailed record of each phase's
individual bugs, tests, and validation runs; this document summarizes
and cross-references them rather than duplicating them, except where a
formula or design decision needs to be stated in full to be usable as a
reference on its own.

## Table of Contents

1. [System Overview](#1-system-overview)
   - [1.1 Repository Structure](#11-repository-structure)
   - [1.2 Pipeline Workflow](#12-pipeline-workflow)
   - [1.3 Methodology & Math](#13-methodology--math)
2. [Chronological Narrative](#2-chronological-narrative)
   - [Phase 1 — Ingestion & Point-in-Time Panel](#phase-1--ingestion--point-in-time-panel)
   - [Phase 2 — CUSIP Mapping & Universe Definition](#phase-2--cusip-mapping--universe-definition)
   - [Phase 3 — Ownership Breadth Momentum Factor](#phase-3--ownership-breadth-momentum-factor)
   - [Phase 4 — Backtest, Report, and Analysis](#phase-4--backtest-report-and-analysis)
   - [Cross-Platform Reproducibility Hardening](#cross-platform-reproducibility-hardening)
3. [Defense Quick-Reference Index](#3-defense-quick-reference-index)

---

## 1. System Overview

### 1.1 Repository Structure

```
docs/
  prompt.pdf                          # the case study prompt itself
  memo.md                             # 3-5 page deliverable memo
  full_project_documentation.md       # this file
  phase1_report.md ... phase4_audit.md  # per-phase detailed records
data/
  raw/         # untouched SEC downloads (gitignored, local-only)
  processed/   # derived/cleaned data (gitignored, local-only) --
               #   the built panel, price cache, backtest results pickle,
               #   per-asset/sub-period CSVs
  reference/   # small, committed reference tables:
               #   cusip_ticker_map.csv, cusip_overrides.csv,
               #   sp500_history.csv, passive_manager_ciks.csv,
               #   SOURCES.md (provenance for every one of the above)
notebooks/     # exploratory analysis (regime_and_attribution.ipynb)
src/           # reusable pipeline code -- see 1.2 below
tests/         # one test file per src/ module
results/       # the committed, self-contained HTML backtest report
```

`data/raw/` and `data/processed/` are gitignored by design (per
`CLAUDE.md`): they're either a straight copy of SEC's public data or
fully derived from committed inputs plus code, so committing them would
just be redundant, large, binary weight in the repo. Everything needed
to regenerate them is either committed (`data/reference/`) or
reproducible by running the pipeline (see 1.2).

### 1.2 Pipeline Workflow

The pipeline is eight modules under `src/`, each with a narrow
responsibility, composing in one direction:

```
ingest → pit → mapping → universe → factor → backtest → report
                                              ↘ detail
```

| Module | Responsibility | CLI |
|---|---|---|
| `ingest.py` | Download SEC's structured 13F datasets, parse `SUBMISSION`/`INFOTABLE`/`COVERPAGE`, dedup, build the raw long panel. | `python -m src.ingest` (3-dataset validation sample) / `--full` (entire archive, ~55 zips) |
| `pit.py` | Turn the raw panel into point-in-time-correct snapshots: "what was known as of date X." | none (pure library) |
| `mapping.py` | CUSIP → ticker crosswalk via OpenFIGI, plus a small curated overrides table. | `python -m src.mapping --build` |
| `universe.py` | Point-in-time S&P 500 membership; passive-manager exclusion list; composes with `pit.breadth()`. | `python -m src.universe --build-sp500-history` |
| `factor.py` | The ownership-breadth-momentum signal itself. | none (pure library) |
| `backtest.py` | Formation-date scheduling, decile long/short portfolios, transaction costs, benchmarks, performance stats. | `python -m src.backtest [--quick]` |
| `report.py` | Renders the backtest results pickle into the self-contained HTML deliverable. | `python -m src.report` |
| `detail.py` | Derives per-asset holdings/returns and sub-period regime stats from an already-run backtest — no changes to `backtest.py`. | `python -m src.detail` |

**To reproduce the full pipeline from a clean clone:**

```bash
./setup.sh && source .venv/bin/activate
pytest                                    # 129 tests, fully offline, no data needed

# See real 13F data flow through the pipeline (fast — 3 real datasets):
python -m src.ingest

# Full historical panel (slow — ~55 zips, several GB, checkpointed so a
# crash mid-run doesn't force re-parsing everything already done):
python -m src.ingest --full

# data/reference/* (CUSIP crosswalk, S&P 500 history, passive-manager
# list) are already committed -- rebuilding them from scratch
# (--build flags below) is optional, only needed to reproduce them from
# zero rather than use the committed versions:
python -m src.mapping --build
python -m src.universe --build-sp500-history

# The real backtest (full lag x cost grid, or --quick for one
# combination over a short recent window):
python -m src.backtest --panel data/processed/13f_panel_full.parquet \
  --out data/processed/backtest_results.pkl

# The HTML deliverable:
python -m src.report --results data/processed/backtest_results.pkl

# Per-asset detail + sub-period regime stats (derived, no re-run):
python -m src.detail --results data/processed/backtest_results.pkl \
  --prices data/processed/prices.parquet
```

Every stage that touches the network or does real work (`ingest`,
`mapping --build`, `universe --build-sp500-history`, `backtest`'s price
fetch) checkpoints to `data/processed/` as it goes, so an interrupted run
resumes instead of restarting from zero — this pattern was established
in Phase 1 (`ingest.py`'s `checkpoint_dir`) and reused consistently in
every later phase (`mapping.py`'s `checkpoint_path`, `backtest.py`'s
`fetch_prices` ticker-keyed cache).

### 1.3 Methodology & Math

This section states every formula the pipeline actually computes, in
the order data flows through it.

#### Point-in-time snapshot (`pit.as_of_snapshot`)

For a given `(period_of_report, as_of_date)`:

1. Restrict to rows with that `PERIODOFREPORT`.
2. For each `CIK`, find the `ACCESSION_NUMBER` of its most recent
   submission (by `FILING_DATE`, ties broken by `ACCESSION_NUMBER`,
   since EDGAR assigns these monotonically) with `FILING_DATE <=
   as_of_date`.
3. Return only the rows belonging to those winning accession numbers.

A CIK with no accepted filing yet by `as_of_date` is simply absent —
**never backfilled** once a later filing arrives. This single rule is
the entire no-lookahead guarantee the rest of the pipeline inherits for
free; nothing downstream re-implements point-in-time logic.

At `--full` scale (~82M rows), a naive `panel["PERIODOFREPORT"] == X`
boolean-mask scan is O(n) per call and, combined with pandas 3's
Arrow-backed string dtype, was profiled as the dominant real-run cost
(see Phase 4 below). `backtest.prepare_panel` builds `panel.attrs
["period_groups"]` once (`groupby("PERIODOFREPORT").indices`) and
`_period_slice` uses it for an O(k) positional lookup instead — a pure
performance path, proven identical to the boolean-mask result via
dedicated equivalence tests, not a second set of semantics.

#### Breadth (`pit.breadth`, `universe.sp500_universe_breadth`)

```
breadth(CUSIP, period, as_of_date) = count of distinct CIKs
    holding that CUSIP in as_of_snapshot(period, as_of_date),
    optionally excluding a passive-manager CIK set
```

`sp500_universe_breadth` restricts the result to `CUSIP`s that resolve
(via the ticker crosswalk) to a ticker in `sp500_members(as_of_date)` —
the point-in-time S&P 500 membership list, keyed on the same
`as_of_date` axis as `breadth()` itself (not `period_of_report`) — the
correct choice for "was this name actually investable at the moment
we'd act on the signal."

#### Factor construction (`factor.breadth_change`, `standardize_cross_section`)

```
raw_change(CUSIP, period, as_of_date) =
    breadth(CUSIP, period, as_of_date) - breadth(CUSIP, prior_quarter_end(period), as_of_date)
```

Both terms are evaluated **at the same `as_of_date`** — not a frozen
snapshot of the prior quarter as of its own, earlier formation date.
This is deliberate: by the current formation date, everything about the
prior quarter is already fully public regardless of how complete it
looked back at its own formation date, so using the freshest available
data for both terms is the correct point-in-time choice, not lookahead
(it would only be lookahead if `as_of_date` predated when the
information became public, which it never does here).

A `CUSIP` with no recorded prior-quarter breadth (almost always: newly
added to the S&P 500) is **dropped via an inner join, not imputed** — an
"infinite increase" from a missing baseline would encode "was added to
the index," not genuine ownership momentum.

Standardization, both computed, rank as the primary signal:

```
rank_signal   = raw_change.rank(pct=True)                    # in [0, 1]
zscore_signal = (raw_change - mean(raw_change)) / std(raw_change)   # NOT winsorized
```

**Known, disclosed property, not a bug**: raw (unscaled) `raw_change`
mechanically favors names with larger baseline breadth — counting-
statistics variance scales with the baseline level. Measured on real
data: correlation ~0.33-0.37 between `breadth_prior` and both
`|raw_change|` and rank-extremity. Left as-is (a percentage-based
alternative has its own worse problem: a 5-to-10-holder move reads as a
"100% increase," almost certainly noise) — see the memo's Results
section and the Phase 4 regime/attribution notebook for how this shows
up in practice (concentration in large-cap names, especially the short
leg).

#### Backtest mechanics (`backtest.py`)

**Formation schedule**: `formation_dates(start, end, lag_days)` yields
`(period_end, period_end + lag_days)` for every quarter-end in range.
`lag_days` is swept, not fixed — 45/60/90 calendar days, run as three
independent full backtests — because 13F filings are due 45 days after
quarter-end but only 87.0% arrive by then (96.3% by day 60, 96.8% by day
90, measured on the real panel); a longer lag buys coverage at the cost
of trading further from the underlying ownership shift.

**Portfolio construction**: `assign_deciles` sorts the cross-section
into 10 equal-count bins on `rank_signal` (`pd.qcut`, `duplicates=
"drop"` guards a degenerate cross-section). Long = top decile, short =
bottom decile, both equal-weighted. No intra-quarter trading — buy at
formation close, hold to next formation.

**Leg NAV** (`leg_nav`): for a basket of tickers over
`[start_date, end_date]`,

```
norm_i(t) = price_i(t) / price_i(start)      # per ticker
nav(t)    = mean_i(norm_i(t))                 # equal-weight across usable tickers
```

A ticker missing entirely, or missing at the window's first available
row, is dropped — the basket reweights over the remaining names. This
construction gives an **exact identity**, used directly as a
reconciliation check in `detail.py`: since the leg is equal-weight and
never rebalanced intra-quarter, `nav(end)/nav(start) - 1` equals the
plain arithmetic mean of the individual tickers' total returns over the
same window. Not an approximation — proven by substitution:
`nav(end)/nav(start) = mean_i(price_i(end)/price_i(start)) =
mean_i(1 + return_i) = 1 + mean_i(return_i)`.

**Turnover** (`leg_turnover`): one-way turnover between two consecutive
rebalances, `|names added| / |new leg size|`. An empty prior leg (the
first quarter) is 1.0 (full turnover) by construction, not a
special-cased 0.5 — building a position from scratch really is 100%
one-way turnover.

**Transaction costs** (`apply_transaction_costs`):

```
cost = (turnover_long + turnover_short) * (cost_bps / 10000) * 2
spread_nav_adjusted = spread_nav * (1 - cost)
```

Applied once, as a constant multiplicative haircut across the whole
quarter's NAV curve — since `spread_nav[0] = 1.0` by construction, this
is mathematically equivalent to "pay the cost once at rebalance, then
compound normally," not an approximation. Swept across 0/5/10/25bps
one-way; turnover itself doesn't depend on the cost assumption, so it's
computed once per lag and reused across the whole cost grid.

**The spread NAV is a daily-rebalanced combination, not a simple
difference of two total returns** — this is the single most
easy-to-get-wrong piece of the whole system (see Phase 4 below for how
it was actually gotten wrong once, and caught):

```
long_ret(t)   = long_nav.pct_change(t)     # daily
short_ret(t)  = short_nav.pct_change(t)    # daily
spread_ret(t) = long_ret(t) - short_ret(t)
spread_nav    = cumprod(1 + spread_ret)
```

Because this combines *daily* returns before compounding, the total
spread return over a multi-day quarter is a **path-dependent** function
of the two legs' daily paths — it does **not** reduce to `long_total_
return - short_total_return` except over a single time-step. The exact
identity above (leg NAV return = mean of per-ticker returns) holds
*per leg*; it does not extend to the spread.

**Stitching across quarters** (`_stitch`): chains a list of quarter-
local NAV curves (each starting at 1.0) into one continuous curve,
carrying the running level forward multiplicatively. Consecutive
quarters share their boundary date (quarter *i*'s exit = quarter *i+1*'s
entry); the duplicate is resolved by keeping the **post-rebalance**
(cost-adjusted) value, the economically correct one.

**Closed vs. open quarters**: `is_open = next_as_of_date > as_of_today`.
Only closed quarters (fully realized holding periods) feed headline
`spread_nav`/`universe_nav`/`ic_series` and their stats — a trailing,
still-forming quarter is tracked separately and never blended in.

**Performance stats** (`performance_stats`, `rf = 0`, no risk-free
series sourced):

```
total_return       = nav[-1] / nav[0] - 1
n_years             = (number of daily returns) / 252
annualized_return   = (1 + total_return) ** (1 / n_years) - 1
annualized_vol      = std(daily_returns, ddof=1) * sqrt(252)
sharpe              = annualized_return / annualized_vol
max_drawdown        = min(nav / running_max(nav) - 1)
hit_rate            = fraction of daily returns > 0
```

**Rank-IC** (`_rank_ic`): Spearman correlation between `rank_signal` at
formation and each name's realized buy-and-hold return over
`[as_of_date, next_as_of_date]`, computed across the **full scored
universe** (not just the two extreme deciles) — a portfolio-construction-
independent efficacy check.

**Benchmarks**: SPY (external — "did this beat the market") and an
internal equal-weight portfolio of that quarter's full resolvable
universe (controls for the CUSIP-mapping coverage gap — separates
genuine factor skill from "the universe we could map is itself a biased
slice"). Both carry **no simulated transaction costs**, an intentional
asymmetry disclosed in the report — a single buy-and-hold ETF position
is not comparable, cost-wise, to a quarterly-rebalanced 40-name book.

#### Sub-period / regime analysis (`detail.py`)

`subperiod_stats` splits a lag's already time-ordered closed-quarter
list into `n_splits` contiguous, roughly-equal-count chunks
(`np.array_split` on the index range) and recomputes the exact same
`apply_transaction_costs`/`_stitch`/`performance_stats` pipeline per
chunk — no new stats logic, a slice of the same tested pipeline. IC
aggregation per chunk drops `NaN` values before computing mean/std/
%-positive, matching `run_backtest`'s own convention.

`quarter_asset_detail` reconstructs each quarter's per-ticker holdings
and returns directly from `prices.parquet` + the already-pickled basket
membership (`long_tickers`/`short_tickers`), reconciled **per leg**
against `backtest.leg_nav` itself (both the dropped-ticker set and the
total return must tie out exactly) — not against the combined
`spread_nav`, for the reason stated above.

---

## 2. Chronological Narrative

### Phase 1 — Ingestion & Point-in-Time Panel

**Built**: `src/ingest.py` (SEC EDGAR download/parse) and `src/pit.py`
(point-in-time snapshot logic). Full detail in `docs/phase1_report.md`.

**Key design decisions**: scrape SEC's index page for real filenames
rather than templating a URL (SEC changed its naming convention in 2024,
from calendar-quarter buckets to rolling 3-month windows — a hardcoded
template would silently miss data across that boundary); only parse
`SUBMISSION` and `INFOTABLE` (the point-in-time axis and the holdings
themselves — `COVERPAGE` added later, in Phase 2); 13F-NT ("notice")
filings drop out automatically via an inner join on `ACCESSION_NUMBER`,
no special-case code; per-filing dedup on `(ACCESSION_NUMBER, CUSIP)`
since SEC's schema lets a manager split one position across multiple
voting-authority rows.

**The no-lookahead rule itself** (`pit.as_of_snapshot`): filter to
`FILING_DATE <= as_of_date` *before* picking each CIK's latest filing —
no backfill, ever. A 13F-HR/A amendment is treated as a full replacement
of the prior filing for that period ("latest filing wins"), a deliberate
scope cut rather than parsing SEC's inconsistently-documented
`AMENDMENTTYPE` field to merge partial restatements.

**Bugs found and fixed** (post-review hardening, commits `1915909` and
`4b970c3`): unpinned dependencies (a fresh install had already pulled
breaking-change major versions); no Python-version floor enforced
despite PEP 604 syntax requiring ≥3.10; an all-NaN `VALUE`/`SSHPRNAMT`
group silently summed to a fake `0` instead of `NaN`; non-atomic
downloads could leave a truncated zip masquerading as a valid cache hit;
no dedup across dataset boundaries at the 2024 naming-scheme transition;
a hardcoded URL base silently reintroduced the exact brittleness the
filename-scraping was built to avoid; unused columns parsed at full
scale; no checkpointing in `--full` mode (a late-run crash meant
re-parsing everything already done). All seven fixed, verified against
real data, covered by new tests (suite grew 9 → 33).

**Validated against real data**: 3.9M rows / 14,353 filings from the
3-dataset sample; a full `--full` run produced the 806MB
`13f_panel_full.parquet` used throughout Phase 4.

### Phase 2 — CUSIP Mapping & Universe Definition

**Built**: `src/mapping.py` (CUSIP → ticker via OpenFIGI), `src/
universe.py` (point-in-time S&P 500 membership + passive-manager
exclusion), plus a `COVERPAGE`-parsing addendum to `src/ingest.py`. Full
detail in `docs/phase2_report.md`.

**COVERPAGE addendum**: adds filer-identity and confidential-treatment
detection. `CONFDENIEDEXPIRED` decoded from real filed examples (blank =
no request; `'N'` = still confidential; `'Y'` = expired, now disclosed).
Left-joined onto the existing `SUBMISSION`⋈`INFOTABLE` panel so a
`COVERPAGE` parsing gap can never silently drop an otherwise-valid
holdings row. Bundled fix: CIK zero-padding (`str.zfill(10)`) — found
because the same real filer appeared in the panel both as `"1349434"`
and `"0001349434"`, which would have silently broken exact-string
passive-manager exclusion for whichever form wasn't listed.

**Four real bugs found building the OpenFIGI crosswalk**: (1) the
unauthenticated batch limit is 10 jobs/request, not the documented 100
(discovered via a real `413` on the 11th job); (2) the primary-listing
filter (`exchCode == "US"`) doesn't generalize — verified concretely
that ExxonMobil's real listings use `UZ`/`OU`/`QU`, not `US`, causing
the naive filter to silently resolve XOM's CUSIP to an unrelated
Colombian listing — fixed by grouping on `compositeFIGI` and taking the
largest group; (3) zero resilience to transient failures, hit for real
mid-build (`OSError: No route to host`) — fixed with exponential-backoff
retry plus a checkpoint file; (4) a `pd.concat` dtype-corruption bug
where an empty placeholder frame's `object`-dtype `RESOLVED` column
demoted the real `bool` column on concat, causing `~` (invert) to
execute integer bitwise-NOT instead of logical negation — root-caused
and fixed with explicit per-column dtypes plus a defensive cast,
regression-tested against the exact crash path.

**Residual coverage gap, investigated not assumed**: OpenFIGI resolves
a CUSIP to its *current* ticker only. Confirmed two distinct causes —
genuine delistings via M&A (Monsanto, EMC, Red Hat, and others with no
current ticker to resolve to at all) and pure ticker renames on an
unchanged CUSIP (Facebook's CUSIP → `META` never `FB`; BNY Mellon's →
`BNY` never `BK`) — demonstrated, not comprehensively patched, via 4 real
entries in `cusip_overrides.csv`.

**Universe definition**: `sp500_members(as_of_date, ...)` — point-in-time
S&P 500 membership from a free, MIT-licensed history source, keyed on
`as_of_date` not `period_of_report`. Passive-manager exclusion list
curated and cross-verified against real `FILINGMANAGER_NAME`/`VALUE`
data at multiple points across the window.

**Post-report bias audit found and fixed a real survivorship-bias bug**:
the original passive-manager CIK list was cross-checked against only the
*most recent* quarter of data, which meant it silently missed that
Vanguard and BlackRock each filed under an entirely different CIK for
most of 2013-2025 — the exclusion was a **silent no-op for roughly 13 of
the ~13.5-year backtest window**. Found by checking real
`FILINGMANAGER_NAME`/`VALUE` at four points spread across the full
window (2015, 2019, 2023, 2026) using already-cached raw data. Fixed by
listing both eras' CIKs (14 entries, up from 9), verified by recomputing
AAPL's breadth with/without exclusion at an early- and a recent-window
date, and regression-tested directly against the real committed
reference file so it can't silently regress.

### Phase 3 — Ownership Breadth Momentum Factor

**Built**: `src/factor.py`. Full detail in `docs/phase3_report.md`.

**Design decisions confirmed before implementation**: standardize via
both percentile rank (primary — bounded, robust to a single outlier
event) and z-score (secondary, explicitly not winsorized, disclosed as
more outlier-sensitive); drop, don't impute, a CUSIP with no
prior-period breadth; no minimum-history requirement beyond having both
quarters present.

**The central point-in-time design decision**: both `breadth(t)` and
`breadth(t-1)` evaluated as of the *same* `as_of_date` (see 1.3 above for
the full reasoning) — required **zero new point-in-time logic**, since
it composes `sp500_universe_breadth()` (already correct from Phase 2)
unchanged, called twice. Proven by a dedicated test constructing a
late-arriving prior-quarter filing and checking both directions: included
when `as_of_date` is after it, excluded when before.

**A real methodology issue caught during end-to-end validation, not a
code bug**: the first real check produced an implausible ~30x jump in
breadth for every name in the universe. Traced to the validation sample
panel's three datasets not including the on-time filing window for the
prior quarter — it only had late-arriving stragglers, because that
sample panel was built in Phase 1 to prove both SEC naming eras parse
correctly with a minimal download, not to support quarter-over-quarter
comparison. Resolved by building a proper two-adjacent-quarter panel
from already-cached raw data; flagged that any real backtest needs the
full historical panel with genuine adjacent-quarter coverage — carried
forward as an explicit Phase 4 prerequisite (see below for how that
played out).

**Post-implementation review**: no correctness bugs found (verified by
mentally mutating the implementation against each test assertion — a
sign flip, a left-join-instead-of-inner, a frozen prior `as_of_date`,
a flipped sort direction — and confirming each would fail a specific
test). One real, disclosed-not-fixed property: raw breadth change
mechanically favors mega-caps (0.33-0.37 correlation between baseline
breadth and rank-extremity), carried into the memo's honest
noise-vs-signal discussion.

### Phase 4 — Backtest, Report, and Analysis

**Built**: `src/backtest.py`, `src/report.py`, `src/detail.py`,
`notebooks/regime_and_attribution.ipynb`. This phase's arc spans one
extended working session; full detail across
`docs/phase4_efficient_implementation.md`, `docs/phase4_status.md`, and
`docs/phase4_audit.md`.

**Starting state**: the backtest engine and report generator were
already implemented and fully tested, but the real end-to-end run had
crashed twice without ever producing a completed result — no
`backtest_results.pkl` had ever existed.

**The bottleneck, misdiagnosed then correctly diagnosed via real
profiling**: an earlier plan attributed the crash to `pit.py` never
indexing the panel by `PERIODOFREPORT`, and proposed building that
indexing from scratch. Checking the actual code first showed this
diagnosis was stale — that exact indexing (`panel.attrs
["period_groups"]`, an O(k) positional lookup) already existed.
Profiling the real panel (8 real quarters, `cProfile`) revealed the
*actual* dominant cost was something neither diagnosis anticipated:
pandas 3's default Arrow-backed string dtype routes `.iloc[]` through a
slow `pyarrow` take kernel (242 of 336.7 seconds on 8 quarters), and
pandas deep-copies the large `period_groups` `.attrs` dict on every
downstream operation (79% of remaining runtime after the first fix).
Both fixed — `CIK`/`ACCESSION_NUMBER`/`CUSIP` converted to `category`
dtype once (`backtest.prepare_panel`), and `.attrs` cleared on
`_period_slice`'s returned slice once it's served its purpose — for a
combined, measured **~31x speedup** (336.7s → 10.9s on the same 8
quarters), memory flat at ~9.3GB peak, verified byte-identical to the
original slow path via `pd.testing.assert_frame_equal` on real data.
Also added (a genuinely still-open gap, unrelated to the main
bottleneck): caching `sp500_members`'s repeated CSV parse/rebuild
(~300+ redundant calls per full grid run) and a `--quick` CLI flag for
fast dev iteration.

**The real full-history run**: window 2015-09-30 to 2026-03-31 (auto-
detected — first quarter reaching ≥300 scored names), 515 tickers priced,
all 12 lag×cost combinations completed. Only 4 tickers total (across
every closed quarter) had no usable price data — real delistings/
acquisitions (`SNI`, `CELG`, `TWTR`), not a data-pipeline defect.

**Honest headline result** (primary: 60-day lag, 10bps): the long-short
spread underperforms both benchmarks on every risk-adjusted metric
(Sharpe 0.15 vs. SPY's 0.84; max drawdown -55.4% vs. SPY's -33.7%).
Rank-IC is weakly positive at every lag (0.016-0.023) but not
statistically significant (t-stat ~0.6-1.1). Lag sensitivity is
**non-monotonic** — the 60-day primary lag is actually the *worst*-
performing of the three by Sharpe, contradicting a clean front-loaded-
decay story and, notably, real evidence against the primary lag having
been cherry-picked after seeing results (it was fixed a priori). Cost
sensitivity is severe: ~77-82%/quarter one-way turnover on each leg
means the edge nearly vanishes by 25bps (annualized return 3.92% → 0.76%
across the 0-25bps grid). The most likely explanation, laid out in the
memo: the mega-cap tilt disclosed in Phase 3 colliding with a decade of
mega-cap-led market returns, rather than the breadth-momentum concept
being disproven outright.

**Pre-commit audit** (`docs/phase4_audit.md`): independently re-derived
and verified the core computation logic against the real output (decile
direction, cost-drag math cross-checked to the exact percentage point,
closed/open gating explained via a real boundary-date coincidence,
rank-IC scope, benchmark asymmetry). Found and closed a real gap in this
session's own test coverage (`ticker_to_cusip` threading through
`universe.py`/`factor.py`/`backtest.py` had no explicit regression test
— added one, verified against real data first). Found one genuine,
previously-flagged-but-never-fixed data issue: the panel used for this
entire run predates `ingest.py`'s CIK zero-padding fix (Phase 3's report
had explicitly recommended rebuilding the panel as a Phase 4
prerequisite; this was never done). Precisely scoped, not left as a
vague risk: 0.22% of rows affected, confirmed **no overlap** with the 14
curated passive-manager CIKs (the specific failure mode Phase 2's audit
had found and fixed), and only a mild double-count of ~21 real filers'
breadth in some quarters — low materiality, disclosed as a recommended
follow-up rather than blocking the commit.

**Per-asset detail and regime/attribution analysis** (`src/detail.py`,
`notebooks/regime_and_attribution.ipynb`): `quarter_asset_detail`
reconstructs every quarter's individual ticker returns, contributions,
and weights directly from already-committed artifacts — no re-run,
no changes to `backtest.py`'s tested core — reconciled per-leg against
`leg_nav` itself (see 1.3 above for why per-leg, not per-spread).
`subperiod_stats`/`subperiod_navs` split the closed-quarter history into
regime chunks, reusing the exact same tested stats pipeline. **Real
finding**: the weak full-sample number is not uniformly weak — a
consistently bad 2019-2022 regime (negative Sharpe at every lag,
including COVID and the 2022 bear market) is averaged against a
consistently strong 2022Q4-2026Q1 regime (Sharpe >0.65 at every lag, up
to 1.14). That recent strength is **concentration-driven**: the largest
individual contributions are almost entirely semiconductor/AI names
(`SNDK`, `AMD`, `INTC`, `MU`) in 2025, and `NVDA` alone is the single
largest cumulative contributor across the whole window, long in 28 of
~42 quarters. Net read: more evidence of *something real* than the
pooled full-sample number suggested on its own, but closer to "the
signal caught one secular theme" than "a robust, diversified
breadth-momentum edge" — a finding for the memo's Next Steps discussion,
not a rewrite of its honest full-sample headline numbers.

### Cross-Platform Reproducibility Hardening

The case study's first deliverable is "a working pipeline, runnable
from a clean clone" — no OS specified, and evaluators aren't guaranteed
to be on macOS/Linux. Real testing on Windows (not assumed, not
skipped) surfaced a series of concrete gaps, each investigated and
fixed in turn, then re-verified against real Windows testing:

1. **`setup.sh` didn't run at all on native Windows `cmd`/PowerShell**
   (it's bash), and the README's "manual" fallback was Unix-only too
   (`python3`, `source .venv/bin/activate`) — neither documented setup
   path actually worked on Windows. Fixed: an explicit Windows section
   in `README.md` (WSL recommended, Git Bash as an alternative), real
   per-OS manual-setup commands (Windows native venv activation is
   `.venv\Scripts\activate`, not `source .venv/bin/activate`).
2. **No OS-agnostic Python version signal.** The `>=3.10` check lived
   only inside `setup.sh`, which never runs on Windows — a Windows user
   got zero warning before hitting the exact `TypeError` (from `pit.py`'s
   PEP 604 syntax) that check exists to prevent. Fixed: a `.python-version`
   file stating the exact tested version (3.12.3, not just the `>=3.10`
   floor), and `setup.sh` itself made more robust (prefers `python3`,
   falls back to `python` — Windows' official installer only registers
   `python.exe`, not `python3.exe`, so even Git Bash could fail on
   "command not found" before ever reaching the version check).
3. **No `.gitattributes`** — line-ending normalization was unmanaged,
   risking a CRLF-corrupted `setup.sh` depending on a cloning machine's
   git config. Fixed, with `.parquet`/`.pkl` explicitly marked binary
   (not left to heuristic auto-detection) and the renormalized binary
   artifacts verified byte-identical (`md5sum`) before committing.
4. **README commands broke on literal copy-paste into `cmd.exe`** —
   bash-style trailing-backslash line continuations and inline `#`
   comments aren't just unsupported there, they get passed to the
   Python program as literal unrecognized arguments (`cmd.exe` has no
   continuation or comment syntax for either character). Fixed: every
   command in every `bash`-fenced block reduced to one physical line
   with no inline comments, and the one block that mixed two OS
   variants via `#`-comment section headers split into two separate
   blocks.
5. **`python -m src.ingest --full` stopped mid-run with no error
   message, and produced no progress output at all from start to
   finish** — traced to `ingest.py` being the one module in the
   pipeline that never received the retry/progress hardening
   `mapping.py` and `backtest.py` each got after hitting real transient
   network failures during their own phases (see Phase 2 and Phase 4
   above) — a bare, unretried `requests.get()`, with a single `print()`
   only at full success. Fixed: `_get_with_retry`, mirroring
   `mapping.py::_post_with_retry`'s proven schedule (5 attempts,
   5s/10s/20s/40s backoff, retries `ConnectionError`/`Timeout`/429/5xx,
   fails fast on a permanent error like 404 rather than burning through
   retries pointlessly), plus progress printing throughout
   (start-of-run message, per-dataset `[i/n]` progress, byte counts on
   completed downloads, silent on cache hits so reruns aren't spammy).

**Verified, not just implemented**: the retry logic was exercised
against the real, live SEC site (a simulated transient failure
recovered mid-call against real data, not a mock), and a full
validation-sample run was confirmed to show continuous progress output
end to end. Final confirmation came from the user re-running `--full`
ingest on their actual Windows machine after these fixes landed — it
completed successfully, closing the loop on an issue this environment
could only reason about, not reproduce directly.

6. **A real `--full` run went on to hit two more failures at the exact
   same code location**: a `TypeError` deep in pandas 3.0.5 internals
   (`groupby → __finalize__: "'slice' object is not callable"`) parsing
   `2015q1_form13f.zip`, and `2015q4_form13f.zip` appearing to hang
   indefinitely with no error at all. Root-caused (not guessed) by
   reproducing against the exact same real files, cached locally from
   earlier in this session: `_clean_infotable`'s
   `.agg(col=(col, lambda s: s.sum(min_count=1)))` routes through
   pandas' "pure Python" per-group aggregation fallback — any custom
   callable forces this, since it can't use the cython-optimized path.
   Measured directly: **77.3 seconds** for one ~1.8M-row dataset on this
   (Linux, same pandas version) machine — it didn't crash here, but a
   fallback path that slow and that fragile is exactly the kind of code
   a different platform's pandas build can behave badly in, and there
   was no need to rely on it at all. Fixed: `groupby(...)[cols].sum(
   min_count=1)` — `min_count` is natively supported by groupby's own
   `sum()`, preserving the exact "NaN stays NaN, not a fake zero"
   semantics without ever touching the fallback. Verified byte-identical
   output against the old lambda form (`assert_frame_equal`, real data)
   and **>200x faster** (77.3s → 0.36s for that file; both real problem
   datasets now complete in ~2-3s each, full pipeline included).

---

## 3. Defense Quick-Reference Index

Mapped directly to the case study prompt's own stated evaluation
criteria (`docs/prompt.pdf`):

| Evaluation criterion | Where addressed |
|---|---|
| **Quality of factor hypothesis** — coherent story, alternatives considered | `docs/memo.md` Factor Hypothesis section (Chen-Hong-Kubik 2002 grounding, Miller 1977 short-sale-constraint mechanism); two alternatives explicitly considered and rejected (value-weighted "smart money" following, raw concentration/Herfindahl) — see memo for the reasoning against each. |
| **Point-in-time discipline** — reporting lag, lookahead | §1.3 above (`pit.as_of_snapshot`'s no-backfill rule); the same-`as_of_date` design in `factor.breadth_change` (§1.3, Phase 3 above); the 45/60/90-day lag sweep and its non-monotonic real result (Phase 4 above); dedicated tests in `tests/test_pit.py` including the fast-path/boolean-mask equivalence tests added this session. |
| **Data engineering care** — CUSIP mapping, filer dedup, amendments, confidential treatment | CUSIP mapping: `mapping.py`, 4 real bugs found+fixed (Phase 2 above). Filer dedup: `_winning_accessions`' same-day tie-break by accession number (§1.3); voting-authority-split row dedup in `ingest.py` (Phase 1 above). Amendments: "latest filing wins," a deliberate, disclosed scope cut vs. parsing `AMENDMENTTYPE` (Phase 1 above, §1.3). Confidential treatment: `COVERPAGE`'s `CONFDENIEDEXPIRED` detection (Phase 2 above), disclosed as fundamentally unrecoverable from public data until later disclosure, not a gap engineering can close. |
| **Backtest methodology** — costs, rebalancing, benchmarks, quarterly signal in a daily framework | §1.3 above in full: transaction cost formula, quarterly rebalancing with daily mark-to-market via `leg_nav`, SPY + internal-universe benchmark choice and the cost asymmetry disclosed for it, the daily-rebalanced spread-NAV construction and why it isn't a simple return difference. |
| **Ability to defend every choice** | This document in full, plus the six phase-specific reports it cross-references, plus `docs/memo.md`'s own Scope Decisions section (what was deliberately excluded, and why, for every axis: holder universe, equity universe, time window, CUSIP genealogy, amendment parsing, value-weighting). |
| **"Runnable from a clean clone"** (deliverable #1) | [Cross-Platform Reproducibility Hardening](#cross-platform-reproducibility-hardening) above — every gap found via real Windows testing (not assumed), fixed, and re-verified; final confirmation was the user's own Windows machine completing a full `--full` ingest run after the fixes landed. |
