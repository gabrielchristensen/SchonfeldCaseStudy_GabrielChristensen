import pandas as pd
import pytest

import src.detail as detail_mod
from src.detail import (
    benchmark_correlation,
    decile_returns,
    decile_summary,
    quarter_asset_detail,
    signal_diagnostics,
    subperiod_navs,
    subperiod_stats,
    subperiod_stats_grid,
)

AS_OF = pd.Timestamp("2020-06-01")
NEXT_AS_OF = pd.Timestamp("2020-09-01")


def _prices(rows):
    """rows: list of (date, ticker, adj_close)."""
    df = pd.DataFrame(rows, columns=["date", "ticker", "adj_close"])
    df["date"] = pd.to_datetime(df["date"])
    return df


def _quarter(long_tickers, short_tickers, spread_end):
    return {
        "period_of_report": pd.Timestamp("2020-03-31"),
        "as_of_date": AS_OF,
        "next_as_of_date": NEXT_AS_OF,
        "spread_nav": pd.Series([1.0, spread_end], index=[AS_OF, NEXT_AS_OF]),
        "long_tickers": set(long_tickers),
        "short_tickers": set(short_tickers),
    }


def _results(quarters, lag_days=60, cost_bps=10):
    return {(lag_days, cost_bps): {"quarters": quarters}}


# Long: TICKA 100->110 (+10%), TICKB 100->130 (+30%) -> mean +20%
# Short: TICKC 100->90 (-10%), TICKD 100->80 (-20%) -> mean -15%
# Implied spread return: 0.20 - (-0.15) = 0.35
_BASE_PRICES = _prices([
    (AS_OF, "TICKA", 100), (NEXT_AS_OF, "TICKA", 110),
    (AS_OF, "TICKB", 100), (NEXT_AS_OF, "TICKB", 130),
    (AS_OF, "TICKC", 100), (NEXT_AS_OF, "TICKC", 90),
    (AS_OF, "TICKD", 100), (NEXT_AS_OF, "TICKD", 80),
])


def test_quarter_asset_detail_computes_correct_returns_and_contributions():
    quarters = [_quarter(["TICKA", "TICKB"], ["TICKC", "TICKD"], spread_end=1.35)]
    result = quarter_asset_detail(_results(quarters), _BASE_PRICES, lag_days_grid=[60], cost_bps=10)

    by_ticker = result.set_index("ticker")
    assert by_ticker.loc["TICKA", "asset_return"] == pytest.approx(0.10)
    assert by_ticker.loc["TICKB", "asset_return"] == pytest.approx(0.30)
    assert by_ticker.loc["TICKC", "asset_return"] == pytest.approx(-0.10)
    assert by_ticker.loc["TICKD", "asset_return"] == pytest.approx(-0.20)

    long_rows = result[result["leg"] == "long"]
    short_rows = result[result["leg"] == "short"]
    assert long_rows["leg_weight"].tolist() == [0.5, 0.5]
    assert long_rows["contribution"].sum() == pytest.approx(0.20)
    assert short_rows["contribution"].sum() == pytest.approx(-0.15)
    assert not result["dropped"].any()


def test_quarter_asset_detail_flags_dropped_ticker_and_reweights_remaining():
    # TICKE has no price data at all -- dropped, long leg reweights over
    # TICKA/TICKB only (mean_long unaffected, still +20%).
    quarters = [_quarter(["TICKA", "TICKB", "TICKE"], ["TICKC", "TICKD"], spread_end=1.35)]
    result = quarter_asset_detail(_results(quarters), _BASE_PRICES, lag_days_grid=[60], cost_bps=10)

    dropped_row = result[result["ticker"] == "TICKE"].iloc[0]
    assert dropped_row["dropped"]
    assert pd.isna(dropped_row["asset_return"])
    assert dropped_row["leg_weight"] == 0.0
    assert dropped_row["contribution"] == 0.0

    long_rows = result[(result["leg"] == "long") & (~result["dropped"])]
    assert long_rows["leg_weight"].tolist() == [0.5, 0.5]
    assert long_rows["contribution"].sum() == pytest.approx(0.20)


