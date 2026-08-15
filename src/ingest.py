"""Download and parse SEC Form 13F structured data sets.

SEC publishes one ZIP per ~3-month period at
sec.gov/data-research/sec-markets-data/form-13f-data-sets, each containing
every 13F filing *received* in that window, already flattened into TSV
tables (SUBMISSION, COVERPAGE, INFOTABLE, ...). Naming changed in 2024 from
calendar-quarter buckets (`2013q2_form13f.zip`) to rolling 3-month windows
anchored to Feb/May/Aug/Nov (`01mar2024-31may2024_form13f.zip`), so
`list_datasets` scrapes the index page for real download URLs rather than
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

Webscrapping maxtime = 3min * Number of Quarters + 30s 
(timeout to scrape the hrefs is 30s, while timeout to zipfiles requests are
180s)

"""

import argparse
import re
import time
import zipfile
from pathlib import Path
from urllib.parse import urljoin

import pandas as pd
import requests

SEC_INDEX_URL = "https://www.sec.gov/data-research/sec-markets-data/form-13f-data-sets"
SEC_FILE_BASE = "https://www.sec.gov/files/structureddata/data/form-13f-data-sets"
USER_AGENT = "SchonfeldCaseStudy gabriel.christensen2019@gmail.com"

RAW_DIR = Path("data/raw/13f") #Use of Path instead of a string so there are no problems caused by different OS

# Only these columns are ever read downstream (see _clean_submission /
# _clean_infotable) -- restricting parsing to them cuts I/O and peak memory
# substantially at --full scale, where each dataset also carries columns
# like NAMEOFISSUER, FIGI, and the voting-authority split fields that are
# never used.
_SUBMISSION_COLUMNS = ["ACCESSION_NUMBER", "FILING_DATE", "SUBMISSIONTYPE", "CIK", "PERIODOFREPORT"]
_INFOTABLE_COLUMNS = ["ACCESSION_NUMBER", "CUSIP", "VALUE", "SSHPRNAMT"]

#Simple function to not repeat the hashmap everytime. Also, if there is any future change in the headers required, it is simples to adjust
def _headers() -> dict:
    return {"User-Agent": USER_AGENT}


def list_datasets() -> list[str]:
    """Scrape the SEC index page for every available dataset's full URL.

    Returns the actual scraped href (resolved to an absolute URL), not a
    filename reconstructed against SEC_FILE_BASE -- if SEC ever serves a
    dataset from a different path, this still finds it.

    use of urljoin in case of relative link

    use of set to avoid duplicates
    
    """
    resp = requests.get(SEC_INDEX_URL, headers=_headers(), timeout=30)
    resp.raise_for_status() #Throw exception if failure on status code (ex: 404)
    hrefs = re.findall(r'href="([^"]*_form13f\.zip)"', resp.text, re.I)
    return sorted({urljoin(SEC_INDEX_URL, href) for href in hrefs})


def download_dataset(url_or_filename: str, dest_dir: Path = RAW_DIR, *, force: bool = False) -> Path:
    """Download (with local caching) one dataset ZIP.

    Accepts either a full URL (as returned by `list_datasets`) or a bare
    filename (resolved against SEC_FILE_BASE, for the pinned validation
    sample in `main`). Writes to a temp file and renames into place so a
    dest that exists is always a complete download -- an interrupted
    download can never leave a truncated file that later runs mistake for
    a valid cache hit.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    url = url_or_filename if url_or_filename.startswith("http") else f"{SEC_FILE_BASE}/{url_or_filename}"
    filename = url.rsplit("/", 1)[-1] #extracts filename from the end of a URL/path
    dest = dest_dir / filename #join a directory path with a filename.
    if dest.exists() and not force:
        return dest
    resp = requests.get(url, headers=_headers(), timeout=180) #Bigger timeout margins because the zipfiles are bigger
    resp.raise_for_status() 
    tmp = dest.with_suffix(dest.suffix + ".part")
    tmp.write_bytes(resp.content)
    tmp.replace(dest)  # atomic on same filesystem
    time.sleep(0.2)  # polite spacing; nowhere near the 10 req/sec SEC fair-access limit
    return dest


def parse_dataset(zip_path: Path) -> dict[str, pd.DataFrame]:
    """Parse the SUBMISSION and INFOTABLE tables out of a dataset ZIP.

    Most datasets store the TSVs at the zip root, but at least one observed
    dataset (01jun2025-31aug2025) nests them under a subfolder instead
    (`01JUN2025-31AUG2025_form13f/SUBMISSION.tsv`) -- match on basename, not
    exact path, to be robust to that inconsistency.
    """
    tables = {}
    usecols = {"SUBMISSION": _SUBMISSION_COLUMNS, "INFOTABLE": _INFOTABLE_COLUMNS}
    with zipfile.ZipFile(zip_path) as zf:
        members = {name.rsplit("/", 1)[-1].upper(): name for name in zf.namelist()}
        for table in ("SUBMISSION", "INFOTABLE"):
            member = members[f"{table}.TSV"]
            with zf.open(member) as fh:
                tables[table.lower()] = pd.read_csv(fh, sep="\t", dtype=str, usecols=usecols[table])
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
    once).

    Uses sum(min_count=1) rather than plain sum: pandas' default sum treats
    an all-NaN group as 0, which would make a filing whose VALUE/SSHPRNAMT
    are entirely unparseable indistinguishable from a real zero position.
    With min_count=1, a group needs at least one real number to sum to
    anything other than NaN, so a data-quality failure stays visible as NaN
    instead of silently becoming a fake zero.
    """
    df = df.copy()
    df["VALUE"] = pd.to_numeric(df["VALUE"], errors="coerce")
    df["SSHPRNAMT"] = pd.to_numeric(df["SSHPRNAMT"], errors="coerce")
    df["CUSIP"] = df["CUSIP"].str.strip().str.upper()
    return (
        df.groupby(["ACCESSION_NUMBER", "CUSIP"], as_index=False)
        .agg(
            VALUE=("VALUE", lambda s: s.sum(min_count=1)),
            SSHPRNAMT=("SSHPRNAMT", lambda s: s.sum(min_count=1)),
        )
    )


