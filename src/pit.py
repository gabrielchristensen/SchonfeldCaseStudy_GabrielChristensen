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


def _period_slice(panel: pd.DataFrame, period_of_report: pd.Timestamp) -> pd.DataFrame:
    """Rows for a single PERIODOFREPORT.

    At --full scale (~82M rows), the boolean mask `panel["PERIODOFREPORT"]
    == X` re-scans the entire panel on every call -- profiled at ~20s/call,
    78% of it in pyarrow's take kernel moving Arrow-backed string/datetime
    columns, dominant enough that the full lag x quarter backtest grid
    (~300+ calls) never finished in a reasonable time. `panel.attrs
    ["period_groups"]`, if present (see backtest.py's `main()`), is a
    `{period: int64 row-positions}` dict built once via a single pass over
    just the PERIODOFREPORT column (`groupby(...).indices`) -- turns this
    into an O(k) positional take (k = rows in that one quarter) instead of
    an O(n) full-panel scan. Falls back to the boolean mask when absent
    (every existing test builds a plain DataFrame with no `.attrs` set), so
    this is a pure performance path, not a second code path with different
    semantics -- `.iloc[positions]` and the boolean mask select the exact
    same rows for the exact same period_of_report.
    """
    period_groups = panel.attrs.get("period_groups")
    if period_groups is not None:
        positions = period_groups.get(period_of_report)
        result = panel.iloc[0:0] if positions is None else panel.iloc[positions]
    else:
        result = panel[panel["PERIODOFREPORT"] == period_of_report]
    # pandas propagates .attrs to every derived object via __finalize__,
    # which deep-copies it -- harmless for a small dict, but period_groups
    # covers the whole panel (~650MB of position arrays), so every
    # downstream op on this already-scoped slice (sort_values, groupby,
    # drop_duplicates, ...) was re-deep-copying that full dict for no
    # reason (profiled: 79% of this function's real-panel runtime). It's
    # only ever read here, so once it's been used to build `result`,
    # nothing downstream needs it.
    if result.attrs:
        result.attrs = {}
    return result


def _winning_accessions(period_slice: pd.DataFrame, as_of_date) -> set:
    """Accession number of each CIK's latest known filing for the given
    period (already scoped to one PERIODOFREPORT by `_period_slice`), as of
    `as_of_date`. Ties (same-day original + amendment) are broken by
    accession number, since same-day pairs do occur and the later accession
    is the one that supersedes."""
    as_of_date = pd.Timestamp(as_of_date)

    submissions = (
        period_slice.loc[
            period_slice["FILING_DATE"] <= as_of_date,
            ["CIK", "ACCESSION_NUMBER", "FILING_DATE"],
        ]
        .drop_duplicates()
        .sort_values(["CIK", "FILING_DATE", "ACCESSION_NUMBER"])
    )
    winners = submissions.groupby("CIK")["ACCESSION_NUMBER"].last()
    return set(winners) #Return the ACCESION_NUMBER winners for the as_of_date period in a set (for efficiency)


def as_of_snapshot(panel: pd.DataFrame, period_of_report, as_of_date) -> pd.DataFrame:
    """Holdings known as of `as_of_date` for the given `period_of_report`.

    `panel` is the long panel produced by `ingest.build_raw_panel`.
    """
    period_of_report = pd.Timestamp(period_of_report)
    period_slice = _period_slice(panel, period_of_report)
    winning = _winning_accessions(period_slice, as_of_date)
    if not winning:
        return panel.iloc[0:0] #empty df instead of None/[] to avoid crashes
    # Filtered on the same period_slice, not the full panel: every winning
    # accession belongs to this period_of_report by construction, so the
    # ACCESSION_NUMBER match can never land outside period_slice -- this is
    # the second full-panel scan the docstring above refers to, now folded
    # into the same small slice as the FILING_DATE filter.
    return period_slice[period_slice["ACCESSION_NUMBER"].isin(winning)].reset_index(drop=True)


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
        snap = snap[~snap["CIK"].isin(exclude_ciks)] #REMOVE PASSIVE PPLAYERS
    return snap.groupby("CUSIP")["CIK"].nunique().rename("breadth").reset_index() #for each CUSIP, return breadth
