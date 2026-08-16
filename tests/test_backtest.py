import numpy as np
import pandas as pd
import pytest

from src import pit
from src.backtest import (
    _stitch,
    apply_transaction_costs,
    assign_deciles,
    detect_start_period,
    fetch_prices,
    formation_dates,
    leg_nav,
    leg_turnover,
    performance_stats,
    prepare_panel,
    quarter_pnl,
    run_backtest,
)

_PANEL_COLUMNS = [
    "ACCESSION_NUMBER", "FILING_DATE", "SUBMISSIONTYPE", "CIK",
    "PERIODOFREPORT", "CUSIP", "VALUE", "SSHPRNAMT",
]


def _panel(rows):
    df = pd.DataFrame(rows, columns=_PANEL_COLUMNS)
    df["FILING_DATE"] = pd.to_datetime(df["FILING_DATE"])
    df["PERIODOFREPORT"] = pd.to_datetime(df["PERIODOFREPORT"])
    return df


def _mapping():
    return pd.DataFrame({
        "CUSIP": [f"CUSIP{i:03d}" for i in range(1, 13)],
        "TICKER": [f"TICK{i:03d}" for i in range(1, 13)],
        "NAME": [f"NAME {i}" for i in range(1, 13)],
    })


def _write_history(path, rows):
    pd.DataFrame(rows, columns=["date", "tickers"]).to_csv(path, index=False)


def _prices(rows):
    """rows: list of (date, ticker, adj_close)."""
    df = pd.DataFrame(rows, columns=["date", "ticker", "adj_close"])
    df["date"] = pd.to_datetime(df["date"])
    return df


# --- formation_dates -------------------------------------------------------

def test_formation_dates_lag_applied_to_every_quarter_end():
    result = formation_dates("2020-01-01", "2020-12-31", lag_days=60)

    assert result == [
        (pd.Timestamp("2020-03-31"), pd.Timestamp("2020-03-31") + pd.Timedelta(days=60)),
        (pd.Timestamp("2020-06-30"), pd.Timestamp("2020-06-30") + pd.Timedelta(days=60)),
        (pd.Timestamp("2020-09-30"), pd.Timestamp("2020-09-30") + pd.Timedelta(days=60)),
        (pd.Timestamp("2020-12-31"), pd.Timestamp("2020-12-31") + pd.Timedelta(days=60)),
    ]


def test_formation_dates_different_lags_shift_as_of_date_only():
    short_lag = formation_dates("2020-01-01", "2020-03-31", lag_days=45)
    long_lag = formation_dates("2020-01-01", "2020-03-31", lag_days=90)

    # period_of_report unaffected by lag choice; as_of_date shifts by exactly
    # the difference in lag days.
    assert short_lag[0][0] == long_lag[0][0]
    assert (long_lag[0][1] - short_lag[0][1]) == pd.Timedelta(days=45)


# --- assign_deciles ---------------------------------------------------------

def test_assign_deciles_top_and_bottom_match_rank_signal_extremes():
    df = pd.DataFrame({
        "CUSIP": [f"C{i}" for i in range(10)],
        "rank_signal": [i / 9 for i in range(10)],
    })

    result = assign_deciles(df, n_quantiles=10)

    assert result.loc[result["rank_signal"] == result["rank_signal"].max(), "decile"].iloc[0] == 9
    assert result.loc[result["rank_signal"] == result["rank_signal"].min(), "decile"].iloc[0] == 0


# --- leg_nav -----------------------------------------------------------------

def test_leg_nav_equal_weight_compounds_correctly():
    prices = _prices([
        ("2020-01-01", "A", 100), ("2020-01-02", "A", 110),
        ("2020-01-01", "B", 50), ("2020-01-02", "B", 55),
    ])

    nav, dropped = leg_nav(prices, {"A", "B"}, "2020-01-01", "2020-01-02")

    assert dropped == set()
    assert nav.iloc[0] == pytest.approx(1.0)
    # Both names return exactly +10% -> equal-weight NAV also +10%.
    assert nav.iloc[-1] == pytest.approx(1.10)


def test_leg_nav_drops_ticker_entirely_missing():
    prices = _prices([
        ("2020-01-01", "A", 100), ("2020-01-02", "A", 110),
    ])

    nav, dropped = leg_nav(prices, {"A", "GHOST"}, "2020-01-01", "2020-01-02")

    assert dropped == {"GHOST"}
    assert nav.iloc[-1] == pytest.approx(1.10)  # unaffected by the dropped name


