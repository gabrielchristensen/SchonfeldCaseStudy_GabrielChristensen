import zipfile
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq
import pytest
import requests

from src.ingest import (
    _clean_coverpage,
    _clean_infotable,
    _clean_submission,
    _dedupe_combined_parquet,
    build_raw_panel,
    download_dataset,
    list_datasets,
    parse_dataset,
)


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


def test_clean_infotable_all_nan_group_stays_nan_not_zero():
    raw = pd.DataFrame({
        "ACCESSION_NUMBER": ["A1"],
        "CUSIP": ["037833100"],
        "VALUE": ["not-a-number"],
        "SSHPRNAMT": ["also-not-a-number"],
    })
    cleaned = _clean_infotable(raw)

    row = cleaned.iloc[0]
    assert np.isnan(row["VALUE"])
    assert np.isnan(row["SSHPRNAMT"])


def test_clean_infotable_partial_nan_group_sums_only_valid_values():
    # One split row is malformed, the other is real -- the group should sum
    # the real one rather than either dropping the whole group to NaN or
    # letting the malformed row silently contribute 0.
    raw = pd.DataFrame({
        "ACCESSION_NUMBER": ["A1", "A1"],
        "CUSIP": ["037833100", "037833100"],
        "VALUE": ["100", "not-a-number"],
        "SSHPRNAMT": ["10", "not-a-number"],
    })
    cleaned = _clean_infotable(raw)

    row = cleaned.iloc[0]
    assert row["VALUE"] == 100
    assert row["SSHPRNAMT"] == 10


def test_clean_infotable_normalizes_cusip_case_and_whitespace():
    raw = pd.DataFrame({
        "ACCESSION_NUMBER": ["A1", "A1"],
        "CUSIP": [" 88579y101", "88579Y101 "],
        "VALUE": ["100", "50"],
        "SSHPRNAMT": ["10", "5"],
    })
    cleaned = _clean_infotable(raw)

    assert len(cleaned) == 1
    assert cleaned.loc[0, "CUSIP"] == "88579Y101"
    assert cleaned.loc[0, "VALUE"] == 150


def test_clean_submission_drops_extra_columns():
    raw = pd.DataFrame({
        "ACCESSION_NUMBER": ["A1"],
        "FILING_DATE": ["31-MAY-2013"],
        "SUBMISSIONTYPE": ["13F-HR"],
        "CIK": ["823621"],
        "PERIODOFREPORT": ["31-MAR-2013"],
        "OTHERMANAGER": ["some value"],
    })
    cleaned = _clean_submission(raw)

    assert list(cleaned.columns) == [
        "ACCESSION_NUMBER", "FILING_DATE", "SUBMISSIONTYPE", "CIK", "PERIODOFREPORT",
    ]


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


def test_clean_submission_zfills_unpadded_cik():
    # Real data has the same filer appearing both zero-padded and not (e.g.
    # "1349434" and "0001349434") -- an exact-match CIK exclusion list
    # downstream would silently miss one form without this normalization.
    raw = pd.DataFrame({
        "ACCESSION_NUMBER": ["A1"],
        "FILING_DATE": ["31-MAY-2013"],
        "SUBMISSIONTYPE": ["13F-HR"],
        "CIK": ["1349434"],
        "PERIODOFREPORT": ["31-MAR-2013"],
    })
    cleaned = _clean_submission(raw)

    assert cleaned.loc[0, "CIK"] == "0001349434"


def test_clean_coverpage_flags_confidentiality_only_on_y():
    raw = pd.DataFrame({
        "ACCESSION_NUMBER": ["A1", "A2", "A3"],
        "FILINGMANAGER_NAME": [" Alyeska Investment Group, L.P. ", "Blank Corp", "No Corp"],
        "CONFDENIEDEXPIRED": ["Y", "N", ""],
        "DATEDENIEDEXPIRED": ["04-MAR-2013", "", ""],
        "DATEREPORTED": ["14-NOV-2012", "", ""],
        "REASONFORNONCONFIDENTIALITY": ["Confidential Treatment Expired", "", ""],
    })
    cleaned = _clean_coverpage(raw)

    flags = cleaned.set_index("ACCESSION_NUMBER")["CONFIDENTIAL_TREATMENT_FLAG"].to_dict()
    assert flags == {"A1": True, "A2": False, "A3": False}
    assert cleaned.loc[cleaned["ACCESSION_NUMBER"] == "A1", "FILINGMANAGER_NAME"].iloc[0] == "Alyeska Investment Group, L.P."


def test_clean_coverpage_coerces_blank_dates_to_nat():
    raw = pd.DataFrame({
        "ACCESSION_NUMBER": ["A1"],
        "FILINGMANAGER_NAME": ["Blank Corp"],
        "CONFDENIEDEXPIRED": [""],
        "DATEDENIEDEXPIRED": [""],
        "DATEREPORTED": [""],
        "REASONFORNONCONFIDENTIALITY": [""],
    })
    cleaned = _clean_coverpage(raw)

    assert pd.isna(cleaned.loc[0, "DATEDENIEDEXPIRED"])
    assert pd.isna(cleaned.loc[0, "DATEREPORTED"])


