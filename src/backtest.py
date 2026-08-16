"""Quarterly long/short backtest of the ownership-breadth-momentum factor.

Composes factor.breadth_momentum() (already point-in-time correct) across a
schedule of formation dates, sorts into decile portfolios on rank_signal,
marks each decile's equal-weight NAV daily against real adjusted-close
prices, and nets out transaction costs on quarterly turnover. Reports
against two benchmarks: SPY (external) and an internal equal-weight
portfolio of the same quarter's full resolvable universe (controls for the
~65-85% CUSIP-mapping coverage gap already disclosed in Phase 2 -- SPY
answers "did we beat the market", the internal benchmark answers "did the
factor add anything beyond that coverage gap").

Design decisions, confirmed in discussion before implementation (see
docs/phase4_report.md for the full reasoning):

- Formation lag is swept, not fixed: 45d/60d/90d as_of_date buffers after
  each quarter-end, run as three independent full backtests (LAG_DAYS_GRID).
  There is a real, unresolved coverage-vs-freshness tradeoff here -- a
  longer lag captures more filers (96.3% by day 60 vs. 87.0% by day 45,
  measured on the real panel) but enters further into any front-loaded
  post-disclosure return decay. This is measured empirically across the
  grid rather than assumed away in either direction; the memo states this
  plainly rather than treating a single lag choice as obviously correct.
- Transaction costs are also swept (COST_BPS_GRID: 0/5/10/25bps one-way),
  applied to quarterly turnover -- cheap to add per lag run since turnover
  doesn't depend on the cost assumption, computed once and reused.
- Daily NAV mark-to-market, equal-weight within each decile, no
  intra-quarter trading (buy at formation close, hold to next formation).
- A ticker with no usable price data for a given quarter (delisted,
  renamed away from a resolvable ticker, thin/no yfinance coverage) is
  dropped from that quarter's basket, not imputed -- a small, disclosed
  scope cut layered on the already-disclosed OpenFIGI current-ticker-only
  mapping gap, not a silent hole.
- A quarter is only included in headline performance stats once its exit
  (next formation date) has actually occurred; the trailing still-open
  quarter is tracked separately and never blended into headline Sharpe/
  drawdown numbers.

Price data note: fetch_prices/price_universe key everything off
mapping.cusip_to_ticker()'s CURRENT ticker per CUSIP, not the historical
ticker label sp500_members() uses for point-in-time index-membership
matching -- verified empirically against real Yahoo Finance data that a
retired post-rename ticker (e.g. "FB", "BK") has zero price history, only
the current one ("META", "BNY") does. This is also why the mapping.py
cusip_to_ticker() dedup direction was fixed in this phase (see that
module's docstring) -- discovered while building this price-fetch step.
"""

import time
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

from src import factor
from src import mapping as mapping_mod
from src import universe

DEFAULT_PRICES_CACHE = Path("data/processed/prices.parquet")
DEFAULT_QUICK_OUT = Path("data/processed/backtest_results_quick.pkl")

# pit.py/universe.py only ever read these 5 columns off the panel
# (SUBMISSIONTYPE/VALUE/SSHPRNAMT are ingest.py-only, unused by breadth
# counting) -- loading just these at --full scale keeps the resident panel
# well under the ~13GB a full 8-column read costs on the full ~82M-row
# historical panel, real headroom on a 15GB-RAM box.
PANEL_COLUMNS = ["CIK", "ACCESSION_NUMBER", "FILING_DATE", "PERIODOFREPORT", "CUSIP"]

LAG_DAYS_GRID = [45, 60, 90]
COST_BPS_GRID = [0, 5, 10, 25]
PRIMARY_LAG_DAYS = 60
PRIMARY_COST_BPS = 10
N_QUANTILES = 10
BENCHMARK_TICKER = "SPY"


def formation_dates(start_period, end_period, lag_days: int) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    """(period_of_report, as_of_date) for every quarter-end in
    [start_period, end_period]. as_of_date = period_end + lag_days calendar
    days -- a fixed calendar rule, not data-driven, so it stays simple to
    explain and reuse; LAG_DAYS_GRID is how the lag-length question itself
    gets resolved (empirically, via a sensitivity sweep), not by picking a
    per-quarter adaptive lag."""
    start = pd.Timestamp(start_period)
    end = pd.Timestamp(end_period)
    periods = pd.period_range(start=start, end=end, freq="Q")
    return [
        (p.end_time.normalize(), p.end_time.normalize() + pd.Timedelta(days=lag_days))
        for p in periods
    ]