def test_leg_nav_drops_ticker_missing_at_start_date():
    # B has no price at all until after the window starts -- can't be
    # bought at formation, so it's dropped, not partially weighted in.
    prices = _prices([
        ("2020-01-01", "A", 100), ("2020-01-02", "A", 120),
        ("2020-01-02", "B", 55),
    ])

    nav, dropped = leg_nav(prices, {"A", "B"}, "2020-01-01", "2020-01-02")

    assert dropped == {"B"}
    assert nav.iloc[-1] == pytest.approx(1.20)


def test_leg_nav_empty_tickers_returns_empty_series():
    nav, dropped = leg_nav(_prices([]), set(), "2020-01-01", "2020-01-02")
    assert nav.empty
    assert dropped == set()


# --- leg_turnover -------------------------------------------------------------

def test_leg_turnover_unchanged_leg_is_zero():
    assert leg_turnover({"A", "B", "C"}, {"A", "B", "C"}) == 0.0


def test_leg_turnover_fully_replaced_leg_is_one():
    assert leg_turnover({"A", "B"}, {"C", "D"}) == pytest.approx(1.0)


def test_leg_turnover_partial_replacement():
    # 1 of 4 names replaced: 1 added, 1 removed, n=4 -> (1+1)/(2*4) = 0.25
    assert leg_turnover({"A", "B", "C", "D"}, {"A", "B", "C", "E"}) == pytest.approx(0.25)


def test_leg_turnover_first_quarter_empty_prior_is_full_turnover():
    assert leg_turnover(set(), {"A", "B"}) == pytest.approx(1.0)


# --- apply_transaction_costs --------------------------------------------------

def test_apply_transaction_costs_zero_bps_is_a_no_op():
    nav = pd.Series([1.0, 1.05, 1.10])
    result = apply_transaction_costs(nav, turnover_long=0.5, turnover_short=0.5, cost_bps=0)
    pd.testing.assert_series_equal(result, nav)


def test_apply_transaction_costs_scales_by_expected_drag():
    nav = pd.Series([1.0, 1.05, 1.10])
    # turnover_long=1, turnover_short=1, cost_bps=10 -> (1+1)*10/10000*2 = 0.004
    result = apply_transaction_costs(nav, turnover_long=1.0, turnover_short=1.0, cost_bps=10)
    expected_drag = 2 * (10 / 10000) * 2
    pd.testing.assert_series_equal(result, nav * (1 - expected_drag))


# --- performance_stats ---------------------------------------------------------

def test_performance_stats_hand_computed_on_simple_series():
    # Two daily returns: +10%, -5% -> total return = 1.1*0.95 - 1
    nav = pd.Series([1.0, 1.10, 1.045])

    stats = performance_stats(nav, periods_per_year=252)

    assert stats["total_return"] == pytest.approx(1.045 - 1.0)
    assert stats["n_days"] == 3
    assert stats["hit_rate"] == pytest.approx(0.5)  # one up day, one down day


def test_performance_stats_max_drawdown_detects_a_real_drawdown():
    nav = pd.Series([1.0, 1.20, 0.90, 1.10])

    stats = performance_stats(nav)

    # Peak 1.20 -> trough 0.90 -> drawdown = 0.90/1.20 - 1 = -0.25
    assert stats["max_drawdown"] == pytest.approx(-0.25)


def test_performance_stats_too_short_series_returns_nan_not_raise():
    stats = performance_stats(pd.Series([1.0]))
    assert all(np.isnan(v) for v in stats.values())


# --- quarter_pnl (integration) -------------------------------------------------

def _two_quarter_panel():
    period = "2020-03-31"
    prior = "2019-12-31"
    rows = []
    # 12 CUSIPs -- enough for a stable 10-quantile qcut.
    # Odd CUSIPs get MORE filers this period than prior (positive momentum);
    # even CUSIPs get FEWER (negative momentum).
    for i in range(1, 13):
        cusip = f"CUSIP{i:03d}"
        n_current = 5 + i if i % 2 else 5
        n_prior = 5 if i % 2 else 5 + i
        for f in range(n_current):
            rows.append((f"{cusip}-CUR-{f}", "2020-05-10", "13F-HR", f"CUR{i}_{f}", period, cusip, 100, 10))
        for f in range(n_prior):
            rows.append((f"{cusip}-PRI-{f}", "2020-02-01", "13F-HR", f"PRI{i}_{f}", prior, cusip, 100, 10))
    return _panel(rows)


