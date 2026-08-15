"""CUSIP -> ticker crosswalk.

Split into two layers, mirroring ingest.py's download/parse split:

- Offline/network layer (candidate_cusips, fetch_openfigi_mappings,
  build_cusip_ticker_map): run once, by a human, via `python -m src.mapping
  --build`. Produces a small static CSV committed to data/reference/.
- Pure/runtime layer (load_cusip_ticker_map, cusip_to_ticker,
  unmapped_summary): no network, used by the actual pipeline and covered by
  tests.

CUSIP -> ticker (not the reverse) is the well-documented OpenFIGI use case:
querying with idType=ID_CUSIP as input is standard and unrestricted. A single
CUSIP query returns every global listing (ADRs, currency-denominated
variants, multiple exchanges) for that security -- verified against real
OpenFIGI responses for AAPL (258 results) and MSFT (279 results). The
primary listing is picked by grouping results by compositeFIGI (every
venue/currency variant of one underlying security shares a compositeFIGI)
and taking the largest group -- NOT by filtering exchCode == "US", which
looked sufficient against AAPL/MSFT but turned out not to be universal: a
real query for ExxonMobil's CUSIP has no exchCode == "US" row at all (the
real "XOM" listings use "UZ"/"OU"/"QU" instead), so an exchCode filter alone
silently picked an unrelated Colombian listing instead. See
_choose_primary_listing's docstring for the full detail.

Efficiency: do not map every CUSIP ever seen in the ingested panel (~120k
distinct CUSIPs in the full historical panel) -- candidate_cusips
pre-filters to CUSIPs with real institutional weight (cumulative breadth
above a threshold) before any network call, since S&P 500 names are
overwhelmingly held by many filers while the long tail of thinly-held
small-caps/private placements is not.
"""

import time
from pathlib import Path

import pandas as pd
import requests

OPENFIGI_URL = "https://api.openfigi.com/v3/mapping"
USER_AGENT = "SchonfeldCaseStudy gabriel.christensen2019@gmail.com"

# Verified empirically against the real endpoint (not assumed from docs):
# an unauthenticated request is capped at exactly 10 mapping jobs ("Request
# may only contain 10 mapping jobs." on an 11th job) -- an authenticated
# request with a registered API key is documented at up to 100.
_BATCH_SIZE_UNAUTHENTICATED = 10
_BATCH_SIZE_AUTHENTICATED = 100

DEFAULT_MAP_PATH = Path("data/reference/cusip_ticker_map.csv")
DEFAULT_OVERRIDES_PATH = Path("data/reference/cusip_overrides.csv")


def candidate_cusips(panel: pd.DataFrame, *, start_date, end_date, min_breadth: int = 10) -> set[str]:
    """CUSIPs with >= min_breadth cumulative distinct filers across
    [start_date, end_date] (inclusive, on FILING_DATE).

    This is a volume pre-filter to shrink the OpenFIGI query set, not the
    point-in-time pit.breadth() -- it only needs to be directionally right.
    """
    start_date = pd.Timestamp(start_date)
    end_date = pd.Timestamp(end_date)
    window = panel[(panel["FILING_DATE"] >= start_date) & (panel["FILING_DATE"] <= end_date)]
    breadth = window.groupby("CUSIP")["CIK"].nunique()
    return set(breadth[breadth >= min_breadth].index)


def _headers(api_key: str | None) -> dict:
    headers = {"Content-Type": "application/json", "User-Agent": USER_AGENT}
    if api_key:
        headers["X-OPENFIGI-APIKEY"] = api_key
    return headers


def _choose_primary_listing(data: list[dict]) -> dict:
    """Pick the single primary US listing out of a CUSIP query's many results.

    Originally filtered to exchCode == "US", but that's not universal --
    verified against a real query for ExxonMobil's CUSIP (30231G102): the
    result set has 11 rows across 4 compositeFIGI groups, and NONE is tagged
    exchCode == "US" (the real "XOM" ticker rows use "UZ"/"OU"/"QU" instead).
    Naively falling back to data[0] in that case silently picked a wrong,
    unrelated listing ("EXMOC" on a Colombian venue).

    The robust signal instead: group by compositeFIGI (every venue/currency
    variant of the *same* underlying security shares one compositeFIGI) and
    take the largest group among Equity/Common Stock rows -- the primary
    listing is the one with the most exchange-code variants. Confirmed this
    also picks the correct AAPL/MSFT listing (their real "US" row is in by
    far the largest group anyway) and correctly picks "XOM" (3-row group)
    over "EXMOC" (2-row group) for the CUSIP above.
    """
    equity_rows = [r for r in data if r.get("marketSector") == "Equity" and r.get("securityType2") == "Common Stock"]
    candidates = equity_rows or data
    groups: dict[str, list[dict]] = {}
    for row in candidates:
        groups.setdefault(row.get("compositeFIGI"), []).append(row)
    best_figi = max(groups, key=lambda figi: len(groups[figi]))
    group = groups[best_figi]
    us_row = next((r for r in group if r.get("exchCode") == "US"), None)
    return us_row or group[0]