def price_universe(
    mapping_df: pd.DataFrame,
    *,
    start_date,
    end_date,
    history_path: Path = universe.DEFAULT_HISTORY_PATH,
) -> set[str]:
    """Every CURRENT ticker resolvable for any CUSIP that was ever an S&P
    500 member in [start_date, end_date]. Computed once for the whole
    backtest window, not per-quarter -- the S&P 500 changes slowly (~20-30
    names/year), so re-deriving a near-identical set 48+ times would be
    pure waste.

    Deliberately resolves through the CUSIP layer (ticker -> CUSIP via the
    same reverse lookup as universe.sp500_members(), then CUSIP -> current
    ticker via mapping.cusip_to_ticker()) rather than returning the raw
    historical ticker labels straight out of sp500_history.csv -- a
    company's older membership rows may use a retired ticker label with no
    price history (see module docstring).
    """
    history = pd.read_csv(history_path)
    history["date"] = pd.to_datetime(history["date"])
    window = history[
        (history["date"] >= pd.Timestamp(start_date)) & (history["date"] <= pd.Timestamp(end_date))
    ]
    all_tickers: set[str] = set()
    for tickers_str in window["tickers"]:
        all_tickers.update(t.strip() for t in tickers_str.split(","))

    ticker_to_cusip = (
        mapping_df.dropna(subset=["TICKER"])
        .sort_values("CUSIP")
        .drop_duplicates(subset="TICKER", keep="first")
        .set_index("TICKER")["CUSIP"]
    )
    cusips = {ticker_to_cusip[t] for t in all_tickers if t in ticker_to_cusip.index}
    current = mapping_mod.cusip_to_ticker(pd.Series(sorted(cusips)), mapping_df).dropna()
    return set(current)


def _download_with_retry(tickers: list[str], start_date, end_date, *, max_attempts: int = 4) -> pd.DataFrame:
    """yfinance has no retry/backoff of its own for hundreds of tickers
    over a connection that can hiccup mid-pull -- mirrors the resilience
    pattern already established for network calls in ingest.py/mapping.py
    (exponential backoff on transient failures), even though this data
    source has no fallback provider."""
    delay = 5.0
    last_exc = None
    for attempt in range(1, max_attempts + 1):
        try:
            return yf.download(
                tickers,
                start=pd.Timestamp(start_date),
                end=pd.Timestamp(end_date) + pd.Timedelta(days=1),
                auto_adjust=True,
                progress=False,
                threads=True,
            )
        except Exception as exc:  # noqa: BLE001 -- yfinance raises assorted network/parsing errors
            last_exc = exc
            if attempt == max_attempts:
                raise
            time.sleep(delay)
            delay *= 2
    raise last_exc  # pragma: no cover -- unreachable, loop always returns or raises


