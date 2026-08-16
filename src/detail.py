"""Per-quarter, per-asset detail derived from an already-run backtest.

`backtest_results.pkl` only persists each quarter's aggregate,
equal-weight leg NAV curve -- not each held ticker's individual return.
This module reconstructs that detail directly from already-committed
artifacts (`backtest_results.pkl` + `prices.parquet`), with no re-run of
the expensive full backtest and no changes to `backtest.py`'s tested
core (`quarter_pnl`/`leg_nav`/`run_backtest` are untouched).

`_leg_returns` intentionally re-implements `backtest.leg_nav`'s exact
price-windowing convention (ffill, drop a ticker missing at the window's
first available row) rather than calling it, since `leg_nav` only ever
returns the aggregate NAV curve, not per-ticker prices. That duplication
is a real drift risk -- if either implementation changes later without
the other, they could silently disagree. `quarter_asset_detail` guards
against exactly that, reconciled at the PER-LEG level against
`backtest.leg_nav` itself (called directly, as ground truth) -- NOT
against the pickled `spread_nav`. `spread_nav` is a daily-rebalanced
combination (`spread_ret = long_ret - short_ret` at each day, then
`cumprod`), so its total return over a multi-day quarter is a
path-dependent function of the two legs' daily paths, not simply
`long_total_return - short_total_return` (that identity only holds for a
single time-step). What IS an exact identity, for a leg that is
equal-weight and never rebalanced intra-quarter:
`leg_nav[-1]/leg_nav[0] - 1 == mean(per-ticker total returns among
usable names)`, since `nav[t] = mean_i(price_i[t]/price_i[0])` at every
t by construction. Every quarter's per-leg detail is checked against
this identity, using `leg_nav`'s own dropped-set and NAV as ground
truth, and the function raises if either the dropped-set or the implied
total return doesn't tie out -- a real bug guard, not a formality (an
earlier, incorrect version of this check compared against `spread_nav`
directly and caught a real discrepancy on the very first real quarter,
which is what surfaced this distinction).
"""

from pathlib import Path

import numpy as np
import pandas as pd

from src.backtest import LAG_DAYS_GRID, PRIMARY_COST_BPS
from src.backtest import _stitch, apply_transaction_costs, performance_stats
from src.backtest import leg_nav as _tested_leg_nav

_RECONCILE_ATOL = 1e-9


def _leg_returns(
    prices: pd.DataFrame, tickers: set[str], start_date, end_date
) -> tuple[pd.Series, pd.Series, set[str]]:
    """Per-ticker (start_price, end_price), Series indexed by ticker, for
    every ticker still usable after the same drop rule `backtest.leg_nav`
    applies (missing entirely, or missing at the window's first available
    row, over [start_date, end_date]). Returns (start_px, end_px, dropped).
    """
    start_date = pd.Timestamp(start_date)
    end_date = pd.Timestamp(end_date)
    if not tickers:
        return pd.Series(dtype=float), pd.Series(dtype=float), set()

    window = prices[
        prices["ticker"].isin(tickers) & (prices["date"] >= start_date) & (prices["date"] <= end_date)
    ]
    if window.empty:
        return pd.Series(dtype=float), pd.Series(dtype=float), set(tickers)
    wide = window.pivot(index="date", columns="ticker", values="adj_close").sort_index()
    px = wide.ffill()

    dropped = set(tickers) - set(px.columns)
    if px.empty:
        return pd.Series(dtype=float), pd.Series(dtype=float), set(tickers)
    first_row = px.iloc[0]
    valid_at_start = set(first_row[first_row.notna()].index)
    dropped |= set(px.columns) - valid_at_start
    usable = sorted(valid_at_start)
    if not usable:
        return pd.Series(dtype=float), pd.Series(dtype=float), set(tickers)

    return px[usable].iloc[0], px[usable].iloc[-1], dropped


