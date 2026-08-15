import numpy as np
import pandas as pd
import pytest

from src.factor import breadth_change, breadth_momentum, prior_quarter_end, standardize_cross_section

PERIOD = "2020-03-31"
PRIOR_PERIOD = "2019-12-31"

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
        "CUSIP": ["037833100", "594918104", "02079K107"],
        "TICKER": ["AAPL", "MSFT", "GOOGL"],
        "NAME": ["APPLE INC", "MICROSOFT CORP", "ALPHABET INC"],
    })


def _write_history(path, rows):
    pd.DataFrame(rows, columns=["date", "tickers"]).to_csv(path, index=False)


# --- prior_quarter_end -------------------------------------------------

@pytest.mark.parametrize("period,expected", [
    ("2020-03-31", "2019-12-31"),
    ("2020-06-30", "2020-03-31"),
    ("2020-09-30", "2020-06-30"),
    ("2020-12-31", "2020-09-30"),
])
def test_prior_quarter_end_all_boundary_transitions(period, expected):
    assert prior_quarter_end(period) == pd.Timestamp(expected)


# --- breadth_change ------------------------------------------------------

def test_breadth_change_computes_correct_raw_change(tmp_path):
    history_path = tmp_path / "sp500_history.csv"
    _write_history(history_path, [("2019-01-01", "AAPL,MSFT")])

    panel = _panel([
        # AAPL: 3 filers this period.
        ("A1", "2020-05-10", "13F-HR", "CIK_A", PERIOD, "037833100", 100, 10),
        ("A2", "2020-05-10", "13F-HR", "CIK_B", PERIOD, "037833100", 100, 10),
        ("A3", "2020-05-10", "13F-HR", "CIK_C", PERIOD, "037833100", 100, 10),
        # AAPL: 1 filer prior period.
        ("B1", "2020-02-01", "13F-HR", "CIK_A", PRIOR_PERIOD, "037833100", 100, 10),
    ])

    result = breadth_change(panel, PERIOD, as_of_date="2020-06-01", mapping=_mapping(), history_path=history_path)

    row = result.set_index("CUSIP").loc["037833100"]
    assert row["breadth"] == 3
    assert row["breadth_prior"] == 1
    assert row["raw_change"] == 2


def test_breadth_change_drops_cusip_with_no_prior_period_breadth(tmp_path):
    history_path = tmp_path / "sp500_history.csv"
    _write_history(history_path, [("2019-01-01", "AAPL,MSFT,GOOGL")])

    panel = _panel([
        # CIK_A's one current-period filing covers both CUSIPs -- a real
        # 13F filing reports every holding under a single accession number.
        ("A1", "2020-05-10", "13F-HR", "CIK_A", PERIOD, "037833100", 100, 10),
        # GOOGL: only in the current period -- no prior-period rows at all,
        # simulating a name newly added to the S&P 500 this quarter.
        ("A1", "2020-05-10", "13F-HR", "CIK_A", PERIOD, "02079K107", 100, 10),
        # AAPL: present both periods.
        ("B1", "2020-02-01", "13F-HR", "CIK_A", PRIOR_PERIOD, "037833100", 100, 10),
    ])

    result = breadth_change(panel, PERIOD, as_of_date="2020-06-01", mapping=_mapping(), history_path=history_path)

    assert set(result["CUSIP"]) == {"037833100"}


def test_breadth_change_uses_same_as_of_date_for_both_periods_not_frozen_snapshot(tmp_path):
    # Point-in-time design proof: a late-arriving filing for the PRIOR
    # period (accepted well after that period's own typical formation
    # date, but before the CURRENT formation date) must be picked up --
    # breadth_prior should reflect everything known as of the CURRENT
    # as_of_date, not a frozen snapshot from the prior period's own cycle.
    history_path = tmp_path / "sp500_history.csv"
    _write_history(history_path, [("2019-01-01", "AAPL,MSFT")])

    panel = _panel([
        ("A1", "2020-05-10", "13F-HR", "CIK_A", PERIOD, "037833100", 100, 10),
        # Prior period's own on-time filing.
        ("B1", "2020-02-01", "13F-HR", "CIK_A", PRIOR_PERIOD, "037833100", 100, 10),
        # A second, unrelated filer reporting the SAME prior period late --
        # accepted well after the prior period's own ~45-day deadline.
        ("B2", "2020-08-15", "13F-HR", "CIK_B", PRIOR_PERIOD, "037833100", 100, 10),
    ])

    # as_of_date after the late filing -- must see both prior-period filers.
    late_result = breadth_change(
        panel, PERIOD, as_of_date="2020-09-01", mapping=_mapping(), history_path=history_path
    )
    assert late_result.set_index("CUSIP").loc["037833100", "breadth_prior"] == 2

    # as_of_date before the late filing -- must NOT see it (no lookahead).
    early_result = breadth_change(
        panel, PERIOD, as_of_date="2020-05-15", mapping=_mapping(), history_path=history_path
    )
    assert early_result.set_index("CUSIP").loc["037833100", "breadth_prior"] == 1