def test_clean_coverpage_dedups_duplicate_accession_defensively():
    raw = pd.DataFrame({
        "ACCESSION_NUMBER": ["A1", "A1"],
        "FILINGMANAGER_NAME": ["First Name", "Second Name"],
        "CONFDENIEDEXPIRED": ["", ""],
        "DATEDENIEDEXPIRED": ["", ""],
        "DATEREPORTED": ["", ""],
        "REASONFORNONCONFIDENTIALITY": ["", ""],
    })
    cleaned = _clean_coverpage(raw)

    assert len(cleaned) == 1
    assert cleaned.loc[0, "FILINGMANAGER_NAME"] == "First Name"


def _write_zip(path, members: dict):
    with zipfile.ZipFile(path, "w") as zf:
        for name, content in members.items():
            zf.writestr(name, content)


_SAMPLE_COVERPAGE_TSV = (
    "ACCESSION_NUMBER\tFILINGMANAGER_NAME\tCONFDENIEDEXPIRED\tDATEDENIEDEXPIRED\t"
    "DATEREPORTED\tREASONFORNONCONFIDENTIALITY\n"
    "A1\tSample Capital LLC\t\t\t\t\n"
)


def test_parse_dataset_handles_flat_and_nested_zip_layouts(tmp_path):
    submission_tsv = "ACCESSION_NUMBER\tFILING_DATE\tSUBMISSIONTYPE\tCIK\tPERIODOFREPORT\nA1\t31-MAY-2013\t13F-HR\t1\t31-MAR-2013\n"
    infotable_tsv = "ACCESSION_NUMBER\tCUSIP\tVALUE\tSSHPRNAMT\nA1\t037833100\t100\t10\n"

    flat_zip = tmp_path / "flat.zip"
    _write_zip(flat_zip, {
        "SUBMISSION.tsv": submission_tsv,
        "INFOTABLE.tsv": infotable_tsv,
        "COVERPAGE.tsv": _SAMPLE_COVERPAGE_TSV,
    })
    flat_tables = parse_dataset(flat_zip)
    assert len(flat_tables["submission"]) == 1
    assert len(flat_tables["infotable"]) == 1
    assert len(flat_tables["coverpage"]) == 1

    # Real-world quirk: at least one SEC dataset (01jun2025-31aug2025) nests
    # its TSVs under a subfolder instead of the zip root.
    nested_zip = tmp_path / "nested.zip"
    _write_zip(nested_zip, {
        "SOME_FOLDER/SUBMISSION.tsv": submission_tsv,
        "SOME_FOLDER/INFOTABLE.tsv": infotable_tsv,
        "SOME_FOLDER/COVERPAGE.tsv": _SAMPLE_COVERPAGE_TSV,
    })
    nested_tables = parse_dataset(nested_zip)
    assert len(nested_tables["submission"]) == 1
    assert len(nested_tables["infotable"]) == 1
    assert len(nested_tables["coverpage"]) == 1


def test_parse_dataset_ignores_unused_columns(tmp_path):
    # Real SEC datasets carry columns (NAMEOFISSUER, FIGI, voting-authority
    # splits, ...) that nothing downstream reads -- usecols should drop them
    # rather than parse and hold them in memory.
    submission_tsv = (
        "ACCESSION_NUMBER\tFILING_DATE\tSUBMISSIONTYPE\tCIK\tPERIODOFREPORT\tOTHERMANAGER\n"
        "A1\t31-MAY-2013\t13F-HR\t1\t31-MAR-2013\tsomevalue\n"
    )
    infotable_tsv = (
        "ACCESSION_NUMBER\tCUSIP\tNAMEOFISSUER\tVALUE\tSSHPRNAMT\tPUTCALL\n"
        "A1\t037833100\tAPPLE INC\t100\t10\t\n"
    )
    coverpage_tsv = (
        "ACCESSION_NUMBER\tFILINGMANAGER_NAME\tCONFDENIEDEXPIRED\tDATEDENIEDEXPIRED\t"
        "DATEREPORTED\tREASONFORNONCONFIDENTIALITY\tCRDNUMBER\n"
        "A1\tSample Capital LLC\t\t\t\t\t12345\n"
    )
    zip_path = tmp_path / "extra_columns.zip"
    _write_zip(zip_path, {
        "SUBMISSION.tsv": submission_tsv,
        "INFOTABLE.tsv": infotable_tsv,
        "COVERPAGE.tsv": coverpage_tsv,
    })

    tables = parse_dataset(zip_path)

    assert list(tables["submission"].columns) == [
        "ACCESSION_NUMBER", "FILING_DATE", "SUBMISSIONTYPE", "CIK", "PERIODOFREPORT",
    ]
    assert list(tables["infotable"].columns) == ["ACCESSION_NUMBER", "CUSIP", "VALUE", "SSHPRNAMT"]
    assert list(tables["coverpage"].columns) == [
        "ACCESSION_NUMBER", "FILINGMANAGER_NAME", "CONFDENIEDEXPIRED",
        "DATEDENIEDEXPIRED", "DATEREPORTED", "REASONFORNONCONFIDENTIALITY",
    ]