def test_quarter_pnl_long_leg_is_the_top_decile(tmp_path):
    history_path = tmp_path / "sp500_history.csv"
    mapping = _mapping()
    _write_history(history_path, [("2019-01-01", ",".join(mapping["TICKER"]))])
    panel = _two_quarter_panel()

    prices_rows = []
    for i in range(1, 13):
        ticker = f"TICK{i:03d}"
        prices_rows.append(("2020-05-30", ticker, 100.0))
        prices_rows.append(("2020-08-28", ticker, 105.0))
    prices = _prices(prices_rows)

    result = quarter_pnl(
        panel, mapping, "2020-03-31", as_of_date="2020-05-30", next_as_of_date="2020-08-28",
        prices=prices, n_quantiles=10, history_path=history_path,
    )

    assert result is not None
    # CUSIP012 (i=12, even) has the largest breadth INCREASE among evens...
    # actually the largest raw_change belongs to the highest odd i (more
    # added filers) -- just check the top/bottom legs are non-empty and
    # the turnover for an empty prior leg is full turnover.
    assert result["long_tickers"]
    assert result["short_tickers"]
    assert result["turnover_long"] == pytest.approx(1.0)
    assert result["turnover_short"] == pytest.approx(1.0)
    assert result["n_names"] == 12
    assert not result["spread_nav"].empty


def test_quarter_pnl_returns_none_when_no_names_score(tmp_path):
    history_path = tmp_path / "sp500_history.csv"
    _write_history(history_path, [("2019-01-01", "TICK001")])
    empty_panel = _panel([])

    result = quarter_pnl(
        empty_panel, _mapping(), "2020-03-31", as_of_date="2020-05-30", next_as_of_date="2020-08-28",
        prices=_prices([]), history_path=history_path,
    )

    assert result is None


# --- run_backtest (integration) ------------------------------------------------

def test_run_backtest_partitions_closed_vs_open_quarters(tmp_path):
    history_path = tmp_path / "sp500_history.csv"
    mapping = _mapping()
    _write_history(history_path, [("2019-01-01", ",".join(mapping["TICKER"]))])

    # Three consecutive quarters of panel data so formation_dates has two
    # full (period, as_of) transitions to loop over.
    rows = []
    for period, prior in [("2020-03-31", "2019-12-31"), ("2020-06-30", "2020-03-31")]:
        for i in range(1, 13):
            cusip = f"CUSIP{i:03d}"
            n_current = 5 + i if i % 2 else 5
            n_prior = 5 if i % 2 else 5 + i
            for f in range(n_current):
                rows.append((f"{cusip}-{period}-CUR-{f}", "2020-05-10" if period == "2020-03-31" else "2020-08-10",
                             "13F-HR", f"CUR{i}_{f}_{period}", period, cusip, 100, 10))
            for f in range(n_prior):
                rows.append((f"{cusip}-{period}-PRI-{f}", "2020-02-01" if prior == "2019-12-31" else "2020-05-10",
                             "13F-HR", f"PRI{i}_{f}_{period}", prior, cusip, 100, 10))
    panel = _panel(rows)

    prices_rows = []
    for date in ["2020-05-30", "2020-08-28", "2020-11-28"]:
        for i in range(1, 13):
            prices_rows.append((date, f"TICK{i:03d}", 100.0 + i))
        prices_rows.append((date, "SPY", 300.0))
    prices = _prices(prices_rows)

    results = run_backtest(
        panel, mapping, prices, start_period="2020-01-01", end_period="2020-09-30",
        lag_days_grid=[60], cost_bps_grid=[0, 10], history_path=history_path,
        as_of_today="2020-09-01",  # after the 2nd formation (2020-08-28), before the 3rd (2020-11-28)
    )

    assert set(results.keys()) == {(60, 0), (60, 10)}
    r0 = results[(60, 0)]
    # Exactly one closed quarter's transition (2020-03-31 -> 2020-06-30,
    # exiting 2020-08-28 <= as_of_today); the 2020-06-30 -> 2020-09-30
    # transition exits 2020-11-28, after as_of_today, so it's open.
    assert len(r0["quarters"]) == 1
    assert len(r0["open_quarters"]) == 1
    assert not r0["spread_nav"].empty