def fetch_prices(
    tickers: set[str],
    start_date,
    end_date,
    cache_path: Path | None = DEFAULT_PRICES_CACHE,
    *,
    chunk_size: int = 80,
) -> pd.DataFrame:
    """Daily adjusted close for every ticker in `tickers`, long format
    [date, ticker, adj_close]. Cached to a local parquet (gitignored,
    data/processed/, per CLAUDE.md) keyed by ticker -- a ticker already in
    the cache is skipped on a re-run, mirroring the checkpoint pattern in
    ingest.py/mapping.py. Needed because the lag-sensitivity grid runs the
    full backtest three times against the same price universe; re-fetching
    ~500-800 tickers x 13 years from Yahoo Finance three times would be
    pure waste and adds unnecessary rate-limit risk.

    A ticker yfinance can't resolve at all, or resolves with zero usable
    price data in the window (delisted, renamed away, never listed), is
    recorded explicitly as an all-NaN row rather than left silently absent
    -- so a later run doesn't keep re-querying a name that's genuinely
    unresolvable, and so callers can tell "no data available" apart from
    "never fetched".
    """
    tickers = sorted(set(tickers))
    cache_path = Path(cache_path) if cache_path is not None else None
    if cache_path is not None and cache_path.exists():
        cached = pd.read_parquet(cache_path)
    else:
        cached = pd.DataFrame(columns=["date", "ticker", "adj_close"])
    done = set(cached["ticker"])
    remaining = [t for t in tickers if t not in done]

    frames = [cached]
    for i in range(0, len(remaining), chunk_size):
        chunk = remaining[i : i + chunk_size]
        data = _download_with_retry(chunk, start_date, end_date)
        if not data.empty and "Close" in set(data.columns.get_level_values(0)):
            close = data["Close"]
            long = close.stack().rename("adj_close").reset_index()
            long.columns = ["date", "ticker", "adj_close"]
            long = long.dropna(subset=["adj_close"])
        else:
            long = pd.DataFrame(columns=["date", "ticker", "adj_close"])
        present = set(long["ticker"].unique())
        missing = set(chunk) - present
        if missing:
            filler = pd.DataFrame({
                "date": pd.Timestamp(start_date),
                "ticker": sorted(missing),
                "adj_close": np.nan,
            })
            long = pd.concat([long, filler], ignore_index=True)
        frames.append(long)
        if cache_path is not None:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            pd.concat(frames, ignore_index=True).drop_duplicates(
                subset=["date", "ticker"], keep="last"
            ).to_parquet(cache_path)

    combined = pd.concat(frames, ignore_index=True).drop_duplicates(subset=["date", "ticker"], keep="last")
    return combined[combined["ticker"].isin(tickers)].reset_index(drop=True)


def assign_deciles(factor_df: pd.DataFrame, n_quantiles: int = N_QUANTILES) -> pd.DataFrame:
    """Adds a `decile` column: 0 = lowest rank_signal (short leg),
    n_quantiles-1 = highest rank_signal (long leg). rank_signal is already
    a [0, 1] percentile rank (factor.standardize_cross_section), so this is
    a plain equal-count qcut; duplicates="drop" guards a degenerate
    cross-section with too few distinct rank_signal values for the
    requested bin count rather than raising."""
    df = factor_df.copy()
    df["decile"] = pd.qcut(df["rank_signal"], n_quantiles, labels=False, duplicates="drop")
    return df


def leg_nav(prices: pd.DataFrame, tickers: set[str], start_date, end_date) -> tuple[pd.Series, set[str]]:
    """Equal-weight daily NAV (normalized to 1 on the first available date
    >= start_date) for a basket of tickers over [start_date, end_date].

    A ticker missing entirely, or missing right at the start of the
    window (can't be bought at formation), is dropped from the basket --
    equal-weight then reweights over the remaining names, matching the
    disclosed missing-price-data policy. Returns (nav, dropped_tickers) so
    callers can log what was excluded rather than have it vanish silently.
    """
    start_date = pd.Timestamp(start_date)
    end_date = pd.Timestamp(end_date)
    if not tickers:
        return pd.Series(dtype=float), set()

    window = prices[
        prices["ticker"].isin(tickers) & (prices["date"] >= start_date) & (prices["date"] <= end_date)
    ]
    if window.empty:
        return pd.Series(dtype=float), set(tickers)
    wide = window.pivot(index="date", columns="ticker", values="adj_close").sort_index()
    px = wide.ffill()

    dropped = set(tickers) - set(px.columns)
    if px.empty:
        return pd.Series(dtype=float), set(tickers)
    first_row = px.iloc[0]
    valid_at_start = set(first_row[first_row.notna()].index)
    dropped |= set(px.columns) - valid_at_start
    usable = sorted(valid_at_start)
    if not usable:
        return pd.Series(dtype=float), set(tickers)

    norm = px[usable] / px[usable].iloc[0]
    nav = norm.mean(axis=1)
    return nav, dropped