def test_parse_dataset_matches_member_names_case_insensitively(tmp_path):
    zip_path = tmp_path / "lowercase_members.zip"
    _write_zip(zip_path, {
        "submission.tsv": _SAMPLE_SUBMISSION_TSV,
        "infotable.tsv": _SAMPLE_INFOTABLE_TSV,
        "coverpage.tsv": _SAMPLE_COVERPAGE_TSV,
    })

    tables = parse_dataset(zip_path)

    assert len(tables["submission"]) == 1
    assert len(tables["infotable"]) == 1
    assert len(tables["coverpage"]) == 1


class _FakeResponse:
    def __init__(self, content: bytes = b"", text: str = ""):
        self.content = content
        self.text = text

    def raise_for_status(self):
        pass


def test_list_datasets_resolves_relative_hrefs_to_full_urls(monkeypatch):
    html = (
        '<a href="/files/structureddata/data/form-13f-data-sets/2013q2_form13f.zip">2013q2</a>'
        '<a href="https://www.sec.gov/files/structureddata/data/form-13f-data-sets/2015q1_form13f.zip">2015q1</a>'
    )
    monkeypatch.setattr("src._http.requests.get", lambda *a, **k: _FakeResponse(text=html))

    urls = list_datasets()

    assert urls == [
        "https://www.sec.gov/files/structureddata/data/form-13f-data-sets/2013q2_form13f.zip",
        "https://www.sec.gov/files/structureddata/data/form-13f-data-sets/2015q1_form13f.zip",
    ]


def test_list_datasets_dedups_duplicate_hrefs(monkeypatch):
    html = (
        '<a href="/files/structureddata/data/form-13f-data-sets/2013q2_form13f.zip">a</a>'
        '<a href="/files/structureddata/data/form-13f-data-sets/2013q2_form13f.zip">b</a>'
    )
    monkeypatch.setattr("src._http.requests.get", lambda *a, **k: _FakeResponse(text=html))

    urls = list_datasets()

    assert urls == ["https://www.sec.gov/files/structureddata/data/form-13f-data-sets/2013q2_form13f.zip"]


def test_download_dataset_writes_atomically_leaves_no_partial_file(tmp_path, monkeypatch):
    monkeypatch.setattr("src._http.requests.get", lambda *a, **k: _FakeResponse(content=b"zip-bytes"))
    monkeypatch.setattr("src.ingest.time.sleep", lambda s: None)
    dest_dir = tmp_path / "raw"

    dest = download_dataset("foo_form13f.zip", dest_dir=dest_dir)

    assert dest.read_bytes() == b"zip-bytes"
    assert not (dest_dir / "foo_form13f.zip.part").exists()


def test_download_dataset_skips_network_call_when_already_cached(tmp_path, monkeypatch):
    calls = []

    def fake_get(*args, **kwargs):
        calls.append((args, kwargs))
        return _FakeResponse(content=b"zip-bytes")

    monkeypatch.setattr("src._http.requests.get", fake_get)
    monkeypatch.setattr("src.ingest.time.sleep", lambda s: None)
    dest_dir = tmp_path / "raw"

    download_dataset("foo_form13f.zip", dest_dir=dest_dir)
    download_dataset("foo_form13f.zip", dest_dir=dest_dir)

    assert len(calls) == 1


def test_download_dataset_force_redownloads_even_if_cached(tmp_path, monkeypatch):
    calls = []

    def fake_get(*args, **kwargs):
        calls.append(1)
        return _FakeResponse(content=f"zip-bytes-v{len(calls)}".encode())

    monkeypatch.setattr("src._http.requests.get", fake_get)
    monkeypatch.setattr("src.ingest.time.sleep", lambda s: None)
    dest_dir = tmp_path / "raw"

    download_dataset("foo_form13f.zip", dest_dir=dest_dir)
    dest = download_dataset("foo_form13f.zip", dest_dir=dest_dir, force=True)

    assert len(calls) == 2
    assert dest.read_bytes() == b"zip-bytes-v2"


