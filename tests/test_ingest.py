import zipfile
from io import BytesIO
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import requests

from src.ingest import (
    _clean_infotable,
    _clean_submission,
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
    zip_path = tmp_path / "extra_columns.zip"
    _write_zip(zip_path, {"SUBMISSION.tsv": submission_tsv, "INFOTABLE.tsv": infotable_tsv})

    tables = parse_dataset(zip_path)

    assert list(tables["submission"].columns) == [
        "ACCESSION_NUMBER", "FILING_DATE", "SUBMISSIONTYPE", "CIK", "PERIODOFREPORT",
    ]
    assert list(tables["infotable"].columns) == ["ACCESSION_NUMBER", "CUSIP", "VALUE", "SSHPRNAMT"]


def test_parse_dataset_matches_member_names_case_insensitively(tmp_path):
    zip_path = tmp_path / "lowercase_members.zip"
    _write_zip(zip_path, {"submission.tsv": _SAMPLE_SUBMISSION_TSV, "infotable.tsv": _SAMPLE_INFOTABLE_TSV})

    tables = parse_dataset(zip_path)

    assert len(tables["submission"]) == 1
    assert len(tables["infotable"]) == 1


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
    monkeypatch.setattr("src.ingest.requests.get", lambda *a, **k: _FakeResponse(text=html))

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
    monkeypatch.setattr("src.ingest.requests.get", lambda *a, **k: _FakeResponse(text=html))

    urls = list_datasets()

    assert urls == ["https://www.sec.gov/files/structureddata/data/form-13f-data-sets/2013q2_form13f.zip"]


def test_download_dataset_writes_atomically_leaves_no_partial_file(tmp_path, monkeypatch):
    monkeypatch.setattr("src.ingest.requests.get", lambda *a, **k: _FakeResponse(content=b"zip-bytes"))
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

    monkeypatch.setattr("src.ingest.requests.get", fake_get)
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

    monkeypatch.setattr("src.ingest.requests.get", fake_get)
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

    monkeypatch.setattr("src.ingest.requests.get", lambda *a, **k: _FailingResponse())
    dest_dir = tmp_path / "raw"

    with pytest.raises(requests.HTTPError):
        download_dataset("missing_form13f.zip", dest_dir=dest_dir)

    assert not (dest_dir / "missing_form13f.zip").exists()
    assert not (dest_dir / "missing_form13f.zip.part").exists()


def test_download_dataset_accepts_full_url(tmp_path, monkeypatch):
    monkeypatch.setattr("src.ingest.requests.get", lambda *a, **k: _FakeResponse(content=b"zip-bytes"))
    monkeypatch.setattr("src.ingest.time.sleep", lambda s: None)
    dest_dir = tmp_path / "raw"

    dest = download_dataset(
        "https://www.sec.gov/files/structureddata/data/form-13f-data-sets/2013q2_form13f.zip",
        dest_dir=dest_dir,
    )

    assert dest.name == "2013q2_form13f.zip"
    assert dest.read_bytes() == b"zip-bytes"


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

    dest_dir = tmp_path / "raw"
    dest_dir.mkdir()
    _write_zip(dest_dir / "ds1_form13f.zip", {"SUBMISSION.tsv": submission_tsv_1, "INFOTABLE.tsv": infotable_tsv_1})
    _write_zip(dest_dir / "ds2_form13f.zip", {"SUBMISSION.tsv": submission_tsv_2, "INFOTABLE.tsv": infotable_tsv_2})

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
    dest_dir = tmp_path / "raw"
    dest_dir.mkdir()
    _write_zip(dest_dir / "ds1_form13f.zip", {"SUBMISSION.tsv": submission_tsv, "INFOTABLE.tsv": infotable_tsv})

    panel = build_raw_panel(["ds1_form13f.zip"], dest_dir=dest_dir)

    assert panel["ACCESSION_NUMBER"].tolist() == ["A1"]


def test_build_raw_panel_checkpoint_roundtrip_matches_direct_parse(tmp_path):
    dest_dir = tmp_path / "raw"
    dest_dir.mkdir()
    _write_zip(dest_dir / "ds1_form13f.zip", {
        "SUBMISSION.tsv": _SAMPLE_SUBMISSION_TSV,
        "INFOTABLE.tsv": _SAMPLE_INFOTABLE_TSV,
    })
    checkpoint_dir = tmp_path / "parts"

    direct = build_raw_panel(["ds1_form13f.zip"], dest_dir=dest_dir)
    via_checkpoint = build_raw_panel(["ds1_form13f.zip"], dest_dir=dest_dir, checkpoint_dir=checkpoint_dir)
    via_checkpoint_reused = build_raw_panel(["ds1_form13f.zip"], dest_dir=dest_dir, checkpoint_dir=checkpoint_dir)

    pd.testing.assert_frame_equal(direct.reset_index(drop=True), via_checkpoint.reset_index(drop=True))
    pd.testing.assert_frame_equal(direct.reset_index(drop=True), via_checkpoint_reused.reset_index(drop=True))


def test_build_raw_panel_checkpoint_dir_skips_reparsing_on_rerun(tmp_path, monkeypatch):
    dest_dir = tmp_path / "raw"
    dest_dir.mkdir()
    _write_zip(dest_dir / "ds1_form13f.zip", {
        "SUBMISSION.tsv": _SAMPLE_SUBMISSION_TSV,
        "INFOTABLE.tsv": _SAMPLE_INFOTABLE_TSV,
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