def leg_turnover(prev_tickers: set[str], new_tickers: set[str]) -> float:
    """One-way turnover fraction between two consecutive rebalances of an
    equal-weight leg: names newly bought this quarter / size of the new
    leg (0 = unchanged, 1 = fully replaced). An empty prior leg (the
    backtest's first quarter) correctly reads as full turnover (1.0) --
    building the entire position from scratch really is 100% one-way
    turnover, the real cost of the initial trade into the strategy, not an
    artifact to special-case away. (An earlier (added+removed)/(2n) form
    was tried and rejected: it silently returns 0.5, not 1.0, for the
    empty-prior case, since it implicitly assumes prev/new are
    comparably-sized ongoing legs rather than handling the "starting from
    nothing" boundary correctly.)"""
    if not new_tickers:
        return 0.0
    added = new_tickers - prev_tickers
    return len(added) / len(new_tickers)


def apply_transaction_costs(
    spread_nav: pd.Series, turnover_long: float, turnover_short: float, cost_bps: float
) -> pd.Series:
    """One-time cost haircut applied at the start of a quarter's spread NAV
    curve: cost = (turnover_long + turnover_short) * cost_bps/10000 * 2 --
    each leg's replaced names incur cost_bps on both the exiting sell and
    the entering buy (hence x2), and both legs (long + short) are traded
    independently at every rebalance. Applied as a constant multiplicative
    haircut across the whole quarter's curve, since the cost is paid once,
    at rebalance, not amortized daily."""
    cost = (turnover_long + turnover_short) * (cost_bps / 10000) * 2
    return spread_nav * (1 - cost)


def quarter_pnl(
    panel: pd.DataFrame,
    mapping_df: pd.DataFrame,
    period_of_report,
    as_of_date,
    next_as_of_date,
    prices: pd.DataFrame,
    *,
    n_quantiles: int = N_QUANTILES,
    prev_long: set[str] | None = None,
    prev_short: set[str] | None = None,
    passive_ciks: set[str] | None = None,
    history_path: Path = universe.DEFAULT_HISTORY_PATH,
    ticker_to_cusip: pd.Series | None = None,
) -> dict | None:
    """One quarter's long/short decile portfolios, held from as_of_date to
    next_as_of_date. Returns None if the factor cross-section or either
    extreme decile's price data is empty (nothing tradable that quarter --
    e.g. very early in the window) rather than raising, since a sparse
    early quarter is an expected, not exceptional, condition.

    IC (rank-IC): Spearman correlation of rank_signal at formation against
    each name's realized buy-and-hold return over the holding period,
    computed across the FULL scored universe (not just the two extreme
    deciles) -- a model-free check of factor efficacy, independent of the
    decile-portfolio construction/cost choices.
    """
    scored = factor.breadth_momentum(
        panel, period_of_report, as_of_date, mapping_df,
        passive_ciks=passive_ciks, history_path=history_path, ticker_to_cusip=ticker_to_cusip,
    )
    if scored.empty:
        return None
    scored = scored.copy()
    scored["TICKER"] = mapping_mod.cusip_to_ticker(scored["CUSIP"], mapping_df)
    scored = scored.dropna(subset=["TICKER"])
    if scored.empty:
        return None
    scored = assign_deciles(scored, n_quantiles)
    top = scored["decile"].max()

    long_tickers = set(scored.loc[scored["decile"] == top, "TICKER"])
    short_tickers = set(scored.loc[scored["decile"] == 0, "TICKER"])
    universe_tickers = set(scored["TICKER"])

    long_nav, long_dropped = leg_nav(prices, long_tickers, as_of_date, next_as_of_date)
    short_nav, short_dropped = leg_nav(prices, short_tickers, as_of_date, next_as_of_date)
    universe_nav, universe_dropped = leg_nav(prices, universe_tickers, as_of_date, next_as_of_date)
    if long_nav.empty or short_nav.empty:
        return None

    common_index = long_nav.index.union(short_nav.index)
    long_nav = long_nav.reindex(common_index).ffill()
    short_nav = short_nav.reindex(common_index).ffill()
    long_ret = long_nav.pct_change().fillna(0.0)
    short_ret = short_nav.pct_change().fillna(0.0)
    spread_ret = long_ret - short_ret
    spread_nav = (1 + spread_ret).cumprod()

    ic = _rank_ic(scored, prices, as_of_date, next_as_of_date)

    return {
        "period_of_report": pd.Timestamp(period_of_report),
        "as_of_date": pd.Timestamp(as_of_date),
        "next_as_of_date": pd.Timestamp(next_as_of_date),
        "spread_nav": spread_nav,
        "universe_nav": universe_nav,
        "long_tickers": long_tickers,
        "short_tickers": short_tickers,
        "turnover_long": leg_turnover(prev_long or set(), long_tickers),
        "turnover_short": leg_turnover(prev_short or set(), short_tickers),
        "ic": ic,
        "n_names": len(scored),
        "dropped": long_dropped | short_dropped | universe_dropped,
    }