def test_download_dataset_failed_request_leaves_no_file(tmp_path, monkeypatch):
    class _FailingResponse:
        def raise_for_status(self):
            raise requests.HTTPError("404 Client Error")

    monkeypatch.setattr("src._http.requests.get", lambda *a, **k: _FailingResponse())
    dest_dir = tmp_path / "raw"

    with pytest.raises(requests.HTTPError):
        download_dataset("missing_form13f.zip", dest_dir=dest_dir)

    assert not (dest_dir / "missing_form13f.zip").exists()
    assert not (dest_dir / "missing_form13f.zip.part").exists()


def test_download_dataset_accepts_full_url(tmp_path, monkeypatch):
    monkeypatch.setattr("src._http.requests.get", lambda *a, **k: _FakeResponse(content=b"zip-bytes"))
    monkeypatch.setattr("src.ingest.time.sleep", lambda s: None)
    dest_dir = tmp_path / "raw"

    dest = download_dataset(
        "https://www.sec.gov/files/structureddata/data/form-13f-data-sets/2013q2_form13f.zip",
        dest_dir=dest_dir,
    )

    assert dest.name == "2013q2_form13f.zip"
    assert dest.read_bytes() == b"zip-bytes"


class _StatusResponse:
    """Mimics a real requests.Response closely enough for retry testing:
    raise_for_status() raises HTTPError with .response set to self, the
    way the real requests library does -- _get_with_retry inspects that
    attribute to decide whether a status is retryable."""

    def __init__(self, status_code: int, content: bytes = b"", text: str = ""):
        self.status_code = status_code
        self.content = content
        self.text = text

    def raise_for_status(self):
        if self.status_code >= 400:
            err = requests.exceptions.HTTPError(f"{self.status_code} Error")
            err.response = self
            raise err


def test_download_dataset_retries_on_connection_error(tmp_path, monkeypatch):
    # A real transient network failure (the same class of bug that killed
    # an unattended mapping.py build, per that module's own regression
    # test) must not kill an unattended --full ingest run either.
    calls = []

    def fake_get(url, headers, timeout):
        calls.append(1)
        if len(calls) == 1:
            raise requests.exceptions.ConnectionError("No route to host")
        return _FakeResponse(content=b"zip-bytes")

    monkeypatch.setattr("src._http.requests.get", fake_get)
    monkeypatch.setattr("src.ingest.time.sleep", lambda s: None)

    dest = download_dataset("ds1_form13f.zip", dest_dir=tmp_path / "raw")

    assert len(calls) == 2
    assert dest.read_bytes() == b"zip-bytes"


def test_list_datasets_retries_on_429(monkeypatch):
    calls = []
    html = '<a href="/files/structureddata/data/form-13f-data-sets/2013q2_form13f.zip">2013q2</a>'

    def fake_get(url, headers, timeout):
        calls.append(1)
        if len(calls) == 1:
            return _StatusResponse(429)
        return _StatusResponse(200, text=html)

    monkeypatch.setattr("src._http.requests.get", fake_get)
    monkeypatch.setattr("src.ingest.time.sleep", lambda s: None)

    result = list_datasets()

    assert len(calls) == 2
    assert result == ["https://www.sec.gov/files/structureddata/data/form-13f-data-sets/2013q2_form13f.zip"]


def test_download_dataset_raises_after_exhausting_retries(tmp_path, monkeypatch):
    def always_fails(url, headers, timeout):
        raise requests.exceptions.ConnectionError("No route to host")

    monkeypatch.setattr("src._http.requests.get", always_fails)
    monkeypatch.setattr("src.ingest.time.sleep", lambda s: None)

    with pytest.raises(requests.exceptions.ConnectionError):
        download_dataset("ds1_form13f.zip", dest_dir=tmp_path / "raw")

    # Never silently swallowed, and no partial/corrupt file left behind.
    assert not (tmp_path / "raw" / "ds1_form13f.zip").exists()
    assert not (tmp_path / "raw" / "ds1_form13f.zip.part").exists()


def test_download_dataset_does_not_retry_on_404(tmp_path, monkeypatch):
    # A permanent client error must fail fast, not burn through 5 retries
    # and a minute of backoff sleeps for something retrying can never fix.
    calls = []

    def fake_get(url, headers, timeout):
        calls.append(1)
        return _StatusResponse(404)

    monkeypatch.setattr("src._http.requests.get", fake_get)
    monkeypatch.setattr("src.ingest.time.sleep", lambda s: None)

    with pytest.raises(requests.exceptions.HTTPError):
        download_dataset("missing_form13f.zip", dest_dir=tmp_path / "raw")

    assert len(calls) == 1


_SAMPLE_SUBMISSION_TSV = (
    "ACCESSION_NUMBER\tFILING_DATE\tSUBMISSIONTYPE\tCIK\tPERIODOFREPORT\n"
    "A1\t31-MAY-2013\t13F-HR\t1\t31-MAR-2013\n"
)
_SAMPLE_INFOTABLE_TSV = "ACCESSION_NUMBER\tCUSIP\tVALUE\tSSHPRNAMT\nA1\t037833100\t100\t10\n"