def build_raw_panel(
    filenames: list[str], dest_dir: Path = RAW_DIR, *, checkpoint_dir: Path | None = None
) -> pd.DataFrame:
    """Download+parse each dataset and return one long panel: one row per
    (CIK, PERIODOFREPORT, ACCESSION_NUMBER, FILING_DATE, SUBMISSIONTYPE,
    CUSIP, VALUE, SSHPRNAMT), after per-filing dedup.

    If `checkpoint_dir` is given, each dataset's parsed+merged frame is
    cached there as its own parquet after processing. On a re-run, a
    dataset with an existing checkpoint is loaded from disk instead of
    re-parsed -- so a crash partway through `--full` mode (~55 datasets)
    doesn't force re-parsing everything already done.

    SEC's dataset windows are documented as covering disjoint filing
    periods, but the naming scheme changed in 2024 (calendar-quarter
    buckets -> rolling 3-month windows); the final dedup on
    (ACCESSION_NUMBER, CUSIP) guards against double-counting a filing that
    ends up represented in two datasets at that boundary.
    """
    frames = []
    for filename in filenames:
        zip_path = download_dataset(filename, dest_dir)
        checkpoint_path = checkpoint_dir / f"{zip_path.stem}.parquet" if checkpoint_dir else None
        if checkpoint_path is not None and checkpoint_path.exists():
            frames.append(pd.read_parquet(checkpoint_path))
            continue
        tables = parse_dataset(zip_path)
        submission = _clean_submission(tables["submission"])
        infotable = _clean_infotable(tables["infotable"])
        merged = submission.merge(infotable, on="ACCESSION_NUMBER", how="inner")
        if checkpoint_path is not None:
            checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
            merged.to_parquet(checkpoint_path)
        frames.append(merged)
    panel = pd.concat(frames, ignore_index=True)
    return panel.drop_duplicates(subset=["ACCESSION_NUMBER", "CUSIP"]).reset_index(drop=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--full",
        action="store_true",
        help="Download and parse every dataset currently listed on the SEC "
        "index page (~55 zips, several GB, takes a while) instead of the "
        "3-dataset validation sample. The exact set of datasets depends on "
        "what SEC has published as of run time, so this is not bit-for-bit "
        "reproducible across runs -- re-running later will pick up newly "
        "published quarters.",
    )
    args = parser.parse_args()

    if args.full:
        filenames = list_datasets()
        out_path = Path("data/processed/13f_panel_full.parquet")
        checkpoint_dir = Path("data/processed/13f_panel_parts")
    else:
        # Phase 1 validation run: a handful of real datasets spanning both
        # naming schemes, proving the pipeline end-to-end.
        filenames = ["2013q2_form13f.zip", "2015q1_form13f.zip", "01mar2026-31may2026_form13f.zip"]
        out_path = Path("data/processed/13f_panel_sample.parquet")
        checkpoint_dir = None

    panel = build_raw_panel(filenames, checkpoint_dir=checkpoint_dir)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    panel.to_parquet(out_path)
    print(f"{len(filenames)} datasets -> {len(panel):,} rows across "
          f"{panel['ACCESSION_NUMBER'].nunique():,} filings -> {out_path}")


if __name__ == "__main__":
    main()