def test_quarter_asset_detail_raises_when_leg_returns_drifts_from_leg_nav_return(monkeypatch):
    # Force _leg_returns to report a wrong end price for TICKB, simulating
    # its price-windowing having silently drifted from leg_nav's -- the
    # per-ticker-implied leg return then disagrees with leg_nav's actual
    # NAV return on the same tickers/prices, which must raise.
    real_leg_returns = detail_mod._leg_returns

    def _drifted(prices, tickers, start_date, end_date):
        start_px, end_px, dropped = real_leg_returns(prices, tickers, start_date, end_date)
        if "TICKB" in end_px.index:
            end_px = end_px.copy()
            end_px["TICKB"] = end_px["TICKB"] * 2  # deliberately wrong
        return start_px, end_px, dropped

    monkeypatch.setattr(detail_mod, "_leg_returns", _drifted)

    quarters = [_quarter(["TICKA", "TICKB"], ["TICKC", "TICKD"], spread_end=1.35)]
    with pytest.raises(ValueError, match="Reconciliation failed"):
        quarter_asset_detail(_results(quarters), _BASE_PRICES, lag_days_grid=[60], cost_bps=10)


def test_quarter_asset_detail_raises_when_dropped_set_disagrees_with_leg_nav(monkeypatch):
    real_leg_returns = detail_mod._leg_returns

    def _drifted(prices, tickers, start_date, end_date):
        start_px, end_px, dropped = real_leg_returns(prices, tickers, start_date, end_date)
        return start_px, end_px, dropped | {"TICKA"}  # falsely marks TICKA as dropped

    monkeypatch.setattr(detail_mod, "_leg_returns", _drifted)

    quarters = [_quarter(["TICKA", "TICKB"], ["TICKC", "TICKD"], spread_end=1.35)]
    with pytest.raises(ValueError, match="Reconciliation failed"):
        quarter_asset_detail(_results(quarters), _BASE_PRICES, lag_days_grid=[60], cost_bps=10)


def test_quarter_asset_detail_covers_every_lag_in_grid():
    quarters_60 = [_quarter(["TICKA"], ["TICKC"], spread_end=1.20)]
    quarters_90 = [_quarter(["TICKB"], ["TICKD"], spread_end=1.50)]
    results = {
        (60, 10): {"quarters": quarters_60},
        (90, 10): {"quarters": quarters_90},
    }
    result = quarter_asset_detail(results, _BASE_PRICES, lag_days_grid=[60, 90], cost_bps=10)
    assert set(result["lag_days"]) == {60, 90}
    assert set(result[result["lag_days"] == 60]["ticker"]) == {"TICKA", "TICKC"}
    assert set(result[result["lag_days"] == 90]["ticker"]) == {"TICKB", "TICKD"}


# --- subperiod_stats ----------------------------------------------------

_PERIODS = [
    "2020-03-31", "2020-06-30", "2020-09-30",
    "2020-12-31", "2021-03-31", "2021-06-30",
]
_RETURNS = [0.05, -0.02, 0.03, 0.01, -0.04, 0.06]
_ICS = [0.1, -0.05, 0.2, float("nan"), 0.0, 0.15]


def _full_quarters():
    periods = [pd.Timestamp(p) for p in _PERIODS]
    boundaries = periods + [periods[-1] + pd.Timedelta(days=90)]
    quarters = []
    for i, (r, ic) in enumerate(zip(_RETURNS, _ICS)):
        as_of, next_as_of = boundaries[i], boundaries[i + 1]
        quarters.append({
            "period_of_report": periods[i],
            "as_of_date": as_of,
            "next_as_of_date": next_as_of,
            "spread_nav": pd.Series([1.0, 1.0 + r], index=[as_of, next_as_of]),
            "turnover_long": 0.0,
            "turnover_short": 0.0,
            "ic": ic,
        })
    return quarters


def _compounded(returns):
    total = 1.0
    for r in returns:
        total *= 1.0 + r
    return total - 1.0