def test_build_raw_panel_dedups_filing_that_appears_in_two_datasets(tmp_path):
    # Simulates the same filing landing in two dataset zips at a naming-scheme
    # boundary -- build_raw_panel should not double-count it.
    dest_dir = tmp_path / "raw"
    dest_dir.mkdir()
    for name in ("ds1_form13f.zip", "ds2_form13f.zip"):
        _write_zip(dest_dir / name, {
            "SUBMISSION.tsv": _SAMPLE_SUBMISSION_TSV,
            "INFOTABLE.tsv": _SAMPLE_INFOTABLE_TSV,
            "COVERPAGE.tsv": _SAMPLE_COVERPAGE_TSV,
        })

    panel = build_raw_panel(["ds1_form13f.zip", "ds2_form13f.zip"], dest_dir=dest_dir)

    assert len(panel) == 1


def test_build_raw_panel_keeps_distinct_filings_across_datasets(tmp_path):
    # Two different accessions holding the same CUSIP -- the dedup on
    # (ACCESSION_NUMBER, CUSIP) must not conflate them just because the CUSIP
    # matches.
    submission_tsv_1 = (
        "ACCESSION_NUMBER\tFILING_DATE\tSUBMISSIONTYPE\tCIK\tPERIODOFREPORT\n"
        "A1\t31-MAY-2013\t13F-HR\t1\t31-MAR-2013\n"
    )
    infotable_tsv_1 = "ACCESSION_NUMBER\tCUSIP\tVALUE\tSSHPRNAMT\nA1\t037833100\t100\t10\n"
    submission_tsv_2 = (
        "ACCESSION_NUMBER\tFILING_DATE\tSUBMISSIONTYPE\tCIK\tPERIODOFREPORT\n"
        "B1\t30-JUN-2015\t13F-HR\t2\t31-MAR-2015\n"
    )
    infotable_tsv_2 = "ACCESSION_NUMBER\tCUSIP\tVALUE\tSSHPRNAMT\nB1\t037833100\t500\t50\n"
    coverpage_tsv_1 = (
        "ACCESSION_NUMBER\tFILINGMANAGER_NAME\tCONFDENIEDEXPIRED\tDATEDENIEDEXPIRED\t"
        "DATEREPORTED\tREASONFORNONCONFIDENTIALITY\nA1\tManager One LLC\t\t\t\t\n"
    )
    coverpage_tsv_2 = (
        "ACCESSION_NUMBER\tFILINGMANAGER_NAME\tCONFDENIEDEXPIRED\tDATEDENIEDEXPIRED\t"
        "DATEREPORTED\tREASONFORNONCONFIDENTIALITY\nB1\tManager Two LLC\t\t\t\t\n"
    )

    dest_dir = tmp_path / "raw"
    dest_dir.mkdir()
    _write_zip(dest_dir / "ds1_form13f.zip", {
        "SUBMISSION.tsv": submission_tsv_1, "INFOTABLE.tsv": infotable_tsv_1, "COVERPAGE.tsv": coverpage_tsv_1,
    })
    _write_zip(dest_dir / "ds2_form13f.zip", {
        "SUBMISSION.tsv": submission_tsv_2, "INFOTABLE.tsv": infotable_tsv_2, "COVERPAGE.tsv": coverpage_tsv_2,
    })

    panel = build_raw_panel(["ds1_form13f.zip", "ds2_form13f.zip"], dest_dir=dest_dir)

    assert sorted(panel["ACCESSION_NUMBER"].tolist()) == ["A1", "B1"]


def test_build_raw_panel_drops_13f_nt_filings_with_no_holdings(tmp_path):
    # A 13F-NT ("notice") filer reports no holdings of its own -- it should
    # drop out on the inner join, not appear as a phantom empty position.
    submission_tsv = (
        "ACCESSION_NUMBER\tFILING_DATE\tSUBMISSIONTYPE\tCIK\tPERIODOFREPORT\n"
        "A1\t31-MAY-2013\t13F-HR\t1\t31-MAR-2013\n"
        "A2\t31-MAY-2013\t13F-NT\t2\t31-MAR-2013\n"
    )
    infotable_tsv = "ACCESSION_NUMBER\tCUSIP\tVALUE\tSSHPRNAMT\nA1\t037833100\t100\t10\n"
    # NT filers still submit a cover page, even with no INFOTABLE rows.
    coverpage_tsv = (
        "ACCESSION_NUMBER\tFILINGMANAGER_NAME\tCONFDENIEDEXPIRED\tDATEDENIEDEXPIRED\t"
        "DATEREPORTED\tREASONFORNONCONFIDENTIALITY\n"
        "A1\tManager One LLC\t\t\t\t\n"
        "A2\tManager Two LLC (NT)\t\t\t\t\n"
    )
    dest_dir = tmp_path / "raw"
    dest_dir.mkdir()
    _write_zip(dest_dir / "ds1_form13f.zip", {
        "SUBMISSION.tsv": submission_tsv, "INFOTABLE.tsv": infotable_tsv, "COVERPAGE.tsv": coverpage_tsv,
    })

    panel = build_raw_panel(["ds1_form13f.zip"], dest_dir=dest_dir)

    assert panel["ACCESSION_NUMBER"].tolist() == ["A1"]


