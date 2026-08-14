"""Download and parse SEC Form 13F structured data sets.

SEC publishes one ZIP per ~3-month period at
sec.gov/data-research/sec-markets-data/form-13f-data-sets, each containing
every 13F filing *received* in that window, already flattened into TSV
tables (SUBMISSION, COVERPAGE, INFOTABLE, ...). Naming changed in 2024 from
calendar-quarter buckets (`2013q2_form13f.zip`) to rolling 3-month windows
anchored to Feb/May/Aug/Nov (`01mar2024-31may2024_form13f.zip`), so
`list_datasets` scrapes the index page for real filenames rather than
templating a URL pattern.

Only SUBMISSION and INFOTABLE are used here: SUBMISSION carries FILING_DATE
(the actual EDGAR acceptance date -- this is the point-in-time axis
everything downstream keys off) and PERIODOFREPORT; INFOTABLE carries the
holdings. 13F-NT ("notice") filings have no INFOTABLE rows of their own --
the manager is reporting that another manager files its holdings -- so they
drop out naturally on the inner join in `build_raw_panel`.

Note for future phases: VALUE's units changed on 2023-01-03 (thousands of
dollars before, whole dollars after) -- irrelevant to the breadth factor
(which only counts distinct filers), but must be corrected before VALUE is
used for anything value-weighted.
"""

import re
import time
import zipfile
from pathlib import Path

import pandas as pd
import requests

SEC_INDEX_URL = "https://www.sec.gov/data-research/sec-markets-data/form-13f-data-sets"
SEC_FILE_BASE = "https://www.sec.gov/files/structureddata/data/form-13f-data-sets"
USER_AGENT = "SchonfeldCaseStudy gabriel.christensen2019@gmail.com"

RAW_DIR = Path("data/raw/13f")


def _headers() -> dict:
    return {"User-Agent": USER_AGENT}


def list_datasets() -> list[str]:
    """Scrape the SEC index page for every available dataset filename."""
    resp = requests.get(SEC_INDEX_URL, headers=_headers(), timeout=30)
    resp.raise_for_status()
    names = re.findall(r'href="[^"]*/([^"/]+_form13f\.zip)"', resp.text, re.I)
    return sorted(set(names))


def download_dataset(filename: str, dest_dir: Path = RAW_DIR, *, force: bool = False) -> Path:
    """Download (with local caching) one dataset ZIP."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / filename
    if dest.exists() and not force:
        return dest
    url = f"{SEC_FILE_BASE}/{filename}"
    resp = requests.get(url, headers=_headers(), timeout=180)
    resp.raise_for_status()
    dest.write_bytes(resp.content)
    time.sleep(0.2)  # polite spacing; nowhere near the 10 req/sec SEC fair-access limit
    return dest


def parse_dataset(zip_path: Path) -> dict[str, pd.DataFrame]:
    """Parse the SUBMISSION and INFOTABLE tables out of a dataset ZIP."""
    tables = {}
    with zipfile.ZipFile(zip_path) as zf:
        members = {name.upper(): name for name in zf.namelist()}
        for table in ("SUBMISSION", "INFOTABLE"):
            member = members[f"{table}.TSV"]
            with zf.open(member) as fh:
                tables[table.lower()] = pd.read_csv(fh, sep="\t", dtype=str)
    return tables


def _clean_submission(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["FILING_DATE"] = pd.to_datetime(df["FILING_DATE"], format="%d-%b-%Y")
    df["PERIODOFREPORT"] = pd.to_datetime(df["PERIODOFREPORT"], format="%d-%b-%Y")
    df["CIK"] = df["CIK"].str.strip()
    return df[["ACCESSION_NUMBER", "FILING_DATE", "SUBMISSIONTYPE", "CIK", "PERIODOFREPORT"]]


def _clean_infotable(df: pd.DataFrame) -> pd.DataFrame:
    """One row per (accession, CUSIP), aggregated across the voting-authority
    /investment-discretion split rows a single filing can report for the same
    security (a single filing commonly reports the same CUSIP more than
    once)."""
    df = df.copy()
    df["VALUE"] = pd.to_numeric(df["VALUE"], errors="coerce")
    df["SSHPRNAMT"] = pd.to_numeric(df["SSHPRNAMT"], errors="coerce")
    df["CUSIP"] = df["CUSIP"].str.strip().str.upper()
    return (
        df.groupby(["ACCESSION_NUMBER", "CUSIP"], as_index=False)
        .agg(VALUE=("VALUE", "sum"), SSHPRNAMT=("SSHPRNAMT", "sum"))
    )


def build_raw_panel(filenames: list[str], dest_dir: Path = RAW_DIR) -> pd.DataFrame:
    """Download+parse each dataset and return one long panel: one row per
    (CIK, PERIODOFREPORT, ACCESSION_NUMBER, FILING_DATE, SUBMISSIONTYPE,
    CUSIP, VALUE, SSHPRNAMT), after per-filing dedup."""
    frames = []
    for filename in filenames:
        zip_path = download_dataset(filename, dest_dir)
        tables = parse_dataset(zip_path)
        submission = _clean_submission(tables["submission"])
        infotable = _clean_infotable(tables["infotable"])
        frames.append(submission.merge(infotable, on="ACCESSION_NUMBER", how="inner"))
    return pd.concat(frames, ignore_index=True)


if __name__ == "__main__":
    # Phase 1 validation run: a handful of real datasets spanning both naming
    # schemes, proving the pipeline end-to-end. The full ~40-dataset
    # historical backfill is a separate, later batch run.
    sample = ["2013q2_form13f.zip", "2015q1_form13f.zip", "01mar2026-31may2026_form13f.zip"]
    panel = build_raw_panel(sample)
    out_path = Path("data/processed/13f_panel_sample.parquet")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    panel.to_parquet(out_path)
    print(f"{len(panel):,} rows across {panel['ACCESSION_NUMBER'].nunique():,} filings "
          f"-> {out_path}")