_RESULT_COLUMNS = ["CUSIP", "TICKER", "NAME", "RESOLVED"]


def fetch_openfigi_mappings(
    cusips: list[str], *, api_key: str | None = None, checkpoint_path: Path | None = None
) -> pd.DataFrame:
    """POST to OpenFIGI's mapping endpoint in batches (idType=ID_CUSIP) --
    10 jobs/request unauthenticated, 100 with a registered API key.

    Returns one row per input CUSIP: [CUSIP, TICKER, NAME, RESOLVED]. A CUSIP
    OpenFIGI can't resolve (delisted, private placement, malformed) gets
    RESOLVED=False with TICKER/NAME left null -- never silently dropped from
    the output, so the caller can count and inspect unresolved CUSIPs.

    Polite spacing: unauthenticated OpenFIGI is rate-limited to roughly
    25 requests/minute, an authenticated key to roughly 250/minute (no
    rate-limit headers are exposed to compute this precisely). The sleep
    between batches keeps either mode comfortably under its limit.

    If `checkpoint_path` is given, each batch's results are appended to it
    immediately, and any CUSIP already present there is skipped on this call
    -- a real ~49-minute unauthenticated run (11,711 candidates) hit a
    transient "No route to host" network failure partway through with no
    resilience, losing all progress; this makes a re-run resume instead of
    restarting from zero. Mirrors ingest.py's checkpoint_dir for --full mode.
    """
    # Explicit dtypes (not a bare pd.DataFrame(columns=...)) matter here: an
    # empty frame's columns default to dtype object, and concatenating that
    # with the real bool-dtype RESOLVED column later silently demotes the
    # whole column to object -- `~` on an object array of Python bools then
    # does bitwise-invert (True -> -2, False -> -1) instead of logical NOT,
    # which pandas then tries to use as integer *column labels* rather than
    # a boolean mask. Hit this for real on a fresh (no-checkpoint) run.
    already = pd.DataFrame({
        "CUSIP": pd.Series(dtype=str), "TICKER": pd.Series(dtype=str),
        "NAME": pd.Series(dtype=str), "RESOLVED": pd.Series(dtype=bool),
    })
    if checkpoint_path is not None and checkpoint_path.exists():
        already = pd.read_csv(checkpoint_path, dtype={"CUSIP": str})
        already["RESOLVED"] = already["RESOLVED"].astype(bool)
    done = set(already["CUSIP"])
    remaining = [c for c in cusips if c not in done]

    headers = _headers(api_key)
    batch_size = _BATCH_SIZE_AUTHENTICATED if api_key else _BATCH_SIZE_UNAUTHENTICATED
    new_rows = []
    for i in range(0, len(remaining), batch_size):
        batch = remaining[i : i + batch_size]
        jobs = [{"idType": "ID_CUSIP", "idValue": c} for c in batch]
        resp = _post_with_retry(headers, jobs)
        results = resp.json()
        batch_rows = []
        for cusip, job in zip(batch, results):
            data = job.get("data")
            if not data:
                batch_rows.append({"CUSIP": cusip, "TICKER": None, "NAME": None, "RESOLVED": False})
                continue
            chosen = _choose_primary_listing(data)
            batch_rows.append({
                "CUSIP": cusip,
                "TICKER": chosen.get("ticker"),
                "NAME": chosen.get("name"),
                "RESOLVED": True,
            })
        new_rows.extend(batch_rows)
        if checkpoint_path is not None:
            checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
            pd.DataFrame(batch_rows, columns=_RESULT_COLUMNS).to_csv(
                checkpoint_path, mode="a", header=not checkpoint_path.exists(), index=False
            )
        if i + batch_size < len(remaining):
            time.sleep(2.5 if not api_key else 0.3)
    new_df = pd.DataFrame(new_rows, columns=_RESULT_COLUMNS)
    combined = pd.concat([already, new_df], ignore_index=True)[_RESULT_COLUMNS]
    # Defensive, not redundant: even with explicit dtypes above, concat can
    # still demote RESOLVED to object in edge cases -- this guarantees the
    # caller always gets a real boolean column, not a footgun.
    combined["RESOLVED"] = combined["RESOLVED"].astype(bool)
    return combined