# --- standardize_cross_section --------------------------------------------

def test_standardize_cross_section_rank_signal_matches_known_ordering():
    df = pd.DataFrame({"CUSIP": ["A", "B", "C"], "raw_change": [10, 30, 20]})

    result = standardize_cross_section(df)

    ranks = result.set_index("CUSIP")["rank_signal"]
    assert ranks["A"] == pytest.approx(1 / 3)
    assert ranks["C"] == pytest.approx(2 / 3)
    assert ranks["B"] == pytest.approx(1.0)


def test_standardize_cross_section_zscore_matches_hand_computed_values():
    df = pd.DataFrame({"CUSIP": ["A", "B", "C", "D", "E"], "raw_change": [1, 2, 3, 4, 5]})

    result = standardize_cross_section(df)

    expected_std = np.std([1, 2, 3, 4, 5], ddof=1)
    expected = [(x - 3) / expected_std for x in [1, 2, 3, 4, 5]]
    assert result["zscore_signal"].tolist() == pytest.approx(expected)


def test_standardize_cross_section_degenerate_constant_column_returns_nan_not_inf():
    df = pd.DataFrame({"CUSIP": ["A", "B"], "raw_change": [5, 5]})

    result = standardize_cross_section(df)

    assert result["zscore_signal"].isna().all()
    assert not np.isinf(result["zscore_signal"]).any()


def test_standardize_cross_section_single_row_returns_nan_not_raise():
    df = pd.DataFrame({"CUSIP": ["A"], "raw_change": [5]})

    result = standardize_cross_section(df)

    assert result["zscore_signal"].isna().all()
    assert result["rank_signal"].iloc[0] == 1.0


# --- breadth_momentum (integration) ---------------------------------------

def test_breadth_momentum_composes_change_and_standardization(tmp_path):
    history_path = tmp_path / "sp500_history.csv"
    _write_history(history_path, [("2019-01-01", "AAPL,MSFT")])

    panel = _panel([
        # CIK_A's one current-period filing covers both CUSIPs.
        ("A1", "2020-05-10", "13F-HR", "CIK_A", PERIOD, "037833100", 100, 10),
        ("A1", "2020-05-10", "13F-HR", "CIK_A", PERIOD, "594918104", 100, 10),
        ("A2", "2020-05-10", "13F-HR", "CIK_B", PERIOD, "037833100", 100, 10),
        # CIK_A's one prior-period filing covers both CUSIPs.
        ("B1", "2020-02-01", "13F-HR", "CIK_A", PRIOR_PERIOD, "037833100", 100, 10),
        ("B1", "2020-02-01", "13F-HR", "CIK_A", PRIOR_PERIOD, "594918104", 100, 10),
        ("B2", "2020-02-01", "13F-HR", "CIK_B", PRIOR_PERIOD, "594918104", 100, 10),
    ])

    result = breadth_momentum(panel, PERIOD, as_of_date="2020-06-01", mapping=_mapping(), history_path=history_path)

    assert set(result.columns) == {
        "CUSIP", "breadth", "breadth_prior", "raw_change", "rank_signal", "zscore_signal",
    }
    aapl = result.set_index("CUSIP").loc["037833100"]
    msft = result.set_index("CUSIP").loc["594918104"]
    assert aapl["raw_change"] == 1  # 2 - 1
    assert msft["raw_change"] == -1  # 1 - 2
    # AAPL has the higher raw_change, so it should rank above MSFT.
    assert aapl["rank_signal"] > msft["rank_signal"]