def test_subperiod_stats_splits_into_expected_chunk_boundaries_and_returns():
    results = {(60, 10): {"quarters": _full_quarters()}}

    two = subperiod_stats(results, lag_days=60, cost_bps=10, n_splits=2)
    assert len(two) == 2
    assert two.loc[0, "n_quarters"] == 3
    assert two.loc[0, "start_period"] == pd.Timestamp("2020-03-31")
    assert two.loc[0, "end_period"] == pd.Timestamp("2020-09-30")
    assert two.loc[0, "total_return"] == pytest.approx(_compounded(_RETURNS[0:3]))
    assert two.loc[1, "n_quarters"] == 3
    assert two.loc[1, "total_return"] == pytest.approx(_compounded(_RETURNS[3:6]))

    three = subperiod_stats(results, lag_days=60, cost_bps=10, n_splits=3)
    assert len(three) == 3
    assert three["n_quarters"].tolist() == [2, 2, 2]
    assert three.loc[0, "total_return"] == pytest.approx(_compounded(_RETURNS[0:2]))
    assert three.loc[2, "total_return"] == pytest.approx(_compounded(_RETURNS[4:6]))


def test_subperiod_stats_ic_aggregation_excludes_nan():
    results = {(60, 10): {"quarters": _full_quarters()}}
    two = subperiod_stats(results, lag_days=60, cost_bps=10, n_splits=2)

    # chunk 0: ic = [0.1, -0.05, 0.2] -> mean 0.25/3, 2/3 positive
    assert two.loc[0, "n_ic_quarters"] == 3
    assert two.loc[0, "mean_ic"] == pytest.approx(0.25 / 3)
    assert two.loc[0, "pct_positive_ic"] == pytest.approx(2 / 3)

    # chunk 1: ic = [nan, 0.0, 0.15] -> nan excluded, mean over [0.0, 0.15]
    assert two.loc[1, "n_ic_quarters"] == 2
    assert two.loc[1, "mean_ic"] == pytest.approx(0.075)
    assert two.loc[1, "pct_positive_ic"] == pytest.approx(0.5)


def test_subperiod_stats_grid_covers_every_lag_and_split_count():
    results = {
        (45, 10): {"quarters": _full_quarters()},
        (60, 10): {"quarters": _full_quarters()},
    }
    grid = subperiod_stats_grid(results, lag_days_grid=[45, 60], cost_bps=10, n_splits_list=(2, 3))
    assert set(zip(grid["lag_days"], grid["n_splits"])) == {(45, 2), (45, 3), (60, 2), (60, 3)}
    assert len(grid) == (2 + 3) * 2  # 2 lags x (2-split + 3-split) chunks


def test_subperiod_stats_empty_quarters_returns_empty_frame():
    results = {(60, 10): {"quarters": []}}
    assert subperiod_stats(results, lag_days=60, cost_bps=10, n_splits=2).empty


def test_subperiod_navs_matches_subperiod_stats_chunk_boundaries():
    results = {(60, 10): {"quarters": _full_quarters()}}
    navs = subperiod_navs(results, lag_days=60, cost_bps=10, n_splits=3)
    stats = subperiod_stats(results, lag_days=60, cost_bps=10, n_splits=3)

    assert len(navs) == len(stats) == 3
    for nav, (_, row) in zip(navs, stats.iterrows()):
        total_return = float(nav.iloc[-1] / nav.iloc[0] - 1)
        assert total_return == pytest.approx(row["total_return"])


# --- benchmark_correlation ------------------------------------------------