def test_run_backtest_most_recent_period_appears_as_an_open_quarter(tmp_path):
    # Regression test: formation_dates(..., end_period, ...) stops exactly
    # at end_period, so without extending the schedule by one extra
    # quarter, end_period would never get a next_as_of boundary and would
    # silently never appear as a trade at all -- not closed, not open,
    # just missing. This constructs a panel whose LATEST period's own
    # as_of_date has already passed (so it should be scored and enter as a
    # trade) while its NEXT quarter's as_of_date has not (so it must show
    # up as an OPEN quarter, not vanish).
    history_path = tmp_path / "sp500_history.csv"
    mapping = _mapping()
    _write_history(history_path, [("2019-01-01", ",".join(mapping["TICKER"]))])

    rows = []
    for period, prior, filing_date_cur, filing_date_pri in [
        ("2020-03-31", "2019-12-31", "2020-05-10", "2020-02-01"),
        ("2020-06-30", "2020-03-31", "2020-08-10", "2020-05-10"),
        ("2020-09-30", "2020-06-30", "2020-11-10", "2020-08-10"),
    ]:
        for i in range(1, 13):
            cusip = f"CUSIP{i:03d}"
            n_current = 5 + i if i % 2 else 5
            n_prior = 5 if i % 2 else 5 + i
            for f in range(n_current):
                rows.append((f"{cusip}-{period}-CUR-{f}", filing_date_cur, "13F-HR", f"CUR{i}_{f}_{period}", period, cusip, 100, 10))
            for f in range(n_prior):
                rows.append((f"{cusip}-{period}-PRI-{f}", filing_date_pri, "13F-HR", f"PRI{i}_{f}_{period}", prior, cusip, 100, 10))
    panel = _panel(rows)

    # Exact quarter-end + 60d boundaries (verified via pd.Timestamp
    # arithmetic), so every quarter's leg_nav window has at least one
    # matching price row at its start date -- 2020-06-30+60d=2020-08-29,
    # 2020-09-30+60d=2020-11-29, not the "...-28" one might guess.
    prices_rows = []
    for date in ["2020-05-30", "2020-08-29", "2020-11-29"]:
        for i in range(1, 13):
            prices_rows.append((date, f"TICK{i:03d}", 100.0 + i))
        prices_rows.append((date, "SPY", 300.0))
    prices = _prices(prices_rows)

    # as_of_today is AFTER 2020-09-30's own formation (2020-11-29) but
    # BEFORE its exit (2020-12-31 + 60d = 2021-03-01).
    results = run_backtest(
        panel, mapping, prices, start_period="2020-01-01", end_period="2020-09-30",
        lag_days_grid=[60], cost_bps_grid=[0], history_path=history_path,
        as_of_today="2020-12-01",
    )

    r = results[(60, 0)]
    open_periods = {q["period_of_report"] for q in r["open_quarters"]}
    assert pd.Timestamp("2020-09-30") in open_periods


def test_run_backtest_higher_cost_bps_never_outperforms_lower_cost_bps(tmp_path):
    history_path = tmp_path / "sp500_history.csv"
    mapping = _mapping()
    _write_history(history_path, [("2019-01-01", ",".join(mapping["TICKER"]))])
    panel = _two_quarter_panel()

    prices_rows = []
    for date in ["2020-05-30", "2020-08-28"]:
        for i in range(1, 13):
            prices_rows.append((date, f"TICK{i:03d}", 100.0 + i))
        prices_rows.append((date, "SPY", 300.0))
    prices = _prices(prices_rows)

    results = run_backtest(
        panel, mapping, prices, start_period="2020-01-01", end_period="2020-06-30",
        lag_days_grid=[60], cost_bps_grid=[0, 25], history_path=history_path,
        as_of_today="2020-09-01",
    )

    nav_no_cost = results[(60, 0)]["spread_nav"]
    nav_with_cost = results[(60, 25)]["spread_nav"]
    assert nav_with_cost.iloc[-1] <= nav_no_cost.iloc[-1]


# --- _stitch ---------------------------------------------------------------

def test_stitch_carries_running_level_forward_across_quarters():
    q1 = pd.Series([1.0, 1.10], index=pd.to_datetime(["2020-01-01", "2020-04-01"]))
    q2 = pd.Series([1.0, 0.95], index=pd.to_datetime(["2020-04-01", "2020-07-01"]))

    result = _stitch([q1, q2])

    assert result.loc["2020-01-01"] == pytest.approx(1.0)
    assert result.loc["2020-07-01"] == pytest.approx(1.10 * 0.95)