def test_build_raw_panel_survives_missing_coverpage_row(tmp_path):
    # A COVERPAGE parsing gap must never drop an otherwise-valid INFOTABLE
    # holdings row -- the left join should surface NaN manager/confidentiality
    # columns for A1, not remove A1 from the panel.
    submission_tsv = _SAMPLE_SUBMISSION_TSV
    infotable_tsv = _SAMPLE_INFOTABLE_TSV
    # COVERPAGE has a row for a different accession only -- A1 gets nothing.
    coverpage_tsv = (
        "ACCESSION_NUMBER\tFILINGMANAGER_NAME\tCONFDENIEDEXPIRED\tDATEDENIEDEXPIRED\t"
        "DATEREPORTED\tREASONFORNONCONFIDENTIALITY\nZ9\tSome Other Manager\t\t\t\t\n"
    )
    dest_dir = tmp_path / "raw"
    dest_dir.mkdir()
    _write_zip(dest_dir / "ds1_form13f.zip", {
        "SUBMISSION.tsv": submission_tsv, "INFOTABLE.tsv": infotable_tsv, "COVERPAGE.tsv": coverpage_tsv,
    })

    panel = build_raw_panel(["ds1_form13f.zip"], dest_dir=dest_dir)

    assert panel["ACCESSION_NUMBER"].tolist() == ["A1"]
    assert pd.isna(panel.loc[0, "FILINGMANAGER_NAME"])
    assert pd.isna(panel.loc[0, "CONFIDENTIAL_TREATMENT_FLAG"])


def test_build_raw_panel_confidential_treatment_flag_flows_end_to_end(tmp_path):
    submission_tsv = _SAMPLE_SUBMISSION_TSV
    infotable_tsv = _SAMPLE_INFOTABLE_TSV
    coverpage_tsv = (
        "ACCESSION_NUMBER\tFILINGMANAGER_NAME\tCONFDENIEDEXPIRED\tDATEDENIEDEXPIRED\t"
        "DATEREPORTED\tREASONFORNONCONFIDENTIALITY\n"
        "A1\tAlyeska Investment Group, L.P.\tY\t04-MAR-2013\t14-NOV-2012\tConfidential Treatment Expired\n"
    )
    dest_dir = tmp_path / "raw"
    dest_dir.mkdir()
    _write_zip(dest_dir / "ds1_form13f.zip", {
        "SUBMISSION.tsv": submission_tsv, "INFOTABLE.tsv": infotable_tsv, "COVERPAGE.tsv": coverpage_tsv,
    })

    panel = build_raw_panel(["ds1_form13f.zip"], dest_dir=dest_dir)

    assert panel.loc[0, "FILINGMANAGER_NAME"] == "Alyeska Investment Group, L.P."
    assert panel.loc[0, "CONFIDENTIAL_TREATMENT_FLAG"] == True
    assert panel.loc[0, "REASONFORNONCONFIDENTIALITY"] == "Confidential Treatment Expired"


def test_build_raw_panel_checkpoint_roundtrip_matches_direct_parse(tmp_path):
    dest_dir = tmp_path / "raw"
    dest_dir.mkdir()
    _write_zip(dest_dir / "ds1_form13f.zip", {
        "SUBMISSION.tsv": _SAMPLE_SUBMISSION_TSV,
        "INFOTABLE.tsv": _SAMPLE_INFOTABLE_TSV,
        "COVERPAGE.tsv": _SAMPLE_COVERPAGE_TSV,
    })
    checkpoint_dir = tmp_path / "parts"

    direct = build_raw_panel(["ds1_form13f.zip"], dest_dir=dest_dir)
    via_checkpoint = build_raw_panel(["ds1_form13f.zip"], dest_dir=dest_dir, checkpoint_dir=checkpoint_dir)
    via_checkpoint_reused = build_raw_panel(["ds1_form13f.zip"], dest_dir=dest_dir, checkpoint_dir=checkpoint_dir)

    pd.testing.assert_frame_equal(direct.reset_index(drop=True), via_checkpoint.reset_index(drop=True))
    pd.testing.assert_frame_equal(direct.reset_index(drop=True), via_checkpoint_reused.reset_index(drop=True))


