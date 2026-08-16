"""Universe definition: which equities and which institutional holders count.

Two axes, matching the case-study prompt's "universe of institutional
holders" and equity scope:

- Equity universe: S&P 500, point-in-time (not today's list -- avoids
  survivorship bias). Membership history sourced once, offline, from the
  free fja05680/sp500 GitHub dataset (MIT licensed) and committed as
  data/reference/sp500_history.csv -- no network at pipeline runtime.
- Holder universe: all 13F filers minus a small, disclosed, curated
  exclusion list of known passive/index managers (data/reference/
  passive_manager_ciks.csv). Index funds' holdings are mechanical, not a
  positioning signal.

sp500_universe_breadth() composes both with pit.breadth() to produce the
exact [CUSIP, breadth] shape factor.py (Phase 3) consumes -- point-in-time
correct by construction, since it's built entirely on pit.breadth()'s
existing no-lookahead guarantee without modifying it.
"""

import functools
import io
from pathlib import Path

import pandas as pd

from src import pit
from src._http import get_with_retry

SP500_HISTORY_URL = (
    "https://raw.githubusercontent.com/fja05680/sp500/master/"
    "S%26P%20500%20Historical%20Components%20%26%20Changes%20(Updated).csv"
)
USER_AGENT = "SchonfeldCaseStudy gabriel.christensen2019@gmail.com"

DEFAULT_HISTORY_PATH = Path("data/reference/sp500_history.csv")
DEFAULT_PASSIVE_CIKS_PATH = Path("data/reference/passive_manager_ciks.csv")


def build_sp500_history(
    *, start_date: str, out_path: Path = DEFAULT_HISTORY_PATH, url: str = SP500_HISTORY_URL
) -> pd.DataFrame:
    """One-time build: download fja05680/sp500's MIT-licensed history CSV,
    trim to [start_date, latest available], dedup consecutive rows with
    identical membership (each row is a cumulative snapshot, not a delta --
    a run of identical rows carries no extra point-in-time information,
    since sp500_members() only ever needs the most recent row on or before
    a given date), and commit the result verbatim in the same date,tickers
    shape. No network needed at pipeline runtime after this runs once.
    """
    print(f"Fetching S&P 500 membership history from {url} ...")
    resp = get_with_retry(url, timeout=30, headers={"User-Agent": USER_AGENT})
    history = pd.read_csv(io.StringIO(resp.text))
    history["date"] = pd.to_datetime(history["date"])
    history = history[history["date"] >= pd.Timestamp(start_date)].reset_index(drop=True)
    # Drop consecutive rows with identical membership -- lossless for
    # as-of-date lookups, since only the change dates carry information.
    changed = history["tickers"] != history["tickers"].shift(1)
    history = history[changed].reset_index(drop=True)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    history.to_csv(out_path, index=False)
    print(f"{len(history):,} membership-change rows from {history['date'].min().date()} "
          f"to {history['date'].max().date()} -> {out_path}")
    return history


@functools.lru_cache(maxsize=1)
def _load_sp500_history(history_path: Path) -> pd.DataFrame:
    """Cached read+parse of sp500_history.csv, keyed only on history_path
    (a Path -- hashable, unlike the `mapping` DataFrame sp500_members also
    takes, which is why this is split out as its own cached helper rather
    than caching sp500_members directly). The file is immutable within a
    single run, but sp500_members was re-reading and re-parsing it from
    disk on every one of the ~300+ calls a full backtest grid makes.

    Returns a shared object across every caller -- treat as read-only,
    never mutate in place.
    """
    history = pd.read_csv(history_path)
    history["date"] = pd.to_datetime(history["date"])
    return history


def build_ticker_to_cusip(mapping: pd.DataFrame) -> pd.Series:
    """Reverse ticker -> CUSIP lookup (Series indexed by TICKER) built from
    `mapping` (mapping.load_cusip_ticker_map()'s output). Ticker->CUSIP
    collisions (distinct tickers occasionally resolving to the same CUSIP,
    e.g. share class noise) are resolved deterministically (sorted, first
    wins).

    Pulled out of sp500_members() so callers that invoke it repeatedly
    (e.g. backtest.py's ~300+-call grid) can build this once and pass it
    in via sp500_members'/sp500_universe_breadth's `ticker_to_cusip`
    parameter, instead of rebuilding it from `mapping` on every call --
    `mapping` is a DataFrame, and thus unhashable, so this can't just be
    wrapped in functools.lru_cache the way _load_sp500_history is.
    """
    return (
        mapping.dropna(subset=["TICKER"])
        .sort_values("CUSIP")
        .drop_duplicates(subset="TICKER", keep="first")
        .set_index("TICKER")["CUSIP"]
    )