def test_benchmark_correlation_matches_hand_computed_beta_and_correlation():
    dates = pd.date_range("2020-01-01", periods=4, freq="D")
    # spy daily returns: [0.01, -0.02, 0.03]. spread = 2x spy (perfect
    # positive correlation, beta=2); universe = -1x spy (perfect negative
    # correlation, beta=-1 vs spy). Built via explicit cumulative products
    # so pct_change() reproduces exactly these returns.
    def _nav_from_returns(rets):
        nav = [1.0]
        for r in rets:
            nav.append(nav[-1] * (1 + r))
        return pd.Series(nav, index=dates)

    spy_rets = [0.01, -0.02, 0.03]
    spread_rets = [2 * r for r in spy_rets]
    universe_rets = [-1 * r for r in spy_rets]

    results = {
        (60, 10): {
            "spread_nav": _nav_from_returns(spread_rets),
            "universe_nav": _nav_from_returns(universe_rets),
            "spy_nav": _nav_from_returns(spy_rets),
        }
    }

    out = benchmark_correlation(results, lag_days=60, cost_bps=10, window=2)

    assert out["correlation"].loc["spread", "spy"] == pytest.approx(1.0)
    assert out["correlation"].loc["universe", "spy"] == pytest.approx(-1.0)
    assert out["beta_vs_spy"] == pytest.approx(2.0)
    assert out["beta_vs_universe"] == pytest.approx(-2.0)
    # Wherever the short rolling window is defined, the exact linear
    # relationship still holds perfectly.
    non_nan = out["rolling_corr_vs_spy"].dropna()
    assert len(non_nan) > 0
    assert (non_nan.round(6) == 1.0).all()


def test_benchmark_correlation_drops_dates_not_shared_across_all_three_series():
    dates = pd.date_range("2020-01-01", periods=3, freq="D")
    extra_date = pd.Timestamp("2020-01-04")
    results = {
        (60, 10): {
            "spread_nav": pd.Series([1.0, 1.01, 1.02], index=dates),
            "universe_nav": pd.Series([1.0, 1.005, 0.999], index=dates),
            # spy has one extra date the others don't -- must be excluded,
            # not ffilled or treated as a real observation.
            "spy_nav": pd.Series([1.0, 1.005, 1.01, 1.02], index=list(dates) + [extra_date]),
        }
    }
    out = benchmark_correlation(results, lag_days=60, cost_bps=10)
    # Only 2 shared return observations (from 3 shared NAV points) --
    # correlation is still computable and doesn't error on the mismatch.
    assert out["correlation"].shape == (3, 3)


# --- signal_diagnostics ----------------------------------------------------

_SD_PERIOD = "2020-03-31"
_SD_PRIOR_PERIOD = "2019-12-31"
_SD_PANEL_COLUMNS = [
    "ACCESSION_NUMBER", "FILING_DATE", "SUBMISSIONTYPE", "CIK",
    "PERIODOFREPORT", "CUSIP", "VALUE", "SSHPRNAMT",
]


def _sd_panel(rows):
    df = pd.DataFrame(rows, columns=_SD_PANEL_COLUMNS)
    df["FILING_DATE"] = pd.to_datetime(df["FILING_DATE"])
    df["PERIODOFREPORT"] = pd.to_datetime(df["PERIODOFREPORT"])
    return df