def _rank_ic(scored: pd.DataFrame, prices: pd.DataFrame, as_of_date, next_as_of_date) -> float:
    tickers = set(scored["TICKER"])
    start_date = pd.Timestamp(as_of_date)
    end_date = pd.Timestamp(next_as_of_date)
    window = prices[
        prices["ticker"].isin(tickers) & (prices["date"] >= start_date) & (prices["date"] <= end_date)
    ]
    if window.empty:
        return float("nan")
    wide = window.pivot(index="date", columns="ticker", values="adj_close").sort_index().ffill()
    if wide.empty or len(wide) < 2:
        return float("nan")
    start_px, end_px = wide.iloc[0], wide.iloc[-1]
    name_ret = (end_px / start_px - 1).dropna()
    if len(name_ret) < 6:
        return float("nan")
    signal = scored.set_index("TICKER")["rank_signal"].reindex(name_ret.index)
    return float(signal.corr(name_ret, method="spearman"))


def performance_stats(nav: pd.Series, *, periods_per_year: int = 252) -> dict:
    """Summary stats from a daily NAV series. rf=0 for Sharpe -- no
    risk-free-rate series was sourced for this case study, disclosed as a
    simplification rather than silently assumed away."""
    nav = nav.dropna()
    keys = ["total_return", "annualized_return", "annualized_vol", "sharpe", "max_drawdown", "hit_rate", "n_days"]
    if len(nav) < 2:
        return {k: float("nan") for k in keys}
    rets = nav.pct_change().dropna()
    total_return = float(nav.iloc[-1] / nav.iloc[0] - 1)
    n_years = len(rets) / periods_per_year
    annualized_return = (1 + total_return) ** (1 / n_years) - 1 if n_years > 0 else float("nan")
    annualized_vol = float(rets.std(ddof=1) * (periods_per_year ** 0.5))
    sharpe = annualized_return / annualized_vol if annualized_vol else float("nan")
    running_max = nav.cummax()
    max_drawdown = float((nav / running_max - 1).min())
    hit_rate = float((rets > 0).mean())
    return {
        "total_return": total_return,
        "annualized_return": annualized_return,
        "annualized_vol": annualized_vol,
        "sharpe": sharpe,
        "max_drawdown": max_drawdown,
        "hit_rate": hit_rate,
        "n_days": len(nav),
    }