def quarter_asset_detail(
    results: dict,
    prices: pd.DataFrame,
    *,
    lag_days_grid: list[int] = LAG_DAYS_GRID,
    cost_bps: float = PRIMARY_COST_BPS,
) -> pd.DataFrame:
    """One row per (lag_days, period_of_report, ticker, leg) for every
    CLOSED quarter's long/short baskets. `cost_bps` only selects which
    lag's `quarters` list to read -- turnover/cost is a post-hoc haircut
    on the aggregate NAV and doesn't affect basket membership or
    individual returns, so every cost_bps for a given lag shares the same
    underlying quarters.

    A ticker dropped (no usable price at the window's start) gets
    `dropped=True`, `asset_return=NaN`, `leg_weight=0` -- matches the
    project's disclosed "drop, don't impute" convention, made explicit
    here rather than silently omitted from the export.

    Raises ValueError, per leg per quarter, if either (a) `_leg_returns`'
    dropped-ticker set disagrees with `backtest.leg_nav`'s own, or (b) the
    per-ticker-implied leg return doesn't match `leg_nav`'s actual NAV
    return to within floating-point tolerance -- see module docstring for
    why this is checked per-leg against `leg_nav` directly, not against
    the combined `spread_nav`.
    """
    rows = []
    for lag_days in lag_days_grid:
        quarters = results[(lag_days, cost_bps)]["quarters"]
        for q in quarters:
            as_of_date, next_as_of_date = q["as_of_date"], q["next_as_of_date"]
            for leg, tickers in (("long", q["long_tickers"]), ("short", q["short_tickers"])):
                start_px, end_px, dropped = _leg_returns(prices, tickers, as_of_date, next_as_of_date)
                nav, legnav_dropped = _tested_leg_nav(prices, tickers, as_of_date, next_as_of_date)

                if dropped != legnav_dropped:
                    raise ValueError(
                        f"Reconciliation failed for lag_days={lag_days}, "
                        f"period_of_report={q['period_of_report'].date()}, leg={leg}: "
                        f"_leg_returns dropped {dropped} but leg_nav dropped {legnav_dropped}. "
                        "_leg_returns' price-windowing has likely drifted from backtest.leg_nav's."
                    )

                asset_return = end_px / start_px - 1
                n_usable = len(asset_return)
                implied_leg_return = float(asset_return.mean()) if n_usable else float("nan")
                if not nav.empty:
                    actual_leg_return = float(nav.iloc[-1] / nav.iloc[0] - 1)
                    if not np.isclose(implied_leg_return, actual_leg_return, atol=_RECONCILE_ATOL):
                        raise ValueError(
                            f"Reconciliation failed for lag_days={lag_days}, "
                            f"period_of_report={q['period_of_report'].date()}, leg={leg}: "
                            f"per-ticker-implied return {implied_leg_return:.10f} != leg_nav's actual "
                            f"return {actual_leg_return:.10f} "
                            f"(diff={implied_leg_return - actual_leg_return:.2e}). "
                            "_leg_returns' price-windowing has likely drifted from backtest.leg_nav's."
                        )

                weight = 1.0 / n_usable if n_usable else 0.0
                for ticker in sorted(tickers):
                    if ticker in dropped:
                        rows.append({
                            "lag_days": lag_days, "period_of_report": q["period_of_report"],
                            "as_of_date": as_of_date, "next_as_of_date": next_as_of_date,
                            "ticker": ticker, "leg": leg,
                            "start_price": float("nan"), "end_price": float("nan"),
                            "asset_return": float("nan"), "dropped": True,
                            "leg_weight": 0.0, "contribution": 0.0,
                        })
                    else:
                        r = float(asset_return[ticker])
                        rows.append({
                            "lag_days": lag_days, "period_of_report": q["period_of_report"],
                            "as_of_date": as_of_date, "next_as_of_date": next_as_of_date,
                            "ticker": ticker, "leg": leg,
                            "start_price": float(start_px[ticker]), "end_price": float(end_px[ticker]),
                            "asset_return": r, "dropped": False,
                            "leg_weight": weight, "contribution": weight * r,
                        })
    return pd.DataFrame(rows)


def _split_into_chunks(quarters: list[dict], n_splits: int) -> list[list[dict]]:
    """Splits an already time-ordered `quarters` list into `n_splits`
    contiguous, roughly-equal-count chunks (`np.array_split` on the
    index range), dropping any empty chunk (possible when `n_splits`
    exceeds the number of quarters). Shared by `subperiod_stats` and
    `subperiod_navs` so both always agree on where a regime boundary
    falls."""
    n = len(quarters)
    if n == 0:
        return []
    boundaries = np.array_split(np.arange(n), n_splits)
    return [[quarters[i] for i in idx] for idx in boundaries if len(idx) > 0]


