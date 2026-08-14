import pandas as pd

from src.ingest import _clean_infotable, _clean_submission


def test_clean_infotable_aggregates_voting_authority_split_rows():
    raw = pd.DataFrame({
        "ACCESSION_NUMBER": ["A1", "A1", "A2"],
        "CUSIP": ["037833100", "037833100", "88579Y101"],
        "VALUE": ["100", "50", "200"],
        "SSHPRNAMT": ["10", "5", "20"],
    })
    cleaned = _clean_infotable(raw)

    assert len(cleaned) == 2
    row = cleaned[(cleaned["ACCESSION_NUMBER"] == "A1") & (cleaned["CUSIP"] == "037833100")].iloc[0]
    assert row["VALUE"] == 150
    assert row["SSHPRNAMT"] == 15


def test_clean_submission_parses_sec_date_format_and_strips_cik():
    raw = pd.DataFrame({
        "ACCESSION_NUMBER": ["A1"],
        "FILING_DATE": ["31-MAY-2013"],
        "SUBMISSIONTYPE": ["13F-HR"],
        "CIK": [" 0000823621 "],
        "PERIODOFREPORT": ["31-MAR-2013"],
    })
    cleaned = _clean_submission(raw)

    assert cleaned.loc[0, "FILING_DATE"] == pd.Timestamp("2013-05-31")
    assert cleaned.loc[0, "PERIODOFREPORT"] == pd.Timestamp("2013-03-31")
    assert cleaned.loc[0, "CIK"] == "0000823621"
