import pandas as pd

from src.pit import _period_slice, as_of_snapshot, breadth

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


# --- period_groups fast path (backtest.py's real-run performance path) ---
# All existing tests above build a plain DataFrame with no .attrs set, so
# they only ever exercise the boolean-mask fallback. These tests exercise
# the `panel.attrs["period_groups"]` fast path directly and prove it's
# equivalent, not just faster -- see backtest.prepare_panel / pit._period_slice.

OTHER_PERIOD = "2020-06-30"


def _panel_with_period_groups(rows):
    panel = _panel(rows)
    panel.attrs["period_groups"] = panel.groupby("PERIODOFREPORT", sort=False).indices
    return panel


def test_period_slice_fast_path_matches_boolean_mask_with_duplicate_periods_and_ties():
    rows = [
        # PERIOD: same-day amendment tie (CIK_A) plus an unrelated filer.
        ("A0000001", "2020-05-10", "13F-HR", "CIK_A", PERIOD, "CUSIP1", 100, 10),
        ("A0000002", "2020-05-10", "13F-HR/A", "CIK_A", PERIOD, "CUSIP1", 150, 15),
        ("A0000003", "2020-05-11", "13F-HR", "CIK_B", PERIOD, "CUSIP2", 200, 20),
        # A second, distinct period interleaved in the same panel.
        ("A0000004", "2020-08-10", "13F-HR", "CIK_C", OTHER_PERIOD, "CUSIP1", 300, 30),
    ]
    plain = _panel(rows)
    indexed = _panel_with_period_groups(rows)

    for period, as_of in [(PERIOD, "2020-06-01"), (OTHER_PERIOD, "2020-09-01")]:
        fast = _period_slice(indexed, pd.Timestamp(period)).reset_index(drop=True)
        boolean_mask = _period_slice(plain, pd.Timestamp(period)).reset_index(drop=True)
        pd.testing.assert_frame_equal(fast, boolean_mask)

    fast_snap = as_of_snapshot(indexed, PERIOD, as_of_date="2020-05-10").reset_index(drop=True)
    boolean_snap = as_of_snapshot(plain, PERIOD, as_of_date="2020-05-10").reset_index(drop=True)
    pd.testing.assert_frame_equal(fast_snap, boolean_snap)
    # The amendment (A0000002) wins the same-day tie in both paths.
    assert fast_snap["SSHPRNAMT"].tolist() == boolean_snap["SSHPRNAMT"].tolist() == [15]


def test_period_slice_fast_path_returns_empty_for_period_absent_from_groups():
    indexed = _panel_with_period_groups([
        ("A0000001", "2020-05-10", "13F-HR", "CIK_A", PERIOD, "CUSIP1", 100, 10),
    ])
    result = _period_slice(indexed, pd.Timestamp("2021-12-31"))
    assert result.empty


def test_period_slice_does_not_leak_period_groups_attrs_onto_the_returned_slice():
    # Regression guard for the fix itself: pandas deep-copies .attrs on
    # every downstream op (sort_values, groupby, ...), so leaving the
    # whole-panel period_groups dict attached to the small per-period
    # slice silently re-triggers that deep copy on every op a caller runs
    # on the slice afterward -- profiled as the dominant real-run cost
    # after the O(n) scan itself was fixed. The slice must come back clean.
    indexed = _panel_with_period_groups([
        ("A0000001", "2020-05-10", "13F-HR", "CIK_A", PERIOD, "CUSIP1", 100, 10),
    ])
    result = _period_slice(indexed, pd.Timestamp(PERIOD))
    assert result.attrs == {}
    assert "period_groups" in indexed.attrs  # the source panel is untouched
