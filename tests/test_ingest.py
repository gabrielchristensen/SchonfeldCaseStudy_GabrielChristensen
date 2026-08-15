import zipfile
from io import BytesIO

import pandas as pd

from src.ingest import _clean_infotable, _clean_submission, parse_dataset


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


def _write_zip(path, members: dict):
    with zipfile.ZipFile(path, "w") as zf:
        for name, content in members.items():
            zf.writestr(name, content)


def test_parse_dataset_handles_flat_and_nested_zip_layouts(tmp_path):
    submission_tsv = "ACCESSION_NUMBER\tFILING_DATE\tSUBMISSIONTYPE\tCIK\tPERIODOFREPORT\nA1\t31-MAY-2013\t13F-HR\t1\t31-MAR-2013\n"
    infotable_tsv = "ACCESSION_NUMBER\tCUSIP\tVALUE\tSSHPRNAMT\nA1\t037833100\t100\t10\n"

    flat_zip = tmp_path / "flat.zip"
    _write_zip(flat_zip, {"SUBMISSION.tsv": submission_tsv, "INFOTABLE.tsv": infotable_tsv})
    flat_tables = parse_dataset(flat_zip)
    assert len(flat_tables["submission"]) == 1
    assert len(flat_tables["infotable"]) == 1

    # Real-world quirk: at least one SEC dataset (01jun2025-31aug2025) nests
    # its TSVs under a subfolder instead of the zip root.
    nested_zip = tmp_path / "nested.zip"
    _write_zip(nested_zip, {
        "SOME_FOLDER/SUBMISSION.tsv": submission_tsv,
        "SOME_FOLDER/INFOTABLE.tsv": infotable_tsv,
    })
    nested_tables = parse_dataset(nested_zip)
    assert len(nested_tables["submission"]) == 1
    assert len(nested_tables["infotable"]) == 1