def run_backtest(
    panel: pd.DataFrame,
    mapping_df: pd.DataFrame,
    prices: pd.DataFrame,
    start_period,
    end_period,
    *,
    lag_days_grid: list[int] = LAG_DAYS_GRID,
    cost_bps_grid: list[float] = COST_BPS_GRID,
    n_quantiles: int = N_QUANTILES,
    passive_ciks: set[str] | None = None,
    history_path: Path = universe.DEFAULT_HISTORY_PATH,
    ticker_to_cusip: pd.Series | None = None,
    as_of_today=None,
) -> dict:
    """Full lag x cost sensitivity grid. Quarter-level work (factor
    scoring, decile assignment, price pulls into NAV) is done exactly once
    per lag value -- cost sensitivity is a cheap post-hoc adjustment on
    already-computed turnover, not a re-run of the whole pipeline.

    Returns {(lag_days, cost_bps): {"spread_nav": ..., "universe_nav": ...,
    "spy_nav": ..., "ic_series": ..., "quarters": [...]}}. Only closed
    quarters (next_as_of_date <= as_of_today) feed the stitched NAV curves
    and stats; a trailing open quarter, if any, is returned separately per
    lag under key ("open", lag_days) for disclosure, never blended into
    headline numbers.
    """
    as_of_today = pd.Timestamp(as_of_today) if as_of_today is not None else pd.Timestamp.today().normalize()
    results: dict = {}
    # formation_dates(..., end_period, ...) stops exactly at end_period, so
    # the loop below (which always uses schedule[idx+1] as the exit
    # boundary for schedule[idx]) would never actually enter end_period
    # itself as a trade -- it would only ever appear as someone else's exit
    # boundary. Extending by one extra quarter gives end_period a real exit
    # boundary to hold against; that extra quarter is never scored as its
    # own trade (the loop still stops at len(schedule)-1), only used as a
    # date marker -- correctly yielding a partial/open holding period for
    # end_period when that boundary is still in the future.
    extended_end = (pd.Period(pd.Timestamp(end_period), freq="Q") + 1).end_time.normalize()

    for lag_days in lag_days_grid:
        schedule = formation_dates(start_period, extended_end, lag_days)
        quarters = []
        prev_long, prev_short = set(), set()
        for idx in range(len(schedule) - 1):
            period, as_of = schedule[idx]
            _, next_as_of = schedule[idx + 1]
            q = quarter_pnl(
                panel, mapping_df, period, as_of, next_as_of, prices,
                n_quantiles=n_quantiles, prev_long=prev_long, prev_short=prev_short,
                passive_ciks=passive_ciks, history_path=history_path,
                ticker_to_cusip=ticker_to_cusip,
            )
            if q is None:
                continue
            q["is_open"] = next_as_of > as_of_today
            quarters.append(q)
            prev_long, prev_short = q["long_tickers"], q["short_tickers"]

        closed = [q for q in quarters if not q["is_open"]]
        open_quarters = [q for q in quarters if q["is_open"]]

        spy_nav, _ = leg_nav(
            prices, {BENCHMARK_TICKER},
            closed[0]["as_of_date"] if closed else (schedule[0][1] if schedule else pd.Timestamp.now()),
            closed[-1]["next_as_of_date"] if closed else pd.Timestamp.now(),
        )
        universe_nav = _stitch([q["universe_nav"] for q in closed])
        ic_series = pd.Series(
            {q["period_of_report"]: q["ic"] for q in closed if not pd.isna(q["ic"])}
        )

        for cost_bps in cost_bps_grid:
            adjusted = [
                apply_transaction_costs(q["spread_nav"], q["turnover_long"], q["turnover_short"], cost_bps)
                for q in closed
            ]
            spread_nav = _stitch(adjusted)
            results[(lag_days, cost_bps)] = {
                "spread_nav": spread_nav,
                "universe_nav": universe_nav,
                "spy_nav": spy_nav,
                "ic_series": ic_series,
                "quarters": closed,
                "open_quarters": open_quarters,
            }
    return results


def _stitch(quarter_navs: list[pd.Series]) -> pd.Series:
    """Chains a list of quarter-local NAV curves (each starting at 1.0)
    into one continuous curve, carrying the running level forward from one
    quarter into the next.

    Each quarter's leg_nav window is [as_of_date, next_as_of_date]
    inclusive on both ends, so consecutive quarters share their boundary
    date (quarter i's next_as_of_date == quarter i+1's as_of_date) --
    concatenating naively leaves that date duplicated in the index, and
    once transaction costs scale quarter i+1's curve down, the two
    duplicate rows hold genuinely different values (pre- vs.
    post-rebalance-cost), which would quietly inflate daily-return
    volatility at every single rebalance date. Keep the LAST occurrence at
    each duplicate date -- the post-rebalance value is the economically
    correct one (the cost is paid at the start of the new quarter)."""
    if not quarter_navs:
        return pd.Series(dtype=float)
    pieces = []
    running = 1.0
    for nav in quarter_navs:
        if nav.empty:
            continue
        scaled = nav * running
        pieces.append(scaled)
        running = float(scaled.iloc[-1])
    if not pieces:
        return pd.Series(dtype=float)
    combined = pd.concat(pieces)
    return combined[~combined.index.duplicated(keep="last")]


