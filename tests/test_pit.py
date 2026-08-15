import pandas as pd

from src.pit import as_of_snapshot, breadth

PERIOD = "2020-03-31"

_COLUMNS = [
    "ACCESSION_NUMBER", "FILING_DATE", "SUBMISSIONTYPE", "CIK",
    "PERIODOFREPORT", "CUSIP", "VALUE", "SSHPRNAMT",
]


def _panel(rows):
    df = pd.DataFrame(rows, columns=_COLUMNS)
    df["FILING_DATE"] = pd.to_datetime(df["FILING_DATE"])
    df["PERIODOFREPORT"] = pd.to_datetime(df["PERIODOFREPORT"])
    return df


def test_amendment_after_as_of_date_is_ignored():
    panel = _panel([
        ("A0000001", "2020-05-10", "13F-HR", "CIK_A", PERIOD, "CUSIP1", 100, 10),
        ("A0000002", "2020-08-01", "13F-HR/A", "CIK_A", PERIOD, "CUSIP1", 200, 20),
    ])
    snap = as_of_snapshot(panel, PERIOD, as_of_date="2020-05-15")
    assert snap["SSHPRNAMT"].tolist() == [10]


def test_amendment_on_or_before_as_of_date_supersedes_original():
    panel = _panel([
        ("A0000001", "2020-05-10", "13F-HR", "CIK_A", PERIOD, "CUSIP1", 100, 10),
        ("A0000002", "2020-08-01", "13F-HR/A", "CIK_A", PERIOD, "CUSIP1", 200, 20),
    ])
    snap = as_of_snapshot(panel, PERIOD, as_of_date="2020-08-01")
    assert snap["SSHPRNAMT"].tolist() == [20]


def test_late_filer_absent_not_backfilled():
    panel = _panel([
        ("A0000001", "2020-05-10", "13F-HR", "CIK_A", PERIOD, "CUSIP1", 100, 10),
        ("A0000003", "2020-09-01", "13F-HR", "CIK_B", PERIOD, "CUSIP1", 500, 50),
    ])
    snap = as_of_snapshot(panel, PERIOD, as_of_date="2020-05-15")
    assert set(snap["CIK"]) == {"CIK_A"}


def test_as_of_snapshot_excludes_other_reporting_periods():
    # Same CIK filing for two different quarters -- a snapshot for one
    # period must not leak in the other period's holdings even though both
    # filings are already accepted by as_of_date.
    panel = _panel([
        ("A0000001", "2020-05-10", "13F-HR", "CIK_A", "2020-03-31", "CUSIP1", 100, 10),
        ("A0000002", "2020-08-10", "13F-HR", "CIK_A", "2020-06-30", "CUSIP1", 200, 20),
    ])
    snap = as_of_snapshot(panel, "2020-03-31", as_of_date="2020-12-31")
    assert snap["PERIODOFREPORT"].unique().tolist() == [pd.Timestamp("2020-03-31")]
    assert snap["SSHPRNAMT"].tolist() == [10]


def test_latest_filing_wins_across_multiple_amendments():
    panel = _panel([
        ("A0000001", "2020-05-10", "13F-HR", "CIK_A", PERIOD, "CUSIP1", 100, 10),
        ("A0000002", "2020-06-01", "13F-HR/A", "CIK_A", PERIOD, "CUSIP1", 200, 20),
        ("A0000003", "2020-07-01", "13F-HR/A", "CIK_A", PERIOD, "CUSIP1", 300, 30),
    ])
    snap = as_of_snapshot(panel, PERIOD, as_of_date="2020-12-31")
    assert snap["SSHPRNAMT"].tolist() == [30]


def test_same_day_amendment_wins_tiebreak_by_accession_number():
    panel = _panel([
        ("A0000001", "2020-05-10", "13F-HR", "CIK_A", PERIOD, "CUSIP1", 100, 10),
        ("A0000002", "2020-05-10", "13F-HR/A", "CIK_A", PERIOD, "CUSIP1", 999, 99),
    ])
    snap = as_of_snapshot(panel, PERIOD, as_of_date="2020-05-10")
    assert snap["SSHPRNAMT"].tolist() == [99]


def test_breadth_counts_distinct_ciks_and_respects_exclusions():
    panel = _panel([
        ("A0000001", "2020-05-10", "13F-HR", "CIK_A", PERIOD, "CUSIP1", 100, 10),
        ("A0000002", "2020-05-11", "13F-HR", "CIK_B", PERIOD, "CUSIP1", 200, 20),
        ("A0000003", "2020-05-12", "13F-HR", "CIK_C", PERIOD, "CUSIP1", 300, 30),
    ])
    result = breadth(panel, PERIOD, as_of_date="2020-06-01")
    assert result.set_index("CUSIP")["breadth"].to_dict() == {"CUSIP1": 3}

    result_excl = breadth(panel, PERIOD, as_of_date="2020-06-01", exclude_ciks={"CIK_B"})
    assert result_excl.set_index("CUSIP")["breadth"].to_dict() == {"CUSIP1": 2}


def test_breadth_drops_cusip_entirely_when_all_filers_excluded():
    panel = _panel([
        ("A0000001", "2020-05-10", "13F-HR", "CIK_A", PERIOD, "CUSIP1", 100, 10),
    ])
    result = breadth(panel, PERIOD, as_of_date="2020-06-01", exclude_ciks={"CIK_A"})
    assert result.empty


def test_breadth_computes_independently_across_multiple_cusips():
    # A single 13F filing reports every CUSIP a manager holds under one
    # accession number -- CIK_A's one filing here covers both CUSIP1 and
    # CUSIP2.
    panel = _panel([
        ("A0000001", "2020-05-10", "13F-HR", "CIK_A", PERIOD, "CUSIP1", 100, 10),
        ("A0000001", "2020-05-10", "13F-HR", "CIK_A", PERIOD, "CUSIP2", 50, 5),
        ("A0000002", "2020-05-11", "13F-HR", "CIK_B", PERIOD, "CUSIP1", 200, 20),
    ])
    result = breadth(panel, PERIOD, as_of_date="2020-06-01")
    assert result.set_index("CUSIP")["breadth"].to_dict() == {"CUSIP1": 2, "CUSIP2": 1}


def test_no_known_filings_returns_empty():
    panel = _panel([
        ("A0000001", "2020-05-10", "13F-HR", "CIK_A", PERIOD, "CUSIP1", 100, 10),
    ])
    snap = as_of_snapshot(panel, PERIOD, as_of_date="2020-01-01")
    assert snap.empty