def sp500_members(
    as_of_date,
    mapping: pd.DataFrame,
    *,
    history_path: Path = DEFAULT_HISTORY_PATH,
    ticker_to_cusip: pd.Series | None = None,
) -> set[str]:
    """S&P 500 member CUSIPs as of as_of_date.

    Takes the last row of sp500_history.csv with date <= as_of_date
    (cumulative-snapshot format, not a delta -- confirmed against the real
    upstream file), comma-splits the ticker list, and reverse-maps
    ticker -> CUSIP through `ticker_to_cusip` (built from `mapping` via
    build_ticker_to_cusip() if not supplied -- see that function's
    docstring for why a caller making many calls should build it once and
    pass it in instead).

    Deliberate, disclosed design choice: keyed on as_of_date (the
    point-in-time trade/decision date), not period_of_report -- consistent
    with pit.breadth()'s own as_of_date axis, and the correct choice for
    "was this name actually investable at the moment we'd act on the
    signal."

    Unmapped tickers (no resolved CUSIP) and ticker->CUSIP collisions
    (distinct tickers occasionally resolving to the same CUSIP, e.g. share
    class noise) are resolved deterministically (sorted, first wins) and
    silently excluded from the returned set -- callers who need the exact
    unmapped count should cross-check against mapping.unmapped_summary
    separately.
    """
    as_of_date = pd.Timestamp(as_of_date)
    history = _load_sp500_history(history_path)
    eligible = history[history["date"] <= as_of_date]
    if eligible.empty:
        raise ValueError(
            f"as_of_date {as_of_date.date()} predates the earliest sp500_history.csv "
            f"row ({history['date'].min().date()})"
        )
    row = eligible.iloc[-1]
    tickers = sorted(t.strip() for t in row["tickers"].split(","))

    if ticker_to_cusip is None:
        ticker_to_cusip = build_ticker_to_cusip(mapping)
    cusips = {ticker_to_cusip[t] for t in tickers if t in ticker_to_cusip.index}
    return cusips


def passive_manager_ciks(path: Path = DEFAULT_PASSIVE_CIKS_PATH) -> set[str]:
    """Curated exclusion list of known passive/index managers (CIK). Re-fills
    to 10 digits defensively so a hand-edited CSV can't silently break
    exclusion via a padding mismatch."""
    df = pd.read_csv(path, dtype={"CIK": str})
    return set(df["CIK"].str.strip().str.zfill(10))


def sp500_universe_breadth(
    panel: pd.DataFrame,
    period_of_report,
    as_of_date,
    mapping: pd.DataFrame,
    *,
    passive_ciks: set[str] | None = None,
    history_path: Path = DEFAULT_HISTORY_PATH,
    ticker_to_cusip: pd.Series | None = None,
) -> pd.DataFrame:
    """Point-in-time breadth, restricted to the S&P 500 universe as of
    as_of_date, with passive managers excluded. Exact [CUSIP, breadth] shape
    factor.py consumes. Note that passive_ciks is an optional parameter.
    """
    raw = pit.breadth(panel, period_of_report, as_of_date, exclude_ciks=passive_ciks)
    universe = sp500_members(
        as_of_date, mapping, history_path=history_path, ticker_to_cusip=ticker_to_cusip,
    )
    return raw[raw["CUSIP"].isin(universe)].reset_index(drop=True)


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--build-sp500-history", action="store_true",
        help="Download and commit the point-in-time S&P 500 membership history.",
    )
    parser.add_argument("--start-date", default="2012-01-01")
    parser.add_argument("--out", default=str(DEFAULT_HISTORY_PATH))
    args = parser.parse_args()

    if not args.build_sp500_history:
        parser.error("pass --build-sp500-history to run the build")

    build_sp500_history(start_date=args.start_date, out_path=Path(args.out))


if __name__ == "__main__":
    main()
