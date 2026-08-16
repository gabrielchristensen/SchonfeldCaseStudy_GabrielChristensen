# 13F Positioning Factor & Backtester — Memo

## Factor Hypothesis

The signal is **ownership breadth momentum**: the quarter-over-quarter
change in the number of distinct 13F filers holding a stock,
cross-sectionally standardized within the S&P 500. Formally,
`signal(CUSIP, t) = rank[breadth(t) - breadth(t-1)]`, where `breadth` is
the count of distinct institutional CIKs reporting a position as of the
current formation date (not a frozen historical snapshot — see
Methodology).

The hypothesis is grounded in Chen, Hong & Kubik (2002), who show that
changes in the number of mutual fund owners of a stock predict returns,
and in the short-sale-constraint mechanism from Miller (1977) that
motivates it: a stock's price reflects only the beliefs of investors
willing to hold it. When breadth is low, a stock's price is set by a
narrower, more optimistic subset of potential owners, and pessimistic
information is more likely to be excluded from the price (would-be
sellers who never bought can't push the price down by staying out).
Rising breadth is a direct, observable signal that this constraint is
relaxing — more distinct institutional views are entering the price
discovery process — which should on average resolve upward as previously
excluded information gets incorporated. Falling breadth is the mirror
case: a narrowing pool of holders, consistent with an information
disadvantage building up outside the price.

This is a **positioning-breadth** story, deliberately distinct from two
adjacent, more obvious alternatives that were considered and set aside:

- **Value-weighted "smart money" following** (sizing the signal by 13F
  reported dollar value, not just filer count) was rejected as the
  *primary* signal because it conflates two different things — a handful
  of very large managers making a large bet, vs. many independent managers
  each taking a smaller position. The latter is closer to what the
  short-sale-constraint story actually predicts (breadth of *independent
  opinion*, not concentration of capital), and it's also mechanically
  simpler to get right: VALUE's units changed on 2023-01-03
  (thousands-of-dollars → whole-dollars, see `ingest.py`), a correction
  breadth-counting doesn't need at all.
- **Raw concentration** (e.g., Herfindahl index of position sizes among
  holders) measures something related but answers a different question —
  "how unevenly is this stock held," not "is the pool of holders
  growing or shrinking." It was considered and set aside as a candidate
  second factor for a later phase, not built here, to keep this case
  study to one well-defended signal rather than several shallow ones.

## Scope Decisions

**Holder universe**: all 13F filers, minus a small, disclosed, curated
exclusion list of known passive/index managers (Vanguard, BlackRock,
State Street, Geode, Northern Trust, Schwab — see
`data/reference/passive_manager_ciks.csv`). Index funds' holdings are
mechanical (they hold what the index holds, when it's added), not a
positioning signal, and would otherwise dilute genuine active-manager
breadth changes with pure index-reconstitution noise. This list is
deliberately *not* exhaustive (e.g., BlackRock's ~25 smaller
international/regional subsidiary CIKs are not individually included) —
a curated list of the largest, most clearly mechanical filers, not an
attempt at full passive-fund classification.

**Equity universe**: the S&P 500, point-in-time (not today's list —
avoids survivorship bias), sourced from a free, MIT-licensed historical
membership dataset. Chosen over "all 13F-eligible equities" for two
reasons: (1) it bounds the CUSIP→ticker mapping problem to a manageable,
well-covered set rather than the full ~150K distinct CUSIPs ever seen in
the raw panel; (2) it keeps the backtest's price/return data (see below)
tractable within this case study's time budget, since every name needs
real daily price history, not just a holdings record.

**Time window**: the full available window, roughly 2013Q3–2026Q1 (the
exact first usable formation date is validated empirically against the
rebuilt panel — see Methodology). Chosen over a shorter, more recent
window to maximize statistical power (more independent quarterly
observations) for the honest assessment the Results section below is
asking for, at the cost of including years where CUSIP-mapping coverage
and passive-manager CIK identification were verified to be messier (see
`docs/phase2_report.md`'s bias audit).

**What was deliberately excluded**, and why:

- **Full historical CUSIP/ticker genealogy.** The crosswalk resolves each
  CUSIP to its *current* ticker only (OpenFIGI's native behavior), patched
  with four manually-curated override rows for real corporate actions
  (Honeywell, Medtronic) and ticker renames (Meta/Facebook, BNY/BK) — not
  comprehensively reconstructed for the ~35% of ever-S&P-500 tickers that
  were acquired, merged, or renamed somewhere in the window. Reconstructing
  a full 14-year ticker genealogy was judged out of scope — a "trying to
  do everything" failure mode the case study explicitly warns against.
- **Amendment-type parsing.** A 13F-HR/A amendment is treated as a full
  replacement of the prior filing for that period, not merged based on
  SEC's `AMENDMENTTYPE` field (partial vs. full restatement) — that
  field's real-world semantics are inconsistent enough in SEC's own
  documentation that guessing at merge logic risked being wrong in ways
  that are hard to detect, versus the safer, disclosed "latest wins" rule.
- **Value-weighting anywhere in the factor or the portfolio.** Both the
  signal (breadth momentum, not value-weighted flow) and the backtest
  portfolio (equal-weight within each decile) avoid market-cap or
  position-size weighting — simpler, no additional data dependency, and
  avoids a few mega-caps dominating either the signal or the P&L.
- **Confidential-treatment-request recovery.** COVERPAGE data detects
  when a filing had a confidential-treatment history, but the *omitted*
  position itself is fundamentally unrecoverable from public data until
  the manager's own later disclosure — a property of the reporting
  regime, not something more engineering fixes.

## Methodology

### Data ingestion and point-in-time discipline

The pipeline (`ingest → pit → mapping → universe → factor → backtest`)
downloads SEC's structured 13F datasets directly (not a third-party
aggregator), parses `SUBMISSION` (filing metadata, critically
`FILING_DATE` — the EDGAR acceptance date), `INFOTABLE` (holdings), and
`COVERPAGE` (filer identity, confidentiality flags), and deduplicates
voting-authority-split rows within a filing before anything downstream
sees the data.

**The reporting-lag / lookahead question is handled at the lowest layer,
once, and every downstream stage inherits it for free.**
`pit.as_of_snapshot(panel, period_of_report, as_of_date)` defines what's
"known" as of any date: for each filer, the most recently *filed*
submission (original or amendment) with `FILING_DATE <= as_of_date`. A
filer with no accepted filing yet by `as_of_date` is simply absent — never
backfilled once a later filing arrives. This is the literal no-lookahead
guarantee: nothing that becomes public after `as_of_date` can ever
influence a computation keyed on that date, checked directly by a
dedicated test suite (`tests/test_pit.py`).

**Formation lag.** 13F filings are due 45 days after quarter-end but,
empirically (measured on the full 305K-filing panel), only 87.0% arrive
by that exact deadline; 96.3% are in by day 60, 96.8% by day 90, with a
long tail out past a year for genuine stragglers. Rather than pick one
lag and assume it's right, the backtest sweeps **45/60/90-day** formation
buffers as three independent runs. This is a real, disclosed
methodological tension: a longer lag captures more filers before the
cross-section is formed, but also means trading later relative to when
the underlying ownership shift actually happened — if the return effect
is front-loaded (a plausible reading of the short-sale-constraint
mechanism above), a longer lag could capture less of it. The Results
section reports what the sweep actually shows, not an assumption in
either direction.

**Quarter-over-quarter comparison, same as_of_date for both terms.**
`factor.breadth_change()` computes `breadth(t)` and `breadth(t-1)` both
evaluated *as of the current formation date* — not a frozen snapshot of
the prior quarter at its own, earlier formation date. This is deliberate:
by the current formation date, everything about the prior quarter is
already fully public regardless of how complete it looked back when it
was originally formed, so using the freshest available data for both
terms is the correct point-in-time choice, not lookahead (it would only
be lookahead if `as_of_date` predated when that information became
public, which it never does here). A name with no prior-quarter breadth
at all — almost always a recent index addition — is dropped, not imputed,
so index-reconstitution noise can't masquerade as ownership momentum.

### Factor construction

`raw_change = breadth(t) - breadth(t-1)`, cross-sectionally standardized
within the S&P 500 universe each quarter via percentile rank (primary
signal, bounded and robust to outliers by construction) and z-score
(secondary, explicitly not winsorized). One known, disclosed property:
raw (unscaled) breadth change mechanically favors names with larger
baseline breadth (correlation ~0.33–0.37 between prior-quarter breadth
and both `|raw_change|` and rank-extremity, measured on real data) —
counting-statistics variance scales with the baseline level. This was not
"fixed" with a percentage-based alternative, because that has its own,
arguably worse problem: a thinly-held name going from 5 to 10 holders
reads as a "100% increase," almost certainly noise. The practical
consequence, stated plainly: the short leg of the long-short portfolio in
particular likely skews toward larger-cap names within the S&P 500, not a
clean signal across the full cap spectrum within that universe.

### Backtest construction

Quarterly rebalancing, on the formation-date schedule above. At each
formation date, names are sorted into **deciles** on `rank_signal`; the
top decile is held long, the bottom decile short, both **equal-weighted**
(no market-cap dependency). Positions are marked **daily** against real
adjusted-close prices (`yfinance`) from formation to the next formation
date — no intra-quarter trading — which is what lets the backtest report
genuine daily-return statistics (Sharpe, volatility, max drawdown) rather
than a single number per quarter.

**Transaction costs** are swept across **0/5/10/25bps** one-way, applied
to each leg's quarterly turnover (names entering/exiting the decile).
**Benchmarks**: SPY (external — "did this beat the market") and an
internal equal-weight portfolio of that quarter's full resolvable
universe, rebalanced on the same schedule (controls for the CUSIP-mapping
coverage gap — separates genuine factor skill from "the universe we
could actually map is itself a biased slice of the true S&P 500").

**Missing price data.** A mapped ticker with no usable price data for a
given quarter (delisted, renamed to an unmapped label, thin coverage) is
dropped from that quarter's basket, not imputed — layered on top of the
already-disclosed CUSIP-mapping coverage gap, not a new hidden hole. One
concrete bug found and fixed while building this: `mapping.py`'s
CUSIP→ticker lookup originally resolved Facebook's and BNY Mellon's
CUSIPs to their *retired* tickers (`FB`, `BK`), which — verified directly
against Yahoo Finance — have zero price history at all (Yahoo does not
keep historical prices addressable under a name's pre-rename ticker).
Fixed by preferring the current-ticker row, the convention the override
file itself already used; see `src/mapping.py`'s `cusip_to_ticker`
docstring.

**A quarter only enters headline performance stats once its exit (next
formation date) has actually occurred.** The most recent, still-open
quarter is tracked and shown separately, never blended into Sharpe/
drawdown numbers computed from only-fully-realized returns.

## Results

Full-history backtest, rebuilt panel (81.8M rows), auto-detected window
**2015-09-30 to 2026-03-31** (~10.5 years, 42-43 quarterly rebalances
depending on lag), 45/60/90-day formation-lag × 0/5/10/25bps one-way-cost
grid, all 12 combinations run to completion. 515 tickers in the price
universe; only **4 tickers total** across every closed quarter had no
usable price data and were dropped — the CUSIP-mapping coverage gap
disclosed in Methodology turned out to be a minor effect in practice, not
a major one. Scored cross-section averaged 370 names/quarter (range
304–428), consistent with a real S&P-500-sized universe throughout.

**Primary result (60-day lag, 10bps), long-short spread vs. both benchmarks:**

| | Long-short spread | Internal universe (EW) | SPY |
|---|---:|---:|---:|
| Annualized return | **2.65%** | 12.96% | 14.99% |
| Annualized vol | 17.19% | 18.17% | 17.85% |
| Sharpe (rf=0) | **0.15** | 0.71 | 0.84 |
| Max drawdown | **-55.4%** | -41.2% | -33.7% |
| Hit rate (daily) | 52.8% | 54.6% | 55.4% |

This is a weak, honestly negative result, not a positive one dressed up.
The long-short spread is barely profitable, carries market-level
volatility with none of the market's return, and draws down nearly twice
as deep as SPY over the same window. It underperforms even the internal
equal-weight universe benchmark — the portfolio-construction-independent
control for "is the mappable universe itself biased" — on every
risk-adjusted metric. The factor is not, as built, a standalone
long-short strategy an allocator would fund.

**Lag sensitivity does not confirm the front-loaded-decay mechanism —
and is not monotonic.** At 10bps: 45-day lag Sharpe 0.36 (max DD -28.4%),
60-day Sharpe 0.15 (max DD -55.4%), 90-day Sharpe 0.26 (max DD -43.5%).
If the short-sale-constraint mechanism in the Factor Hypothesis were
cleanly front-loaded, Sharpe should decay monotonically as lag lengthens;
instead the middle lag (60 days — chosen as primary *before* this sweep
ran, per the original methodology) is the worst of the three by both
Sharpe and drawdown, with 90 days recovering partway. Read plainly: the
sweep is **inconclusive on the decay question**, most consistent with
lag-to-lag variation being dominated by noise rather than a clean
information-decay curve at this sample size (42-43 independent quarterly
observations per lag).

**Cost sensitivity is monotonic, as expected, and the edge is fragile.**
At 60-day lag: annualized return falls from 3.92% (0bps) to 3.29% (5bps)
to 2.65% (10bps) to **0.76% (25bps)** — Sharpe from 0.23 to 0.04 over the
same range. This tracks directly from turnover: the long and short legs
each replace **~77-82% of their names every single quarter** (mean
turnover_long 0.766, turnover_short 0.817), because sorting on *raw*
(unscaled) breadth change — the deliberate, disclosed choice from Phase
3 — is noisy quarter to quarter for names near the decile boundary. At
any cost assumption above the low end of the disclosed grid, transaction
costs consume most of the strategy's already-thin edge.

**Rank-IC: weakly positive, not statistically significant at any lag.**
Mean Spearman IC of `rank_signal` against realized holding-period return,
across the full scored universe (not just the extreme deciles): 45-day
0.017, 60-day 0.016, 90-day 0.023 — all small and positive, with 56-67%
of quarters showing positive IC (better than a coin flip). But IC
standard deviation is 5-10x the mean at every lag (e.g. 60-day: mean
0.016, std 0.158, n=42), giving a t-stat around 0.6-1.1 at every lag —
none clear even the loosest significance threshold. The rank-IC check,
independent of the decile-portfolio construction and cost assumptions
entirely, agrees with the portfolio-level result: there is a real but
very weak positive tilt in the signal, well within the range consistent
with noise at this sample size.

**Honest noise-vs-signal read.** The most likely single explanation for
the negative long-short spread is the mega-cap tilt already disclosed in
Phase 3 (raw breadth change mechanically favors higher-baseline-breadth
names, most visible in the short leg) colliding with an 11-year window
that was, in aggregate, a strong and increasingly mega-cap-concentrated
bull market. Being structurally short a mega-cap-tilted basket for most
of 2015-2026 would produce exactly this signature — a wide, volatile,
underperforming spread — largely independent of whether ownership-breadth
momentum carries any genuine information at the security level. The
rank-IC result is consistent with that read: a small, non-significant
positive tilt survives at the cross-sectional level (where the mega-cap
confound is diluted across the whole scored universe, not concentrated
in one basket), while the decile-portfolio construction (which
concentrates the confound in the short leg specifically) shows a much
worse result. **Confidence is low that this specific construction
(raw-count, equal-weight decile, quarterly rebalance) reflects a tradeable
edge; confidence is also low that the underlying breadth-momentum concept
is worthless** — the IC evidence is too weak in either direction to
conclude cleanly, which is itself the honest conclusion, not a hedge.

*(The most recent quarter, formation 2026-03-31, is still open as of this
writing — partial spread NAV -1.6% through 2026-05-30, tracked separately
and correctly excluded from all statistics above.)*

## Next Steps

Three priorities, each tied directly to a specific finding above rather
than a generic list of "things one could try":

1. **Reduce turnover before anything else — the largest, most mechanical
   lever this backtest actually measured.** Cost sensitivity is the one
   result in this memo that is *not* ambiguous: ~80%/quarter turnover
   consumes 3.16 of 3.92 points of annualized return between 0bps and
   25bps, a strategy-design problem independent of whether the underlying
   signal is real. A banding/hysteresis rule (e.g., only trade out of a
   position if it falls outside the top/bottom 15% rather than exactly
   the top/bottom decile) or a smoothed version of `raw_change` (e.g., a
   2-quarter rolling average) would directly test whether most of that
   turnover is genuine signal turnover or boundary-crossing noise from a
   volatile raw count — resolvable with the data already on hand, no new
   ingestion.
2. **Test a scale-neutralized signal variant as a targeted robustness
   check on the noise-vs-signal read above**, not a wholesale replacement
   of the raw-count design. Specifically: a percentage-based or
   baseline-breadth-adjusted version of `raw_change`, scored and run
   through the identical backtest machinery already built, to see whether
   removing the mega-cap tilt from the short leg specifically closes the
   gap between the (weakly positive) IC result and the (negative)
   portfolio result. This is the most direct test of this memo's central
   open question — mega-cap-tilt confound vs. genuine information content
   — and Phase 4's own architecture (lag × cost grid, IC computation) was
   built generically enough that this is a signal-construction swap, not
   a rebuild.
3. **Re-run the lag sensitivity at finer granularity (e.g. 30/45/60/75/90
   days) before drawing any conclusion about decay**, given the current
   3-point sweep was non-monotonic and each point rests on only 42-43
   independent quarterly observations. A finer sweep either reveals a
   real (if noisy) decay curve the 3-point grid was too coarse to see, or
   confirms that lag, within this range, genuinely doesn't matter much —
   both are more useful findings than the current inconclusive 3-point
   result, and this is the cheapest of the three follow-ups (identical
   pipeline, no new data, no new signal logic).

Deferred, not prioritized above: extending the equity universe beyond the
S&P 500 and building a full CUSIP/ticker genealogy (both disclosed scope
cuts in Methodology) would improve coverage but are unlikely to be the
first-order explanation for this backtest's weak result — the coverage
gap measured at only 4 dropped tickers total, far too small to explain a
-55% max drawdown or a nonsignificant IC.