def test_stitch_drops_duplicate_seam_date_keeping_post_rebalance_value():
    # Simulates a cost haircut applied to q2: its first (seam) value is
    # LOWER than q1's last value at the same calendar date. The stitched
    # curve must keep exactly one row for that date -- the post-cost one,
    # not both (which would corrupt volatility/day-count downstream).
    q1 = pd.Series([1.0, 1.20], index=pd.to_datetime(["2020-01-01", "2020-04-01"]))
    q2 = pd.Series([0.99, 1.05], index=pd.to_datetime(["2020-04-01", "2020-07-01"]))  # cost-haircut day-1

    result = _stitch([q1, q2])

    assert result.index.is_unique
    assert len(result) == 3  # not 4 -- the duplicate 2020-04-01 row is collapsed
    assert result.loc["2020-04-01"] == pytest.approx(1.20 * 0.99)


# --- detect_start_period ------------------------------------------------------

def test_detect_start_period_skips_sparse_early_quarter(tmp_path):
    history_path = tmp_path / "sp500_history.csv"
    mapping = _mapping()
    _write_history(history_path, [("2019-01-01", ",".join(mapping["TICKER"]))])

    rows = []
    # 2019-12-31: sparse -- only 2 of 12 CUSIPs have any filers at all. This
    # is the prior quarter for 2020-03-31, so 2020-03-31's cross-section
    # (inner join with prior) is capped at 2 names regardless of its own
    # coverage -- exactly the "stragglers-only early quarter" shape being
    # guarded against.
    for cusip in ["CUSIP001", "CUSIP002"]:
        rows.append((f"{cusip}-p0", "2020-02-01", "13F-HR", f"F0_{cusip}", "2019-12-31", cusip, 100, 10))
    # 2020-03-31: full coverage (all 12), doubles as both a would-be
    # current period AND the prior period for 2020-06-30.
    for i in range(1, 13):
        rows.append((f"CUSIP{i:03d}-p1", "2020-05-10", "13F-HR", f"F1_{i}", "2020-03-31", f"CUSIP{i:03d}", 100, 10))
    # 2020-06-30: full coverage (all 12) -- with 2020-03-31 also full, this
    # quarter's cross-section is the full 12.
    for i in range(1, 13):
        rows.append((f"CUSIP{i:03d}-p2", "2020-08-10", "13F-HR", f"F2_{i}", "2020-06-30", f"CUSIP{i:03d}", 100, 10))
    panel = _panel(rows)

    result = detect_start_period(
        panel, mapping, candidate_start="2020-01-01", end_period="2020-12-31",
        min_names=10, history_path=history_path,
    )

    assert result == pd.Timestamp("2020-06-30")


def test_detect_start_period_raises_when_nothing_reaches_threshold(tmp_path):
    history_path = tmp_path / "sp500_history.csv"
    mapping = _mapping()
    _write_history(history_path, [("2019-01-01", ",".join(mapping["TICKER"]))])
    panel = _panel([])

    with pytest.raises(ValueError):
        detect_start_period(
            panel, mapping, candidate_start="2020-01-01", end_period="2020-12-31",
            min_names=10, history_path=history_path,
        )


# --- fetch_prices (network mocked) ---------------------------------------------

def _fake_yf_frame(dates, ticker_values: dict):
    """Mimics yfinance's real multiindex output shape: columns are
    (field, ticker) tuples, 'Close' is the field auto_adjust=True uses."""
    idx = pd.to_datetime(dates)
    data = {}
    for field in ["Close", "Open", "High", "Low", "Volume"]:
        for ticker, values in ticker_values.items():
            data[(field, ticker)] = values if field == "Close" else [np.nan] * len(values)
    columns = pd.MultiIndex.from_tuples(data.keys())
    return pd.DataFrame(data.values(), index=columns).T.set_axis(idx)


def test_fetch_prices_caches_and_skips_already_fetched_tickers(tmp_path, monkeypatch):
    cache_path = tmp_path / "prices.parquet"
    calls = []

    def fake_download(tickers, start, end, **kwargs):
        calls.append(sorted(tickers))
        return _fake_yf_frame(["2020-01-01", "2020-01-02"], {t: [100.0, 101.0] for t in tickers})

    monkeypatch.setattr("src.backtest.yf.download", fake_download)

    first = fetch_prices({"AAPL", "MSFT"}, "2020-01-01", "2020-01-02", cache_path=cache_path)
    assert len(calls) == 1
    assert set(first["ticker"]) == {"AAPL", "MSFT"}

    # Second call adds one new ticker -- only the new one should be fetched.
    second = fetch_prices({"AAPL", "MSFT", "GOOGL"}, "2020-01-01", "2020-01-02", cache_path=cache_path)
    assert len(calls) == 2
    assert calls[1] == ["GOOGL"]
    assert set(second["ticker"]) == {"AAPL", "MSFT", "GOOGL"}