def _post_with_retry(headers: dict, jobs: list[dict], max_attempts: int = 5):
    """Retry on transient failures -- connection errors/timeouts (a real
    "No route to host" killed an unattended ~49-minute build mid-run with no
    resilience) as well as 429 (rate limit) and 5xx -- with exponential
    backoff. No rate-limit headers are exposed to compute a precise
    backoff, so a fixed schedule (5s, 10s, 20s, 40s) is used instead.
    """
    delay = 5
    for attempt in range(max_attempts):
        try:
            resp = requests.post(OPENFIGI_URL, headers=headers, json=jobs, timeout=30)
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
            if attempt == max_attempts - 1:
                raise
            time.sleep(delay)
            delay *= 2
            continue
        if resp.status_code == 429 or resp.status_code >= 500:
            if attempt == max_attempts - 1:
                resp.raise_for_status()
            time.sleep(delay)
            delay *= 2
            continue
        resp.raise_for_status()
        return resp
    raise RuntimeError("unreachable")  # pragma: no cover


def sp500_ticker_coverage(mapping_df: pd.DataFrame, sp500_history_path: Path) -> dict:
    """Coverage check from the S&P side, not just the CUSIP side: what
    fraction of tickers that were EVER an S&P 500 constituent (per
    sp500_history.csv) have no resolved CUSIP in mapping_df.

    This catches a different failure mode than unmapped_summary: a
    genuine S&P 500 name whose CUSIP never got queried at all because it
    fell below candidate_cusips' breadth pre-filter (most plausible in the
    earliest, thinnest-coverage years, or a name added to the index
    shortly before the backtest window starts).
    """
    history = pd.read_csv(sp500_history_path)
    ever_tickers = set()
    for tickers in history["tickers"]:
        ever_tickers.update(t.strip() for t in tickers.split(","))
    mapped_tickers = set(mapping_df.loc[mapping_df["TICKER"].notna(), "TICKER"])
    missing = sorted(ever_tickers - mapped_tickers)
    return {
        "n_ever_sp500_tickers": len(ever_tickers),
        "n_missing": len(missing),
        "frac_missing": len(missing) / len(ever_tickers) if ever_tickers else 0.0,
        "missing_tickers": missing,
    }


def build_cusip_ticker_map(
    panel_path: Path,
    *,
    start_date,
    end_date,
    min_breadth: int = 10,
    api_key: str | None = None,
    out_path: Path = DEFAULT_MAP_PATH,
    sp500_history_path: Path | None = None,
    checkpoint_path: Path | None = Path("data/processed/cusip_ticker_map_progress.csv"),
) -> pd.DataFrame:
    """CLI entry point body: chain candidate_cusips -> fetch_openfigi_mappings
    -> write the committed CSV (resolved rows only). Prints an audit of
    unresolved CUSIPs and, if sp500_history_path is given, the S&P-side
    coverage check -- both counted, never silently dropped.

    checkpoint_path defaults to a gitignored data/processed/ file (rebuildable
    working state, not a committed reference table) so a re-run after a
    transient network failure resumes instead of re-querying from scratch."""
    panel = pd.read_parquet(panel_path, columns=["CUSIP", "CIK", "FILING_DATE"])
    candidates = sorted(candidate_cusips(panel, start_date=start_date, end_date=end_date, min_breadth=min_breadth))
    print(f"{len(candidates):,} candidate CUSIPs (min_breadth={min_breadth}) out of "
          f"{panel['CUSIP'].nunique():,} distinct CUSIPs in the panel")

    mapping_df = fetch_openfigi_mappings(candidates, api_key=api_key, checkpoint_path=checkpoint_path)
    resolved = mapping_df[mapping_df["RESOLVED"]][["CUSIP", "TICKER", "NAME"]].reset_index(drop=True)
    unresolved = mapping_df[~mapping_df["RESOLVED"]]
    print(f"{len(resolved):,} resolved, {len(unresolved):,} unresolved "
          f"({len(unresolved) / len(mapping_df):.1%})")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    resolved.to_csv(out_path, index=False)
    print(f"wrote {out_path}")

    if sp500_history_path is not None:
        coverage = sp500_ticker_coverage(resolved, sp500_history_path)
        print(f"S&P-side coverage: {coverage['n_missing']:,}/{coverage['n_ever_sp500_tickers']:,} "
              f"({coverage['frac_missing']:.1%}) ever-S&P-500 tickers have no resolved CUSIP")
        if coverage["missing_tickers"]:
            print(f"missing: {coverage['missing_tickers']}")

    return resolved


