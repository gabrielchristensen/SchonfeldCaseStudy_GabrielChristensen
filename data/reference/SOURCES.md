# Reference data sources

Every file in this directory is either a one-time build-script output or a
manually curated table. None require network access at pipeline runtime --
only the build scripts noted below do, and only when regenerating a file.

## `sp500_history.csv`

- **Source**: [fja05680/sp500](https://github.com/fja05680/sp500),
  `S&P 500 Historical Components & Changes (Updated).csv`.
- **License**: MIT (community-maintained, sourced from Andreas Clenow's
  "Trading Evolved" dataset for 1996-2019 and manually updated against
  Wikipedia's S&P 500 changes table since). Not an official S&P Dow Jones
  Indices product -- treat as reasonably trustworthy for research, not
  audit-grade.
- **Retrieved**: 2026-08-15.
- **Transformation**: trimmed to rows on/after 2012-01-01 (a buffer before
  SEC's 13F structured data begins in May 2013), and deduplicated so only
  rows where membership actually changed vs. the prior row are kept (the
  upstream file is a cumulative snapshot per row, not a delta -- a run of
  identical rows carries no extra information for an as-of-date lookup).
  287 rows, covering 2012-01-03 to 2026-06-30.
- **Rebuild**: `python -m src.universe --build-sp500-history --start-date 2012-01-01`.

## `cusip_ticker_map.csv`

- **Source**: [OpenFIGI](https://www.openfigi.com/) mapping API
  (`POST https://api.openfigi.com/v3/mapping`, `idType=ID_CUSIP`), queried
  unauthenticated (no registered API key used for this build).
- **License/terms**: OpenFIGI is Bloomberg's free, open symbology service --
  explicitly free of charge with no cost-recovery or redistribution
  restriction for mapping results (CUSIP as query input, ticker/FIGI as
  output).
- **Retrieved**: 2026-08-15.
- **Candidate selection**: not every CUSIP ever seen in the ingested panel
  was queried -- `mapping.candidate_cusips()` pre-filters to CUSIPs with
  cumulative breadth (distinct filers) >= 100 across the 2013-2026 backtest
  window (32,342 CUSIPs clear >= 10; 11,711 clear >= 100), since S&P 500
  names are overwhelmingly held by many filers while the long tail of
  thinly-held small-caps/private placements is not. `min_breadth=100` was
  chosen over the initially-planned 10 because of a real, empirically-
  discovered rate-limit constraint (next bullet) -- see the S&P-side
  coverage check output (printed by the build, also summarized here once
  run) for confirmation that this threshold didn't drop any real S&P 500
  constituent.
- **Batch size, verified empirically (not assumed from docs)**: an
  unauthenticated OpenFIGI request is capped at exactly 10 mapping jobs
  ("Request may only contain 10 mapping jobs." on an 11th) -- not the 100
  originally planned, which was a documented-but-unverified assumption for
  the *authenticated* case only. This build ran unauthenticated (no
  registered API key), so it used batches of 10 at ~25 requests/minute,
  making the full 32,342-candidate build (~2+ hours) impractical within
  this session -- `min_breadth` was raised to 100 to bring the candidate
  count down to a ~49-minute unauthenticated run instead. A registered
  `OPENFIGI_API_KEY` (free, instant registration at openfigi.com) would
  restore both the 100-job batch size and the lower `min_breadth=10`
  threshold as practical -- see the **Rebuild** command below.
- **Selection rule per CUSIP, corrected after an empirical bug was found**:
  a single CUSIP query returns every global listing (ADRs, currency-
  denominated variants, multiple exchanges) for that security. The first
  build used `exchCode == "US"`, which correctly identified the primary
  listing for AAPL/MSFT (258/279 results respectively) but is **not
  universal**: a real query for ExxonMobil's CUSIP (30231G102) has *no*
  `exchCode == "US"` row at all (the real "XOM" listings use "UZ"/"OU"/"QU"
  instead) -- the naive filter's fallback silently picked an unrelated
  Colombian "EXMOC" listing instead, and the same failure mode affected
  ~27% of the S&P-side coverage check on the first build (`CB`, `HON`,
  `MDT`, `K`, `BK`, `XOM`, and more all missing despite being obviously
  large, widely-held current S&P 500 names). Fixed by grouping results by
  `compositeFIGI` (every venue/currency variant of one underlying security
  shares a compositeFIGI) and taking the **largest group** -- verified this
  both still picks the correct AAPL/MSFT listing and correctly picks "XOM"
  (3-row group) over "EXMOC" (2-row group). See `mapping._choose_primary_listing`.
- **Resilience, added after two real build failures**: a ~49-minute
  unauthenticated run hit a transient "No route to host" network failure
  partway through with no recovery, losing all progress; a second run then
  crashed on a `pd.concat` dtype bug (`RESOLVED` silently demoted from bool
  to object, making `~mapping_df["RESOLVED"]` do Python bitwise-invert
  instead of logical NOT). Both fixed: `_post_with_retry` now retries
  connection errors/timeouts/429/5xx with exponential backoff, and
  `fetch_openfigi_mappings` accepts a `checkpoint_path` that persists
  progress incrementally so a restart resumes instead of re-querying from
  scratch (mirrors `ingest.py`'s `checkpoint_dir` for `--full` mode).
- **Residual S&P-side coverage gap, structural not a bug**: after all
  fixes, 283 of 811 (34.9%) ever-S&P-500 tickers still have no resolved
  CUSIP. Root cause, confirmed by inspection (not assumed): OpenFIGI
  resolves a CUSIP to its **current** ticker only. Over the 2012-2026
  window, a large fraction of the "missing" names were actually acquired,
  merged, or renamed (e.g. `MON`/Monsanto->Bayer 2018, `EMC`->Dell 2016,
  `RHT`/Red Hat->IBM 2019, `UTX`+`RTN`->RTX 2020, `FB`->Meta/`META` 2022)
  -- for genuinely-acquired names there is no current independent ticker to
  resolve to at all (the same survivorship-bias-adjacent issue this plan
  already disclosed for price data, now showing up at the mapping layer
  too); for pure renames (same CUSIP, new ticker) the current ticker
  resolves fine but the retired label used in older S&P 500 membership
  rows does not, unless patched via `cusip_overrides.csv` (see below).
  Not comprehensively patched -- would mean re-deriving a full historical
  ticker genealogy, out of scope for this case study. Disclosed here and in
  the memo rather than silently accepted.
- **Rebuild**: `python -m src.mapping --build --panel data/processed/13f_panel_full.parquet
  --start-date 2013-01-01 --end-date <today> --min-breadth 10
  --sp500-history data/reference/sp500_history.csv`, with `OPENFIGI_API_KEY`
  set in the environment to unlock the faster authenticated batch
  size/rate limit and make the full `min_breadth=10` universe practical.
  Add `--fresh` to ignore an existing checkpoint and start over.

## `cusip_overrides.csv`

- **Source**: manual curation, seeded from `build_cusip_ticker_map`'s
  unresolved-CUSIP and S&P-side coverage-gap audit output (tickers/CUSIPs
  a plain OpenFIGI query couldn't resolve -- ticker changes, spin-offs,
  M&A, or ambiguous multi-listing cases).
- **Layered on top of `cusip_ticker_map.csv`** by `mapping.load_cusip_ticker_map()`
  -- overrides fully supersede the base map's row(s) for any CUSIP they
  cover. A CUSIP can appear more than once in this file *deliberately*: see
  the META/FB and BNY/BK pairs below.
- **Two entries are real historical CUSIP changes** from corporate actions
  (verified 2026-08-15): the pre-2026 Honeywell CUSIP (438516106,
  superseded by a reverse-split-driven new CUSIP 438516205) and the
  pre-2015 Medtronic Inc CUSIP (585055106, superseded when Medtronic
  redomiciled to Ireland as Medtronic plc, new CUSIP G5960L103). Both old
  CUSIPs map to the same ticker as their current CUSIP.
- **Two entries are ticker-rename pairs, same CUSIP** (verified 2026-08-15):
  Facebook/Meta (CUSIP 30303M102: current ticker `META`, pre-2022 ticker
  `FB`) and Bank of New York Mellon/BNY (CUSIP 064058100: current ticker
  `BNY`, pre-2026-05-21 ticker `BK`). Both the current and retired ticker
  are listed as separate rows for the same CUSIP -- needed because
  `universe.sp500_members()` looks up historical S&P 500 membership rows
  by whatever ticker label was in use *at that date*, not the company's
  current name. These two are illustrative, not exhaustive -- see the
  "Residual S&P-side coverage gap" note above.
- This crosswalk is a static snapshot, not a full CUSIP/ticker genealogy --
  disclosed as a limitation here and patched via override only for names
  material enough to matter within this session's time budget. See each
  row's `REASON` for the verification detail and sourcing.

## `passive_manager_ciks.csv`

- **Source**: manual curation, cross-verified against real
  `FILINGMANAGER_NAME` values in `data/processed/13f_panel_sample.parquet`
  (2026 Q1-Q2 filings) rather than trusted from external CIK lookups alone.
  Initial external candidate CIKs for Vanguard, BlackRock, and Northern
  Trust (sourced from SEC EDGAR company search) turned out to be stale --
  those entities now file 13F under different CIKs than commonly-cited
  older ones, discovered only by checking which CIKs actually appear with
  material AUM in real, recent ingested data. See the `NOTES` column of
  the file itself for the verification detail behind each row.
- **Retrieved/verified**: 2026-08-15.
- **Deliberately not exhaustive**: this is a small, curated list of the
  largest, most clearly mechanical index-tracking filers, not an attempt
  to classify every passive dollar in the panel (a "trying to do
  everything" failure mode the case-study prompt explicitly warns
  against). Disclosed as a scope cut in the memo.