def test_build_raw_panel_with_checkpoint_dir_streams_each_dataset_to_disk(tmp_path, monkeypatch):
    # Regression test for a real OOM: the old implementation appended every
    # dataset's parsed frame to a Python list for the whole loop and only
    # combined them via one final pd.concat -- at --full scale (~55
    # datasets) that meant holding every dataset simultaneously plus a
    # second full-size buffer for the concat itself, which is what blew up
    # memory right at the end of a real run. With checkpoint_dir set, each
    # dataset must instead be written straight to a combined parquet file
    # (one dataset's memory footprint at a time) via pq.ParquetWriter,
    # never accumulated as a growing in-memory list.
    checkpoint_dir = tmp_path / "parts"
    combined_path = checkpoint_dir / "_combined_raw.parquet"
    write_calls = []
    real_init = pq.ParquetWriter.__init__
    real_write_table = pq.ParquetWriter.write_table

    def spy_init(self, where, *args, **kwargs):
        self._test_where = Path(where)
        return real_init(self, where, *args, **kwargs)

    def spy_write_table(self, table, **kwargs):
        # to_parquet() (used for per-dataset checkpoints) also routes through
        # ParquetWriter internally -- only count writes to the combined
        # streaming file, which is what this test is verifying.
        if getattr(self, "_test_where", None) == combined_path:
            write_calls.append(table.num_rows)
        return real_write_table(self, table, **kwargs)

    monkeypatch.setattr(pq.ParquetWriter, "__init__", spy_init)
    monkeypatch.setattr(pq.ParquetWriter, "write_table", spy_write_table)

    dest_dir = tmp_path / "raw"
    dest_dir.mkdir()
    _write_zip(dest_dir / "ds1_form13f.zip", {
        "SUBMISSION.tsv": _SAMPLE_SUBMISSION_TSV,
        "INFOTABLE.tsv": _SAMPLE_INFOTABLE_TSV,
        "COVERPAGE.tsv": _SAMPLE_COVERPAGE_TSV,
    })
    submission_tsv_2 = (
        "ACCESSION_NUMBER\tFILING_DATE\tSUBMISSIONTYPE\tCIK\tPERIODOFREPORT\n"
        "B1\t30-JUN-2015\t13F-HR\t2\t31-MAR-2015\n"
    )
    infotable_tsv_2 = "ACCESSION_NUMBER\tCUSIP\tVALUE\tSSHPRNAMT\nB1\t037833100\t500\t50\n"
    _write_zip(dest_dir / "ds2_form13f.zip", {
        "SUBMISSION.tsv": submission_tsv_2,
        "INFOTABLE.tsv": infotable_tsv_2,
        "COVERPAGE.tsv": _SAMPLE_COVERPAGE_TSV.replace("A1", "B1"),
    })

    panel = build_raw_panel(
        ["ds1_form13f.zip", "ds2_form13f.zip"], dest_dir=dest_dir, checkpoint_dir=checkpoint_dir,
    )

    assert sorted(panel["ACCESSION_NUMBER"].tolist()) == ["A1", "B1"]
    # One write_table call per dataset -- proves each dataset was streamed
    # to disk individually rather than accumulated in memory for one final
    # pd.concat.
    assert write_calls == [1, 1]
    # The streaming combine buffer must not survive a successful run -- a
    # leftover file would otherwise risk being mistaken for a complete one.
    assert not combined_path.exists()


def test_build_raw_panel_with_checkpoint_dir_dedups_across_dataset_boundary(tmp_path):
    # Same scenario as test_build_raw_panel_dedups_filing_that_appears_in_two_datasets,
    # but through the checkpoint_dir/streaming-combine path -- the dedup logic
    # changed shape (mask-then-restream instead of read-then-drop_duplicates)
    # when the OOM fix was pushed further, so correctness needs its own
    # regression test on that path specifically.
    dest_dir = tmp_path / "raw"
    dest_dir.mkdir()
    for name in ("ds1_form13f.zip", "ds2_form13f.zip"):
        _write_zip(dest_dir / name, {
            "SUBMISSION.tsv": _SAMPLE_SUBMISSION_TSV,
            "INFOTABLE.tsv": _SAMPLE_INFOTABLE_TSV,
            "COVERPAGE.tsv": _SAMPLE_COVERPAGE_TSV,
        })

    panel = build_raw_panel(
        ["ds1_form13f.zip", "ds2_form13f.zip"], dest_dir=dest_dir, checkpoint_dir=tmp_path / "parts",
    )

    assert len(panel) == 1


def test_build_raw_panel_with_checkpoint_dir_leaves_no_temp_files_behind(tmp_path):
    dest_dir = tmp_path / "raw"
    dest_dir.mkdir()
    _write_zip(dest_dir / "ds1_form13f.zip", {
        "SUBMISSION.tsv": _SAMPLE_SUBMISSION_TSV,
        "INFOTABLE.tsv": _SAMPLE_INFOTABLE_TSV,
        "COVERPAGE.tsv": _SAMPLE_COVERPAGE_TSV,
    })
    checkpoint_dir = tmp_path / "parts"

    build_raw_panel(["ds1_form13f.zip"], dest_dir=dest_dir, checkpoint_dir=checkpoint_dir)

    assert not (checkpoint_dir / "_combined_raw.parquet").exists()
    assert not (checkpoint_dir / "_combined_raw_deduped.parquet").exists()