def load_cusip_ticker_map(
    map_path: Path = DEFAULT_MAP_PATH, overrides_path: Path = DEFAULT_OVERRIDES_PATH
) -> pd.DataFrame:
    """Load the committed crosswalk, with cusip_overrides.csv layered on top
    (overrides fully supersede the base map for any CUSIP they cover).
    Raises a clear error if the committed map is missing -- fail loudly
    rather than run the pipeline on an empty universe.

    A CUSIP can appear in the overrides file more than once, deliberately:
    OpenFIGI resolves a CUSIP to its CURRENT ticker only, so a company that
    renamed (e.g. Facebook -> Meta in 2022, same CUSIP 30303M102, ticker
    FB -> META) resolves cleanly to "META" but never to the retired "FB"
    label a company's *older* S&P 500 membership rows still use. Both
    ticker rows need to exist in the crosswalk for universe.sp500_members()'s
    ticker -> CUSIP reverse lookup to work across a company's full rename
    history, not just its current name -- so overrides are NOT deduped
    against each other by CUSIP, only against the base map.
    """
    if not map_path.exists():
        raise FileNotFoundError(
            f"{map_path} does not exist. Build it once with "
            f"`python -m src.mapping --build` (requires network access and, "
            f"optionally, an OPENFIGI_API_KEY)."
        )
    # dtype=str for CUSIP: a leading-zero CUSIP like "037833100" would
    # otherwise be read as an integer and lose its leading zero, breaking
    # every downstream lookup keyed on the original 9-character string.
    mapping_df = pd.read_csv(map_path, dtype={"CUSIP": str})
    if overrides_path.exists():
        overrides_df = pd.read_csv(overrides_path, dtype={"CUSIP": str})
        overrides_df = overrides_df[["CUSIP", "TICKER", "NAME"]]
        mapping_df = mapping_df[~mapping_df["CUSIP"].isin(overrides_df["CUSIP"])]
        mapping_df = pd.concat([mapping_df, overrides_df], ignore_index=True)
    return mapping_df.reset_index(drop=True)


def cusip_to_ticker(cusips: pd.Series, mapping: pd.DataFrame) -> pd.Series:
    """Map a Series of CUSIPs to tickers; unmapped CUSIPs come back as NaN.

    Forward (CUSIP -> ticker) lookup wants exactly one canonical answer per
    CUSIP, unlike sp500_members()'s reverse ticker -> CUSIP direction, which
    deliberately allows multiple ticker aliases per CUSIP (see
    load_cusip_ticker_map) -- dedup defensively here (keep="last", so a
    cusip_overrides.csv alias row wins over the base map's row for the same
    CUSIP) rather than let a non-unique index silently break .map().
    """
    lookup = mapping.drop_duplicates(subset="CUSIP", keep="last").set_index("CUSIP")["TICKER"]
    return cusips.map(lookup)


def unmapped_summary(cusips: pd.Series, mapping: pd.DataFrame) -> dict:
    """Never silently drop unmapped CUSIPs -- summarize them with a count."""
    tickers = cusip_to_ticker(cusips, mapping)
    unmapped_mask = tickers.isna()
    n_total = len(cusips)
    n_unmapped = int(unmapped_mask.sum())
    return {
        "n_total": n_total,
        "n_unmapped": n_unmapped,
        "frac_unmapped": n_unmapped / n_total if n_total else 0.0,
        "top_unmapped": cusips[unmapped_mask].value_counts().head(10).to_dict(),
    }


def main() -> None:
    import argparse
    import os

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build", action="store_true", help="Run the one-time OpenFIGI crosswalk build.")
    parser.add_argument("--panel", default="data/processed/13f_panel_full.parquet")
    parser.add_argument("--start-date", default="2013-01-01")
    parser.add_argument("--end-date", default=pd.Timestamp.today().strftime("%Y-%m-%d"))
    parser.add_argument("--min-breadth", type=int, default=10)
    parser.add_argument("--sp500-history", default="data/reference/sp500_history.csv")
    parser.add_argument("--out", default=str(DEFAULT_MAP_PATH))
    parser.add_argument(
        "--checkpoint", default="data/processed/cusip_ticker_map_progress.csv",
        help="Resumable progress file -- a re-run after a transient failure skips CUSIPs already fetched here.",
    )
    parser.add_argument(
        "--fresh", action="store_true",
        help="Ignore/delete any existing checkpoint and query every candidate CUSIP from scratch.",
    )
    args = parser.parse_args()

    if not args.build:
        parser.error("pass --build to run the crosswalk build")

    checkpoint_path = Path(args.checkpoint)
    if args.fresh and checkpoint_path.exists():
        checkpoint_path.unlink()

    sp500_history_path = Path(args.sp500_history)
    build_cusip_ticker_map(
        Path(args.panel),
        start_date=args.start_date,
        end_date=args.end_date,
        min_breadth=args.min_breadth,
        api_key=os.environ.get("OPENFIGI_API_KEY"),
        out_path=Path(args.out),
        sp500_history_path=sp500_history_path if sp500_history_path.exists() else None,
        checkpoint_path=checkpoint_path,
    )


if __name__ == "__main__":
    main()
