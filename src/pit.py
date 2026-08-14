"""Point-in-time panel construction for the 13F holdings panel.

For any (CIK, reporting period), the holdings "known" as of a given date are
those of the most recently *filed* submission (original 13F-HR or a
13F-HR/A amendment) whose FILING_DATE is on or before that date. A later
amendment is treated as a full replacement of the prior filing for that
period ("latest filing wins") -- this module does not parse SEC's
AMENDMENTTYPE field to distinguish a full restatement from a partial one and
merge it with the prior snapshot. That's a deliberate, disclosed scope cut,
not silent handling.

CIKs with no filing yet accepted by `as_of_date` are simply absent from the
snapshot -- never backfilled with a later filing's data. That is the
no-lookahead guarantee this module exists to enforce, and the thing
`tests/test_pit.py` checks directly.
"""

import pandas as pd


def _winning_accessions(panel: pd.DataFrame, period_of_report, as_of_date) -> set:
    """Accession number of each CIK's latest known filing for
    `period_of_report` as of `as_of_date`. Ties (same-day original +
    amendment) are broken by accession number, since same-day pairs do
    occur and the later accession is the one that supersedes."""
    period_of_report = pd.Timestamp(period_of_report)
    as_of_date = pd.Timestamp(as_of_date)

    submissions = (
        panel.loc[
            (panel["PERIODOFREPORT"] == period_of_report)
            & (panel["FILING_DATE"] <= as_of_date),
            ["CIK", "ACCESSION_NUMBER", "FILING_DATE"],
        ]
        .drop_duplicates()
        .sort_values(["CIK", "FILING_DATE", "ACCESSION_NUMBER"])
    )
    winners = submissions.groupby("CIK")["ACCESSION_NUMBER"].last()
    return set(winners)


def as_of_snapshot(panel: pd.DataFrame, period_of_report, as_of_date) -> pd.DataFrame:
    """Holdings known as of `as_of_date` for the given `period_of_report`.

    `panel` is the long panel produced by `ingest.build_raw_panel`.
    """
    winning = _winning_accessions(panel, period_of_report, as_of_date)
    if not winning:
        return panel.iloc[0:0]
    return panel[panel["ACCESSION_NUMBER"].isin(winning)].reset_index(drop=True)


def breadth(
    panel: pd.DataFrame,
    period_of_report,
    as_of_date,
    *,
    exclude_ciks: set | None = None,
) -> pd.DataFrame:
    """Distinct filer-CIK count per CUSIP, as known as of `as_of_date`."""
    snap = as_of_snapshot(panel, period_of_report, as_of_date)
    if exclude_ciks:
        snap = snap[~snap["CIK"].isin(exclude_ciks)]
    return snap.groupby("CUSIP")["CIK"].nunique().rename("breadth").reset_index()