def subperiod_navs(
    results: dict, lag_days: int, *, cost_bps: float = PRIMARY_COST_BPS, n_splits: int = 2,
) -> list[pd.Series]:
    """The cost-adjusted, stitched spread NAV curve for each sub-period
    chunk (same chunk boundaries as `subperiod_stats`) -- for plotting
    real equity curves per regime, not just summary stats. Reuses
    `backtest.apply_transaction_costs`/`_stitch` directly, same as
    `subperiod_stats`."""
    quarters = results[(lag_days, cost_bps)]["quarters"]
    navs = []
    for chunk in _split_into_chunks(quarters, n_splits):
        adjusted = [
            apply_transaction_costs(q["spread_nav"], q["turnover_long"], q["turnover_short"], cost_bps)
            for q in chunk
        ]
        navs.append(_stitch(adjusted))
    return navs


def subperiod_stats(
    results: dict, lag_days: int, *, cost_bps: float = PRIMARY_COST_BPS, n_splits: int = 2,
) -> pd.DataFrame:
    """Splits (lag_days, cost_bps)'s CLOSED `quarters` list (already
    time-ordered by `run_backtest`) into `n_splits` contiguous,
    roughly-equal-count chunks (`_split_into_chunks`), and computes the
    same performance stats and IC aggregation `run_backtest`/`report.py`
    compute for the full window, per chunk -- reuses
    `backtest.apply_transaction_costs`, `backtest._stitch`, and
    `backtest.performance_stats` directly (no new stats logic here, only
    a slice of the same tested pipeline), and matches `run_backtest`'s
    own IC-series convention of dropping NaN `ic` values rather than
    letting them corrupt mean/std.

    One row per chunk: `lag_days, n_splits, chunk, start_period,
    end_period, n_quarters`, all of `performance_stats`' keys, plus
    `mean_ic, ic_std, pct_positive_ic, n_ic_quarters`.
    """
    quarters = results[(lag_days, cost_bps)]["quarters"]
    rows = []
    for chunk_idx, chunk in enumerate(_split_into_chunks(quarters, n_splits)):
        adjusted = [
            apply_transaction_costs(q["spread_nav"], q["turnover_long"], q["turnover_short"], cost_bps)
            for q in chunk
        ]
        stats = performance_stats(_stitch(adjusted))
        ic_values = pd.Series([q["ic"] for q in chunk if not pd.isna(q["ic"])])
        rows.append({
            "lag_days": lag_days,
            "n_splits": n_splits,
            "chunk": chunk_idx,
            "start_period": chunk[0]["period_of_report"],
            "end_period": chunk[-1]["period_of_report"],
            "n_quarters": len(chunk),
            **stats,
            "mean_ic": float(ic_values.mean()) if len(ic_values) else float("nan"),
            "ic_std": float(ic_values.std()) if len(ic_values) else float("nan"),
            "pct_positive_ic": float((ic_values > 0).mean()) if len(ic_values) else float("nan"),
            "n_ic_quarters": len(ic_values),
        })
    return pd.DataFrame(rows)


def subperiod_stats_grid(
    results: dict,
    *,
    lag_days_grid: list[int] = LAG_DAYS_GRID,
    cost_bps: float = PRIMARY_COST_BPS,
    n_splits_list: tuple[int, ...] = (2, 3),
) -> pd.DataFrame:
    """subperiod_stats(), concatenated across every lag in `lag_days_grid`
    and every split count in `n_splits_list` -- lets a caller (the regime
    notebook) get every granularity/lag combination in one call."""
    frames = [
        subperiod_stats(results, lag_days, cost_bps=cost_bps, n_splits=n_splits)
        for lag_days in lag_days_grid
        for n_splits in n_splits_list
    ]
    frames = [f for f in frames if not f.empty]
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def main() -> None:
    import argparse
    import pickle

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", default="data/processed/backtest_results.pkl")
    parser.add_argument("--prices", default="data/processed/prices.parquet")
    parser.add_argument("--out", default="data/processed/quarter_asset_detail.csv")
    parser.add_argument("--subperiods-out", default="data/processed/subperiod_stats.csv")
    args = parser.parse_args()

    with open(args.results, "rb") as fh:
        payload = pickle.load(fh)
    results = payload["results"] if isinstance(payload, dict) and "results" in payload else payload
    prices = pd.read_parquet(args.prices)

    detail = quarter_asset_detail(results, prices)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    detail.to_csv(out_path, index=False)
    print(f"{len(detail):,} rows -> {out_path}")

    subperiods = subperiod_stats_grid(results)
    subperiods_out_path = Path(args.subperiods_out)
    subperiods_out_path.parent.mkdir(parents=True, exist_ok=True)
    subperiods.to_csv(subperiods_out_path, index=False)
    print(f"{len(subperiods):,} rows -> {subperiods_out_path}")


if __name__ == "__main__":
    main()