def test_signal_diagnostics_pearson_ic_matches_hand_computed_value(tmp_path):
    history_path = tmp_path / "sp500_history.csv"
    pd.DataFrame(
        [("2019-01-01", "TICKA,TICKB,TICKC")], columns=["date", "tickers"]
    ).to_csv(history_path, index=False)

    mapping_df = pd.DataFrame({
        "CUSIP": ["CUSIP001", "CUSIP002", "CUSIP003"],
        "TICKER": ["TICKA", "TICKB", "TICKC"],
        "NAME": ["A", "B", "C"],
    })

    # CUSIP001: current breadth 3, prior 1 -> raw_change=+2
    # CUSIP002: current breadth 2, prior 1 -> raw_change=+1
    # CUSIP003: current breadth 1, prior 1 -> raw_change=0
    # raw_change=[2,1,0] -> zscore=[1,0,-1] (mean=1, sample std=1)
    rows = [
        ("A1", "2020-05-10", "13F-HR", "CIK_A1", _SD_PERIOD, "CUSIP001", 100, 10),
        ("A2", "2020-05-10", "13F-HR", "CIK_A2", _SD_PERIOD, "CUSIP001", 100, 10),
        ("A3", "2020-05-10", "13F-HR", "CIK_A3", _SD_PERIOD, "CUSIP001", 100, 10),
        ("P1", "2020-02-01", "13F-HR", "CIK_A1", _SD_PRIOR_PERIOD, "CUSIP001", 100, 10),
        ("B1", "2020-05-10", "13F-HR", "CIK_B1", _SD_PERIOD, "CUSIP002", 100, 10),
        ("B2", "2020-05-10", "13F-HR", "CIK_B2", _SD_PERIOD, "CUSIP002", 100, 10),
        ("P2", "2020-02-01", "13F-HR", "CIK_B1", _SD_PRIOR_PERIOD, "CUSIP002", 100, 10),
        ("C1", "2020-05-10", "13F-HR", "CIK_C1", _SD_PERIOD, "CUSIP003", 100, 10),
        ("P3", "2020-02-01", "13F-HR", "CIK_C1", _SD_PRIOR_PERIOD, "CUSIP003", 100, 10),
    ]
    panel = _sd_panel(rows)

    as_of_date = pd.Timestamp("2020-06-01")
    next_as_of_date = pd.Timestamp("2020-09-01")
    # realized returns = 0.02 * zscore exactly -> Pearson correlation = 1.0
    prices = pd.DataFrame({
        "date": [as_of_date, next_as_of_date] * 3,
        "ticker": ["TICKA", "TICKA", "TICKB", "TICKB", "TICKC", "TICKC"],
        "adj_close": [100, 102, 100, 100, 100, 98],
    })

    results = {
        (60, 10): {
            "quarters": [{
                "period_of_report": pd.Timestamp(_SD_PERIOD),
                "as_of_date": as_of_date,
                "next_as_of_date": next_as_of_date,
                "ic": 0.42,
            }]
        }
    }

    out = signal_diagnostics(
        panel, mapping_df, prices, results, lag_days=60, cost_bps=10, history_path=history_path,
    )

    assert len(out) == 1
    row = out.iloc[0]
    assert row["n_names"] == 3
    assert row["spearman_ic"] == pytest.approx(0.42)
    assert row["pearson_ic_zscore"] == pytest.approx(1.0)
    assert row["zscore_signal_mean"] == pytest.approx(0.0, abs=1e-9)
    assert row["zscore_signal_std"] == pytest.approx(1.0)


def test_signal_diagnostics_skips_quarters_with_no_scored_names(tmp_path):
    history_path = tmp_path / "sp500_history.csv"
    pd.DataFrame([("2019-01-01", "TICKA")], columns=["date", "tickers"]).to_csv(history_path, index=False)
    mapping_df = pd.DataFrame({"CUSIP": ["CUSIP001"], "TICKER": ["TICKA"], "NAME": ["A"]})
    panel = _sd_panel([])  # empty panel -> nothing scores for any quarter

    results = {
        (60, 10): {
            "quarters": [{
                "period_of_report": pd.Timestamp(_SD_PERIOD),
                "as_of_date": pd.Timestamp("2020-06-01"),
                "next_as_of_date": pd.Timestamp("2020-09-01"),
                "ic": float("nan"),
            }]
        }
    }
    prices = pd.DataFrame(columns=["date", "ticker", "adj_close"])

    out = signal_diagnostics(
        panel, mapping_df, prices, results, lag_days=60, cost_bps=10, history_path=history_path,
    )
    assert out.empty


# --- decile_returns / decile_summary ----------------------------------------