def detect_start_period(
    panel: pd.DataFrame,
    mapping_df: pd.DataFrame,
    *,
    candidate_start,
    end_period,
    lag_days: int = PRIMARY_LAG_DAYS,
    min_names: int = 300,
    passive_ciks: set[str] | None = None,
    history_path: Path = universe.DEFAULT_HISTORY_PATH,
    ticker_to_cusip: pd.Series | None = None,
):
    """First period_of_report with real, mature adjacent-quarter 13F
    coverage -- NOT simply panel["PERIODOFREPORT"].min(). The real panel's
    raw PERIODOFREPORT range goes back to 2006-12-31 (per Phase 1's
    validation run) purely from sparse late-amendment stragglers
    referencing old periods, years before SEC's structured-data archive
    (and this pipeline's earliest downloaded dataset, 2013q2) even begins.
    Using that raw minimum as a backtest start would run the factor across
    a long stretch of near-empty, degenerate early quarters.

    Scans quarters from candidate_start forward and returns the first one
    whose breadth_momentum() cross-section reaches min_names -- i.e., the
    first quarter with a real S&P-500-sized universe, not a straggler
    fragment. Prints nothing itself; callers (main()) should log the
    result so the choice stays auditable rather than a silent hardcode.
    """
    periods = pd.period_range(start=pd.Timestamp(candidate_start), end=pd.Timestamp(end_period), freq="Q")
    for p in periods:
        period_end = p.end_time.normalize()
        as_of = period_end + pd.Timedelta(days=lag_days)
        scored = factor.breadth_momentum(
            panel, period_end, as_of, mapping_df,
            passive_ciks=passive_ciks, history_path=history_path, ticker_to_cusip=ticker_to_cusip,
        )
        if len(scored) >= min_names:
            return period_end
    raise ValueError(
        f"No quarter between {candidate_start} and {end_period} reached "
        f"{min_names} scored names -- lower min_names or widen the candidate range."
    )


def prepare_panel(panel: pd.DataFrame) -> pd.DataFrame:
    """One-time prep of the raw panel before the lag x cost grid loop runs
    it through ~300+ point-in-time lookups. Two fixes, both purely
    performance -- neither changes what any lookup returns (see
    tests/test_backtest.py's equivalence checks against an unprepared
    panel):

    1. CIK/ACCESSION_NUMBER/CUSIP -> category dtype. Pandas 3's default
       string dtype is Arrow-backed; profiled on the real ~82M-row panel,
       `.iloc[positions]` on Arrow-backed string columns spent ~78% of its
       time inside pyarrow's take kernel. Category dtype makes a take a
       plain numpy int-code lookup instead, and is *more* memory-compact
       here, not less (bounded cardinality: far fewer distinct CIKs/
       CUSIPs/accession numbers than rows) -- measured peak RSS was flat
       to slightly lower after this change, not higher.
    2. `period_groups` fast-path index for `pit._period_slice` (see that
       function) -- built once here via a single pass over just the
       PERIODOFREPORT column, turning each lookup into an O(k) positional
       take instead of an O(n) full-panel scan.

    Combined effect, profiled on 8 real quarters: ~337s -> ~11s (~31x).
    """
    for col in ("CIK", "ACCESSION_NUMBER", "CUSIP"):
        panel[col] = panel[col].astype("category")
    panel.attrs["period_groups"] = panel.groupby("PERIODOFREPORT", sort=False).indices
    return panel