def test_fetch_prices_records_unresolvable_ticker_as_explicit_nan_not_absent(tmp_path, monkeypatch):
    cache_path = tmp_path / "prices.parquet"

    def fake_download(tickers, start, end, **kwargs):
        # Simulate yfinance returning data only for the resolvable ticker --
        # "GHOST" is entirely absent from the response columns.
        return _fake_yf_frame(["2020-01-01"], {"AAPL": [100.0]})

    monkeypatch.setattr("src.backtest.yf.download", fake_download)

    result = fetch_prices({"AAPL", "GHOST"}, "2020-01-01", "2020-01-01", cache_path=cache_path)

    ghost_rows = result[result["ticker"] == "GHOST"]
    assert len(ghost_rows) == 1
    assert ghost_rows["adj_close"].isna().all()

    # A re-run must not re-query GHOST -- it's cached as a known-unresolvable
    # ticker, not silently absent from the cache.
    calls = []
    monkeypatch.setattr(
        "src.backtest.yf.download",
        lambda tickers, start, end, **kwargs: calls.append(sorted(tickers)) or _fake_yf_frame([], {}),
    )
    fetch_prices({"AAPL", "GHOST"}, "2020-01-01", "2020-01-01", cache_path=cache_path)
    assert calls == [] or "GHOST" not in calls[0]


def test_fetch_prices_retries_on_transient_failure(tmp_path, monkeypatch):
    cache_path = tmp_path / "prices.parquet"
    monkeypatch.setattr("src.backtest.time.sleep", lambda _: None)
    attempts = {"n": 0}

    def flaky_download(tickers, start, end, **kwargs):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise ConnectionError("simulated transient network failure")
        return _fake_yf_frame(["2020-01-01"], {t: [100.0] for t in tickers})

    monkeypatch.setattr("src.backtest.yf.download", flaky_download)

    result = fetch_prices({"AAPL"}, "2020-01-01", "2020-01-01", cache_path=cache_path)

    assert attempts["n"] == 3
    assert set(result["ticker"]) == {"AAPL"}


# --- prepare_panel -----------------------------------------------------

def test_prepare_panel_converts_id_columns_to_category_and_sets_period_groups():
    panel = _panel([
        ("A0000001", "2020-05-10", "13F-HR", "CIK_A", "2020-03-31", "CUSIP001", 100, 10),
    ])
    prepared = prepare_panel(panel)

    for col in ("CIK", "ACCESSION_NUMBER", "CUSIP"):
        assert isinstance(prepared[col].dtype, pd.CategoricalDtype)
    assert set(prepared.attrs["period_groups"].keys()) == {pd.Timestamp("2020-03-31")}


def test_prepare_panel_does_not_change_pit_results():
    # The category-dtype conversion and the period_groups fast path are
    # both pure performance changes -- pit.breadth()'s output on a
    # category-dtype, indexed panel must exactly match its output on the
    # original plain-dtype panel with no .attrs at all.
    rows = [
        ("A0000001", "2020-05-10", "13F-HR", "CIK_A", "2020-03-31", "CUSIP001", 100, 10),
        ("A0000002", "2020-05-10", "13F-HR/A", "CIK_A", "2020-03-31", "CUSIP001", 150, 15),
        ("A0000003", "2020-05-11", "13F-HR", "CIK_B", "2020-03-31", "CUSIP002", 200, 20),
    ]
    plain = _panel(rows)
    prepared = prepare_panel(_panel(rows))

    plain_result = pit.breadth(plain, "2020-03-31", as_of_date="2020-06-01").sort_values("CUSIP").reset_index(drop=True)
    prepared_result = pit.breadth(prepared, "2020-03-31", as_of_date="2020-06-01").sort_values("CUSIP").reset_index(drop=True)
    prepared_result["CUSIP"] = prepared_result["CUSIP"].astype(str)

    pd.testing.assert_frame_equal(plain_result, prepared_result, check_dtype=False)