def test_decile_returns_recovers_full_cross_section_not_just_traded_extremes(tmp_path):
    history_path = tmp_path / "sp500_history.csv"
    pd.DataFrame(
        [("2019-01-01", "TICKA,TICKB")], columns=["date", "tickers"]
    ).to_csv(history_path, index=False)
    mapping_df = pd.DataFrame({
        "CUSIP": ["CUSIP001", "CUSIP002"],
        "TICKER": ["TICKA", "TICKB"],
        "NAME": ["A", "B"],
    })
    # CUSIP001: breadth 3 -> 1, raw_change=+2 (higher rank -> decile 1).
    # CUSIP002: breadth 1 -> 1, raw_change=0 (lower rank -> decile 0).
    rows = [
        ("A1", "2020-05-10", "13F-HR", "CIK_A1", _SD_PERIOD, "CUSIP001", 100, 10),
        ("A2", "2020-05-10", "13F-HR", "CIK_A2", _SD_PERIOD, "CUSIP001", 100, 10),
        ("A3", "2020-05-10", "13F-HR", "CIK_A3", _SD_PERIOD, "CUSIP001", 100, 10),
        ("P1", "2020-02-01", "13F-HR", "CIK_A1", _SD_PRIOR_PERIOD, "CUSIP001", 100, 10),
        ("B1", "2020-05-10", "13F-HR", "CIK_B1", _SD_PERIOD, "CUSIP002", 100, 10),
        ("P2", "2020-02-01", "13F-HR", "CIK_B1", _SD_PRIOR_PERIOD, "CUSIP002", 100, 10),
    ]
    panel = _sd_panel(rows)
    as_of_date = pd.Timestamp("2020-06-01")
    next_as_of_date = pd.Timestamp("2020-09-01")
    prices = pd.DataFrame({
        "date": [as_of_date, next_as_of_date] * 2,
        "ticker": ["TICKA", "TICKA", "TICKB", "TICKB"],
        "adj_close": [100, 110, 100, 90],
    })
    results = {
        (60, 10): {
            "quarters": [{
                "period_of_report": pd.Timestamp(_SD_PERIOD),
                "as_of_date": as_of_date,
                "next_as_of_date": next_as_of_date,
                "ic": float("nan"),
            }]
        }
    }

    out = decile_returns(
        panel, mapping_df, prices, results, lag_days=60, cost_bps=10,
        n_quantiles=2, history_path=history_path,
    )

    # Both deciles recovered -- quarter_pnl's results dict never persists
    # anything about a middle/only-other decile, so this must come from
    # re-scoring, not from `results`.
    assert len(out) == 2
    lo = out[out["decile"] == 0].iloc[0]
    hi = out[out["decile"] == 1].iloc[0]
    assert lo["holding_period_return"] == pytest.approx(-0.10)  # TICKB, lower rank
    assert hi["holding_period_return"] == pytest.approx(0.10)   # TICKA, higher rank


def test_decile_returns_skips_quarters_with_no_scored_names(tmp_path):
    history_path = tmp_path / "sp500_history.csv"
    pd.DataFrame([("2019-01-01", "TICKA")], columns=["date", "tickers"]).to_csv(history_path, index=False)
    mapping_df = pd.DataFrame({"CUSIP": ["CUSIP001"], "TICKER": ["TICKA"], "NAME": ["A"]})
    panel = _sd_panel([])  # empty panel -> nothing scores for any quarter
    results = {
        (60, 10): {
            "quarters": [{
                "period_of_report": pd.Timestamp(_SD_PERIOD),
                "as_of_date": pd.Timestamp("2020-06-01"),
                "next_as_of_date": pd.Timestamp("2020-09-01"),
                "ic": float("nan"),
            }]
        }
    }
    prices = pd.DataFrame(columns=["date", "ticker", "adj_close"])

    out = decile_returns(
        panel, mapping_df, prices, results, lag_days=60, cost_bps=10, history_path=history_path,
    )
    assert out.empty


def test_decile_summary_annualizes_via_geometric_compounding():
    decile_df = pd.DataFrame([
        {"period_of_report": pd.Timestamp("2020-03-31"), "decile": 0, "n_names": 5, "n_dropped": 0, "holding_period_return": 0.05},
        {"period_of_report": pd.Timestamp("2020-06-30"), "decile": 0, "n_names": 5, "n_dropped": 0, "holding_period_return": 0.05},
    ])

    out = decile_summary(decile_df, n_quantiles=1)

    assert len(out) == 1
    row = out.iloc[0]
    assert row["decile"] == 0
    assert row["n_quarters"] == 2
    assert row["mean_return"] == pytest.approx(0.05)
    # Both quarters return exactly the same, so the geometric mean equals
    # the simple mean here -- annualized = 1.05**4 - 1.
    assert row["annualized_return"] == pytest.approx(1.05 ** 4 - 1)
    assert row["pct_positive"] == pytest.approx(1.0)


def test_decile_summary_empty_input_returns_empty_frame_with_expected_columns():
    out = decile_summary(pd.DataFrame())

    assert out.empty
    assert list(out.columns) == ["decile", "n_quarters", "mean_return", "annualized_return", "pct_positive"]