def test_dedupe_combined_parquet_keeps_first_occurrence_across_batch_boundary(tmp_path):
    # Regression test for the offset/slicing arithmetic in
    # _dedupe_combined_parquet: force a tiny batch_size so the duplicate pair
    # (rows 1 and 3) is split across three separate batches, and confirm the
    # keep-mask is still applied against the correct absolute row, not
    # re-zeroed per batch.
    combined_path = tmp_path / "_combined_raw.parquet"
    df = pd.DataFrame({
        "ACCESSION_NUMBER": ["A1", "A2", "A1", "A3"],
        "CUSIP": ["037833100", "037833100", "037833100", "88579Y101"],
        "VALUE": [100, 200, 999, 300],  # A1/037833100's second occurrence -- must be the one dropped
    })
    df.to_parquet(combined_path)

    result = _dedupe_combined_parquet(combined_path, batch_size=1)

    assert sorted(result["ACCESSION_NUMBER"].tolist()) == ["A1", "A2", "A3"]
    kept_a1 = result[result["ACCESSION_NUMBER"] == "A1"].iloc[0]
    assert kept_a1["VALUE"] == 100  # first occurrence kept, not the later duplicate
    assert not (tmp_path / "_combined_raw_deduped.parquet").exists()


def test_dedupe_combined_parquet_no_duplicates_returns_all_rows_unchanged(tmp_path):
    combined_path = tmp_path / "_combined_raw.parquet"
    df = pd.DataFrame({
        "ACCESSION_NUMBER": ["A1", "A2", "A3"],
        "CUSIP": ["037833100", "88579Y101", "594918104"],
        "VALUE": [100, 200, 300],
    })
    df.to_parquet(combined_path)

    result = _dedupe_combined_parquet(combined_path, batch_size=2)

    assert len(result) == 3
    assert sorted(result["VALUE"].tolist()) == [100, 200, 300]


def test_build_raw_panel_checkpoint_dir_skips_reparsing_on_rerun(tmp_path, monkeypatch):
    dest_dir = tmp_path / "raw"
    dest_dir.mkdir()
    _write_zip(dest_dir / "ds1_form13f.zip", {
        "SUBMISSION.tsv": _SAMPLE_SUBMISSION_TSV,
        "INFOTABLE.tsv": _SAMPLE_INFOTABLE_TSV,
        "COVERPAGE.tsv": _SAMPLE_COVERPAGE_TSV,
    })
    checkpoint_dir = tmp_path / "parts"

    calls = []
    real_parse_dataset = parse_dataset

    def counting_parse_dataset(zip_path):
        calls.append(zip_path)
        return real_parse_dataset(zip_path)

    monkeypatch.setattr("src.ingest.parse_dataset", counting_parse_dataset)

    build_raw_panel(["ds1_form13f.zip"], dest_dir=dest_dir, checkpoint_dir=checkpoint_dir)
    build_raw_panel(["ds1_form13f.zip"], dest_dir=dest_dir, checkpoint_dir=checkpoint_dir)

    assert len(calls) == 1


def _fake_build_raw_panel(captured):
    def fake(filenames, dest_dir=None, *, checkpoint_dir=None):
        captured["filenames"] = filenames
        captured["checkpoint_dir"] = checkpoint_dir
        return pd.DataFrame({"ACCESSION_NUMBER": ["A1"], "CUSIP": ["037833100"]})

    return fake


def test_main_full_mode_wires_list_datasets_and_checkpoint_dir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["ingest.py", "--full"])
    monkeypatch.setattr("src.ingest.list_datasets", lambda: ["https://example.com/x_form13f.zip"])
    captured = {}
    monkeypatch.setattr("src.ingest.build_raw_panel", _fake_build_raw_panel(captured))

    from src.ingest import main
    main()

    assert captured["filenames"] == ["https://example.com/x_form13f.zip"]
    assert captured["checkpoint_dir"] == Path("data/processed/13f_panel_parts")
    assert (tmp_path / "data/processed/13f_panel_full.parquet").exists()


def test_main_default_mode_uses_sample_path_and_no_checkpoint_dir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["ingest.py"])
    captured = {}
    monkeypatch.setattr("src.ingest.build_raw_panel", _fake_build_raw_panel(captured))

    from src.ingest import main
    main()

    assert captured["filenames"] == [
        "2013q2_form13f.zip", "2015q1_form13f.zip", "01mar2026-31may2026_form13f.zip",
    ]
    assert captured["checkpoint_dir"] is None
    assert (tmp_path / "data/processed/13f_panel_sample.parquet").exists()
