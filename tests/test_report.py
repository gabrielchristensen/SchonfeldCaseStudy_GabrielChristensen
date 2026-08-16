import re

import pandas as pd

from src.backtest import COST_BPS_GRID, LAG_DAYS_GRID, PRIMARY_COST_BPS, PRIMARY_LAG_DAYS
from src.report import build_report


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
            "turnover_long": 1.0,
            "turnover_short": 1.0,
            "ic": 0.12,
        },
        {
            "period_of_report": pd.Timestamp("2020-06-30"),
            "as_of_date": pd.Timestamp("2020-08-28"),
            "next_as_of_date": pd.Timestamp("2020-11-27"),
            "turnover_long": 0.3,
            "turnover_short": 0.4,
            "ic": -0.05,
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
    # Charts are embedded as base64 data URIs, not linked files.
    assert 'src="data:image/png;base64,' in html


def test_build_report_includes_expected_sections_and_stats(tmp_path):
    out_path = tmp_path / "report.html"

    build_report(_fake_results(), out_path=out_path)
    html = out_path.read_text()

    assert "Primary result" in html
    assert "Formation-lag sensitivity" in html
    assert "Transaction-cost sensitivity" in html
    assert "Rank information coefficient" in html
    assert "Turnover" in html
    assert "Known limitations" in html
    assert "still open" in html  # open-quarter disclosure


def test_build_report_handles_empty_results_without_raising(tmp_path):
    out_path = tmp_path / "report.html"

    build_report({}, out_path=out_path)

    assert out_path.exists()
    html = out_path.read_text()
    assert "13F Ownership-Breadth-Momentum Backtest" in html