def main() -> None:
    import argparse
    import pickle

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--panel", default="data/processed/13f_panel_full.parquet")
    parser.add_argument("--start-period", default=None, help="First period_of_report quarter-end (YYYY-MM-DD); auto-detected from the panel if omitted")
    parser.add_argument("--end-period", default=None, help="Last period_of_report quarter-end (YYYY-MM-DD); defaults to the panel's latest PERIODOFREPORT")
    parser.add_argument("--out", default=None)
    parser.add_argument("--prices-cache", default=str(DEFAULT_PRICES_CACHE))
    parser.add_argument(
        "--quick", action="store_true",
        help=(
            "Dev-iteration mode: single (lag, cost) combination instead of "
            "the full sensitivity grid, and a short recent window instead "
            "of the full auto-detected history, unless overridden. Writes "
            f"to {DEFAULT_QUICK_OUT} by default, never the canonical "
            "--out path, and stamps meta['quick_mode'] -- a truncated "
            "quick run can never be mistaken for, or silently overwrite, "
            "the real deliverable output."
        ),
    )
    args = parser.parse_args()

    lag_days_grid = [PRIMARY_LAG_DAYS] if args.quick else LAG_DAYS_GRID
    cost_bps_grid = [PRIMARY_COST_BPS] if args.quick else COST_BPS_GRID
    if args.out is not None:
        out_arg = args.out
    else:
        out_arg = str(DEFAULT_QUICK_OUT) if args.quick else "data/processed/backtest_results.pkl"

    panel = pd.read_parquet(args.panel, columns=PANEL_COLUMNS)
    panel = prepare_panel(panel)
    mapping_df = mapping_mod.load_cusip_ticker_map()
    passive_ciks = universe.passive_manager_ciks()
    # Built once, up front, and threaded down through detect_start_period /
    # run_backtest / quarter_pnl / factor.breadth_momentum /
    # universe.sp500_universe_breadth into sp500_members() -- avoids
    # rebuilding this from `mapping_df` (unhashable, so it can't just be
    # functools.lru_cache'd) on every one of the ~300+ calls a full grid
    # makes. See universe.build_ticker_to_cusip's docstring.
    ticker_to_cusip = universe.build_ticker_to_cusip(mapping_df)

    end_period = args.end_period or str(panel["PERIODOFREPORT"].max().date())
    if args.start_period:
        start_period = args.start_period
    elif args.quick:
        # Short recent window for near-instant iteration -- never used to
        # produce the memo's actual numbers, so it doesn't need
        # detect_start_period's "real, mature coverage" guarantee.
        start_period = (pd.Period(pd.Timestamp(end_period), freq="Q") - 7).start_time.normalize()
        print(f"--quick: using short window starting {start_period.date()}")
    else:
        # NOT panel["PERIODOFREPORT"].min() -- see detect_start_period's
        # docstring for why that raw minimum is the wrong anchor.
        start_period = detect_start_period(
            panel, mapping_df, candidate_start="2013-01-01", end_period=end_period,
            passive_ciks=passive_ciks, ticker_to_cusip=ticker_to_cusip,
        )
        print(f"Auto-detected start_period: {start_period.date()} "
              f"(first quarter with >=300 scored names)")

    price_start = pd.Timestamp(start_period)
    # Extend to at least today: closed quarters only ever need data through
    # end_period + max(lag), but the still-open trailing quarter's partial
    # NAV (see run_backtest's extended_end) should show everything actually
    # available, not be truncated to a stale bound -- negligible extra cost
    # given years of history are already being fetched per ticker.
    price_end = max(
        pd.Timestamp(end_period) + pd.Timedelta(days=max(lag_days_grid) + 5),
        pd.Timestamp.today().normalize(),
    )
    tickers = price_universe(mapping_df, start_date=price_start, end_date=price_end) | {BENCHMARK_TICKER}
    print(f"{len(tickers):,} tickers in the price universe")
    prices = fetch_prices(tickers, price_start, price_end, cache_path=Path(args.prices_cache))
    print(f"{len(prices):,} price rows fetched/cached")

    results = run_backtest(
        panel, mapping_df, prices, start_period, end_period,
        passive_ciks=passive_ciks, ticker_to_cusip=ticker_to_cusip,
        lag_days_grid=lag_days_grid, cost_bps_grid=cost_bps_grid,
    )

    meta = {
        "panel": args.panel,
        "panel rows": f"{len(panel):,}",
        "window": f"{pd.Timestamp(start_period).date()} to {pd.Timestamp(end_period).date()}",
        "price universe (tickers)": f"{len(tickers):,}",
        "lag_days grid": lag_days_grid,
        "cost_bps grid": cost_bps_grid,
        "n_quantiles": N_QUANTILES,
        "generated": pd.Timestamp.today().normalize().date().isoformat(),
        "quick_mode": args.quick,
    }

    out_path = Path(out_arg)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "wb") as fh:
        pickle.dump({"results": results, "meta": meta}, fh)
    print(f"Backtest results ({len(results)} lag x cost combinations) -> {out_path}")


if __name__ == "__main__":
    main()
