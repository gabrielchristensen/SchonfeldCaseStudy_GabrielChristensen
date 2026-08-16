import re

import pandas as pd

from src.backtest import COST_BPS_GRID, LAG_DAYS_GRID, PRIMARY_COST_BPS, PRIMARY_LAG_DAYS
from src.report import _stats_table, build_report


def _nav(values):
    idx = pd.date_range("2020-01-01", periods=len(values), freq="D")
    return pd.Series(values, index=idx)


def _fake_results():
    results = {}
    quarters = [
        {
            "period_of_report": pd.Timestamp("2020-03-31"),
            "as_of_date": pd.Timestamp("2020-05-30"),
            "next_as_of_date": pd.Timestamp("2020-08-28"),
            "spread_nav": _nav([1.0, 1.01, 1.02, 1.015]),
            "turnover_long": 1.0,
            "turnover_short": 1.0,
            "ic": 0.12,
            "long_tickers": {"AAA", "BBB"},
            "short_tickers": {"CCC", "DDD"},
            "n_names": 40,
            "dropped": set(),
        },
        {
            "period_of_report": pd.Timestamp("2020-06-30"),
            "as_of_date": pd.Timestamp("2020-08-28"),
            "next_as_of_date": pd.Timestamp("2020-11-27"),
            "spread_nav": _nav([1.0, 0.995, 1.005, 1.01]),
            "turnover_long": 0.3,
            "turnover_short": 0.4,
            "ic": -0.05,
            "long_tickers": {"AAA", "BBB"},
            "short_tickers": {"CCC", "DDD"},
            "n_names": 42,
            "dropped": {"CCC"},
        },
    ]
    open_quarters = [{
        "period_of_report": pd.Timestamp("2020-09-30"),
        "as_of_date": pd.Timestamp("2020-11-29"),
    }]
    ic_series = pd.Series(
        {q["period_of_report"]: q["ic"] for q in quarters}
    )
    for lag in LAG_DAYS_GRID:
        for cost in COST_BPS_GRID:
            results[(lag, cost)] = {
                "spread_nav": _nav([1.0, 1.01, 1.02, 1.015]),
                "universe_nav": _nav([1.0, 1.005, 1.01, 1.012]),
                "spy_nav": _nav([1.0, 1.003, 1.006, 1.009]),
                "ic_series": ic_series,
                "quarters": quarters,
                "open_quarters": open_quarters,
            }
    return results


def _fake_prices():
    tickers = ["AAA", "BBB", "CCC", "DDD"]
    dates = pd.date_range("2020-05-29", "2020-11-27", freq="D")
    rows = []
    for i, ticker in enumerate(tickers):
        for j, date in enumerate(dates):
            # CCC is deliberately never priced -- exercises the "dropped" path.
            if ticker == "CCC":
                continue
            rows.append({"ticker": ticker, "date": date, "adj_close": 100.0 + i + 0.01 * j})
    return pd.DataFrame(rows)


def test_build_report_writes_self_contained_html_with_no_external_refs(tmp_path):
    out_path = tmp_path / "report.html"

    result_path = build_report(_fake_results(), out_path=out_path, meta={"window": "2020-01-01 to 2020-12-31"})

    assert result_path == out_path
    html = out_path.read_text()
    assert "<title>" in html
    # No external network references -- everything must be embedded.
    assert "http://" not in html
    assert "https://" not in html
    assert "<script" not in html
    # Charts are embedded as base64 JPEG data URIs, not linked files.
    assert 'src="data:image/jpeg;base64,' in html


def test_build_report_saves_jpg_files_to_charts_subfolder(tmp_path):
    out_path = tmp_path / "report.html"

    build_report(_fake_results(), out_path=out_path)

    charts_dir = tmp_path / "charts"
    assert charts_dir.is_dir()
    jpgs = list(charts_dir.glob("*.jpg"))
    assert len(jpgs) > 0
    assert any(p.name == "primary_equity_curve.jpg" for p in jpgs)


def test_build_report_includes_expected_sections_and_stats(tmp_path):
    out_path = tmp_path / "report.html"

    build_report(_fake_results(), out_path=out_path)
    html = out_path.read_text()

    assert "Primary result" in html
    assert "Formation-lag sensitivity" in html
    assert "Transaction-cost sensitivity" in html
    assert "Full lag x cost grid" in html
    assert "Rank information coefficient" not in html
    assert "Turnover" in html
    assert "Universe coverage" in html
    assert "Known limitations" in html
    assert "still open" in html  # open-quarter disclosure
    assert "Headline:" in html  # verdict callout


def test_build_report_adds_regime_section_when_prices_supplied(tmp_path):
    out_path = tmp_path / "report.html"

    build_report(_fake_results(), out_path=out_path, prices=_fake_prices())
    html = out_path.read_text()

    assert "Regime &amp; attribution" in html or "Regime & attribution" in html
    assert "Benchmark correlation" in html
    assert "Attribution" in html
    # The deliberately-unpriced ticker's drop should be reflected in the coverage disclosure.
    assert "ticker-quarter" in html


def test_build_report_skips_regime_section_without_prices(tmp_path):
    out_path = tmp_path / "report.html"

    build_report(_fake_results(), out_path=out_path)
    html = out_path.read_text()

    assert "Regime & attribution" not in html
    assert "Benchmark correlation" not in html


def test_stats_table_renders_nan_stats_as_na_instead_of_raising():
    # performance_stats() deliberately returns NaN for every stat (n_days
    # included) when a NAV series has fewer than 2 points -- a real,
    # degenerate-but-valid case (e.g. a window with zero closed quarters),
    # caught for real by running `python -m src.run --mode smoke` against
    # a tiny sample window that happened to close zero quarters. int(nan)
    # used to raise ValueError here.
    nan_row = {
        "label": "Long-short spread",
        "total_return": float("nan"), "annualized_return": float("nan"),
        "annualized_vol": float("nan"), "sharpe": float("nan"),
        "max_drawdown": float("nan"), "hit_rate": float("nan"), "n_days": float("nan"),
    }
    html = _stats_table([nan_row], row_label_key="label", row_label_name="Series")
    assert "n/a" in html


def test_build_report_handles_empty_results_without_raising(tmp_path):
    out_path = tmp_path / "report.html"

    build_report({}, out_path=out_path)

    assert out_path.exists()
    html = out_path.read_text()
    assert "13F Ownership-Breadth-Momentum Backtest" in html
