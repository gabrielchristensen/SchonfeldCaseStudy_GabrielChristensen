# Phase 2 Implementation Report — CUSIP Mapping & Universe Definition

## Scope of Phase 2

Per the pipeline design (`ingest → pit → mapping → universe → factor → backtest → report`), Phase 2 covers **`src/mapping.py`** (CUSIP → ticker crosswalk) and **`src/universe.py`** (point-in-time S&P 500 membership + passive-manager exclusion), plus a small, approved reopening of Phase 1's `src/ingest.py` to add COVERPAGE parsing. Phase 1's `breadth()` primitive (distinct-filer count per CUSIP) was already point-in-time correct but unusable for a factor: there was no way to go from a CUSIP to a tradeable equity, and no definition of "the universe" on either axis the case study asks for (which equities, which institutional holders).

Status: **complete**. 68/68 tests pass (up from 39 at the start of this phase), and the composed end-to-end contract (`universe.sp500_universe_breadth()`) has been validated against real ingested 13F data, not just synthetic fixtures. Four real bugs were found and fixed while building this against live external services — this report documents each one, since discovering and fixing them under real conditions is a direct demonstration of the "data engineering care" the case study evaluates on.

---

## `src/ingest.py` — COVERPAGE addendum

Phase 1 parsed only `SUBMISSION` and `INFOTABLE`. Phase 2 adds `COVERPAGE`, approved as a small, additive reopening of already-shipped code because it directly answers two evaluation criteria the pipeline previously had zero way to address: filer identity verification and confidential-treatment-request detection.

### Verified real schema
`COVERPAGE.tsv`'s columns were read directly off real downloaded zips (`data/raw/13f/2013q2_form13f.zip` and `data/raw/13f/01mar2026-31may2026_form13f.zip`), not assumed:
```
ACCESSION_NUMBER  REPORTCALENDARORQUARTER  ISAMENDMENT  AMENDMENTNO  AMENDMENTTYPE
CONFDENIEDEXPIRED  DATEDENIEDEXPIRED  DATEREPORTED  REASONFORNONCONFIDENTIALITY
FILINGMANAGER_NAME  FILINGMANAGER_STREET1  FILINGMANAGER_STREET2  FILINGMANAGER_CITY
FILINGMANAGER_STATEORCOUNTRY  FILINGMANAGER_ZIPCODE  REPORTTYPE  FORM13FFILENUMBER
CRDNUMBER  SECFILENUMBER  PROVIDEINFOFORINSTRUCTION5  ADDITIONALINFORMATION
```
`_COVERPAGE_COLUMNS` selects only `ACCESSION_NUMBER`, `FILINGMANAGER_NAME`, `CONFDENIEDEXPIRED`, `DATEDENIEDEXPIRED`, `DATEREPORTED`, `REASONFORNONCONFIDENTIALITY` — deliberately excluding `ISAMENDMENT`/`AMENDMENTNO`/`AMENDMENTTYPE` (would quietly contradict `pit.py`'s already-disclosed decision not to parse amendment-type semantics) and `PROVIDEINFOFORINSTRUCTION5` (verified against real rows to be an unrelated restatement footnote, not a confidentiality signal despite its name).

### Confidentiality detection, verified against real rows
`CONFDENIEDEXPIRED` is `'Y'` / `'N'` / blank. Blank means no confidential-treatment request exists on that filing; `'N'` answers a "was it denied/expired?" question negatively (confidentiality still requested, unresolved); `'Y'` means the confidential period ended and the filing now discloses what was withheld. Real example: accession `0001164691-13-000058`, filer Alyeska Investment Group, `REASONFORNONCONFIDENTIALITY="Confidential Treatment Expired"`. `_clean_coverpage` sets `CONFIDENTIAL_TREATMENT_FLAG = (CONFDENIEDEXPIRED == "Y")`.

Mechanically important: `pit.py`'s existing "latest filing wins, no backfill" logic **already handles confidential holdings correctly with zero changes** — an omitted position simply isn't in `INFOTABLE` until the manager later files the real disclosure at its own real `FILING_DATE`. COVERPAGE's job is detection/disclosure, not a change to the no-lookahead mechanism.

### Left join, not inner
```python
merged = submission.merge(infotable, on="ACCESSION_NUMBER", how="inner")
merged = merged.merge(coverpage, on="ACCESSION_NUMBER", how="left")
```
A COVERPAGE parsing gap must never silently drop otherwise-valid INFOTABLE holdings rows — a missing COVERPAGE row surfaces as `NaN` manager/confidentiality columns, not a lost row.

### Bundled fix: CIK zero-padding
`_clean_submission`'s CIK cleaning became `.str.strip().str.zfill(10)`. **Real bug found while building this phase**: the ingested panel had the same filer appearing both as `"1349434"` and `"0001349434"`. Since `passive_manager_ciks()` exclusion does exact-string matching, this would have silently failed to exclude that filer under whichever padding convention wasn't listed — a real leak, caught before it mattered.

### Validation against real data
Re-ran `python -m src.ingest` (3-dataset sample) after the addendum: identical row/filing counts to the pre-COVERPAGE run (3,868,025 rows / 14,353 filings — the left join dropped nothing), `FILINGMANAGER_NAME` had zero nulls, all CIKs were exactly 10 characters, and the confidentiality flag rate was plausible (0.49% of filings, 70/14,353).

---

## `src/mapping.py` — CUSIP → ticker crosswalk

Split into an offline/network layer (run once, by a human, via `python -m src.mapping --build`) and a pure/runtime layer (no network, used by the pipeline and tested directly) — mirroring `ingest.py`'s `download_dataset` (network) vs. `_clean_*` (pure) split.

### Why CUSIP → ticker, not ticker → CUSIP
OpenFIGI's documented, well-precedented use case is CUSIP as query input, ticker/FIGI as output — CUSIP is a licensed identifier Bloomberg can accept as input but has redistribution restrictions on returning as output, so the direction matters and was chosen deliberately.

### Efficiency: don't map every CUSIP ever seen
The full ingested panel has 151,241 distinct CUSIPs. `candidate_cusips(panel, start_date, end_date, min_breadth)` pre-filters to CUSIPs with cumulative breadth (distinct filers) above a threshold before any network call — at `min_breadth=10`, 32,342 CUSIPs clear the bar; at `min_breadth=100`, 11,711 do. S&P 500 names are held by thousands of filers each (AAPL's CUSIP alone has 6,000+ distinct filers in a single recent quarter); the long tail of thinly-held small-caps and private placements is nowhere near the S&P 500 and isn't worth querying.

### Real bug #1 — batch size assumption was wrong
The original plan assumed OpenFIGI's documented 100-jobs-per-request limit applied unauthenticated. A real build attempt returned `413 Payload Too Large: "Request may only contain 10 mapping jobs."` on the 11th job. Verified empirically via direct `curl` testing: **unauthenticated is capped at exactly 10 jobs/request**; 100 requires a registered API key. `_BATCH_SIZE_UNAUTHENTICATED = 10` / `_BATCH_SIZE_AUTHENTICATED = 100`, selected dynamically in `fetch_openfigi_mappings` based on whether `api_key` is supplied. This single correction changed the unauthenticated runtime for 32,342 candidates from a planned ~15 minutes to a projected ~2+ hours, which is why the real build ran at `min_breadth=100` (11,711 candidates, ~49 minutes) instead of the originally planned 10.

### Real bug #2 — the primary-listing filter wasn't universal
A single CUSIP query returns every global listing for that security (ADRs, currency variants, every exchange) — verified directly: AAPL's CUSIP returned 258 results, MSFT's 279. The first implementation filtered to `exchCode == "US"`, which correctly picked AAPL and MSFT's primary listing. It does **not** generalize: a real query for ExxonMobil's CUSIP (`30231G102`) returns 11 results across 4 `compositeFIGI` groups, and **none** is tagged `exchCode == "US"` — the real "XOM" listings use exchange codes `UZ`/`OU`/`QU` instead. The naive filter's fallback to `data[0]` silently picked an unrelated Colombian listing ("EXMOC"). This wasn't a one-off: it affected ~27% of the S&P-side coverage check on the first build (`CB`, `HON`, `MDT`, `K`, `BK`, `XOM` and more — all obviously large, current S&P 500 names — showing as unresolved).

**Fix**: `_choose_primary_listing` groups results by `compositeFIGI` (every venue/currency variant of one underlying security shares a compositeFIGI) and takes the **largest group**, preferring the `exchCode == "US"` row within that group if present. Verified this both still picks AAPL/MSFT correctly and correctly picks "XOM" (3-row group) over "EXMOC" (2-row group).

### Real bug #3 — no resilience to transient failures
The first full ~49-minute unauthenticated build hit a real `OSError: [Errno 113] No route to host` partway through, with zero recovery — the entire run's progress was lost. **Fix**: `_post_with_retry` now retries connection errors, timeouts, `429`, and `5xx` with exponential backoff (5s, 10s, 20s, 40s, up to 5 attempts). `fetch_openfigi_mappings` additionally accepts a `checkpoint_path`: each batch's results are appended to it immediately, and any CUSIP already present there is skipped on a re-run — mirroring `ingest.py`'s `checkpoint_dir` pattern for `--full` mode. `build_cusip_ticker_map` defaults `checkpoint_path` to a gitignored `data/processed/` file (working state, not a committed reference table).

### Real bug #4 — a `pd.concat` dtype corruption
Even with retry resilience, the very next build attempt crashed with a bizarre `KeyError: "None of [Index([-1, -1, -2, ...])] are in the [columns]"`. Root cause: `fetch_openfigi_mappings` always creates a placeholder `already` DataFrame for the "no checkpoint yet" case; when constructed as a bare `pd.DataFrame(columns=[...])`, its `RESOLVED` column defaults to `object` dtype. Concatenating that empty `object`-dtype column with the real `bool`-dtype results column silently demoted the combined column to `object`. `~` (invert) on an `object` array of Python `bool`s then executed Python's integer bitwise-NOT (`bool` is an `int` subclass: `~True == -2`, `~False == -1`) instead of logical negation — and pandas then tried to use those `-1`/`-2` values as **column labels** in `mapping_df[~mapping_df["RESOLVED"]]`, not a boolean mask.

**Fix**: the placeholder frame is now constructed with explicit per-column dtypes (`pd.Series(dtype=bool)` for `RESOLVED`), plus a defensive `.astype(bool)` cast on the final combined result regardless of how the dtype ended up. Caught with a regression test (`test_fetch_openfigi_mappings_resolved_column_is_real_bool_dtype`) that reproduces the exact crash path — a fresh call with no checkpoint, then the exact `~result["RESOLVED"]` operation that crashed in production.

### The residual coverage gap — structural, not a bug
After all four fixes, the real build against the full panel (11,711 candidates, `min_breadth=100`) resolved 7,998 CUSIPs (68.3%). Cross-checked from the S&P side (`sp500_ticker_coverage`): 283 of 811 (34.9%) tickers that were *ever* an S&P 500 constituent across the 2012–2026 window still have no resolved CUSIP.

This was investigated, not assumed to be a residual bug. Root cause, confirmed by direct inspection: **OpenFIGI resolves a CUSIP to its current ticker only.** Two distinct sub-cases:
- **Genuine delisting via M&A**: `MON` (Monsanto → Bayer, 2018), `EMC` (→ Dell, 2016), `RHT` (Red Hat → IBM, 2019), `UTX`+`RTN` (merged into RTX, 2020), and many more — the underlying company no longer trades independently, so there is no current ticker to resolve to at all. This is the same class of problem the original master plan already disclosed for price data (a survivorship-bias-adjacent gap), now showing up at the mapping layer too.
- **Pure ticker rename, same CUSIP**: confirmed concretely for two names. Facebook's CUSIP (`30303M102`) resolves to `META` (its 2022 rename), never to the retired `FB` label that older S&P 500 membership rows still use. Bank of New York Mellon's CUSIP (`064058100`) resolves to `BNY`, never `BK` — which it traded under until May 21, 2026, three months before this build.

Comprehensively patching this would mean reconstructing a full historical CUSIP/ticker genealogy across 14 years of corporate actions — explicitly out of scope for this case study (a "trying to do everything" failure mode). Instead, `cusip_overrides.csv` demonstrates the fix pattern with two real corporate-action entries and two real ticker-alias pairs (see below), and the gap is disclosed here and in `data/reference/SOURCES.md` rather than silently accepted.

---

## `src/universe.py` — universe definition

### S&P 500 point-in-time membership
`sp500_members(as_of_date, mapping, history_path)` takes the last row of `sp500_history.csv` with `date <= as_of_date` (a cumulative snapshot per row, not a delta — confirmed against the real upstream file), comma-splits the ticker list, and reverse-maps ticker → CUSIP through the loaded crosswalk. Deliberately keyed on `as_of_date` (the point-in-time trade/decision date), not `period_of_report` — consistent with `pit.breadth()`'s own axis, and correct for "was this name actually investable at the moment we'd act on the signal." Raises `ValueError` if `as_of_date` predates the history file's earliest row, rather than silently returning an empty/wrong universe.

`data/reference/sp500_history.csv` is built once, offline, via `python -m src.universe --build-sp500-history` from the free [fja05680/sp500](https://github.com/fja05680/sp500) GitHub dataset (MIT licensed). The raw upstream file is 5.5MB with near-daily redundant snapshot rows; `build_sp500_history` trims to dates on/after 2012-01-01 (a buffer before SEC's May 2013 structured-data start) and **drops consecutive rows with identical membership** — lossless for as-of-date lookups, since only the change dates carry information, shrinking the committed file to 287 rows / 585KB.

### Passive-manager exclusion
`passive_manager_ciks(path)` reads a small curated CSV and re-zfills CIKs to 10 digits defensively (so a hand-edited file can't silently break exclusion via a padding mismatch — the exact bug class fixed in `ingest.py`). The 9 curated entries were **not** sourced from external CIK lookups alone: an initial pass using SEC EDGAR's company search returned CIKs for Vanguard, BlackRock, and Northern Trust that turned out to be stale — those managers file 13F under different, newer CIKs today. This was only caught by cross-checking candidates against real `FILINGMANAGER_NAME` values in the ingested panel (only possible because of the COVERPAGE addendum above) and ranking by aggregate reported `VALUE`:

| CIK | Manager | Verified aggregate VALUE (2026 Q1–Q2) |
|---|---|---|
| 0002100119 | VANGUARD CAPITAL MANAGEMENT LLC | ~$8.0T |
| 0002012383 | BlackRock, Inc. | ~$5.7T |
| 0000093751 | STATE STREET CORP | ~$2.9T |
| 0002100121 | VANGUARD PORTFOLIO MANAGEMENT LLC | ~$1.9T |
| 0001214717 | GEODE CAPITAL MANAGEMENT, LLC | ~$1.6T |
| 0000073124 | NORTHERN TRUST CORP | ~$757B |
| 0000884546 | CHARLES SCHWAB INVESTMENT MANAGEMENT INC | ~$654B |
| 0000933478 | VANGUARD FIDUCIARY TRUST CO | ~$396B |
| 0001811242 | Vanguard Global Advisers LLC | ~$187B |

Geode (Fidelity's dedicated index-fund sub-adviser) is excluded, but FMR LLC (Fidelity's broader active+passive complex, ~$1.9T) deliberately is **not** — excluding it would remove genuine active-manager signal, not just mechanical index flows. Deliberately small and not exhaustive, matching the "curated exclusion list, not exhaustive classification" scope decision — disclosed as such, not claimed complete.

### `load_cusip_ticker_map` — supporting multiple ticker aliases per CUSIP
Adding the FB/META and BK/BNY override pairs exposed a real design gap: the original override-layering logic deduped strictly by CUSIP (`drop_duplicates(subset="CUSIP", keep="last")`), which would have silently kept only one of two legitimate alias rows for the same CUSIP. Fixed: overrides now **fully supersede** the base map's row(s) for any CUSIP they cover (rather than merge-and-dedupe), so `cusip_overrides.csv` can list a CUSIP more than once deliberately. `cusip_to_ticker`'s forward (CUSIP → ticker) direction still wants exactly one canonical answer per CUSIP, so it dedupes defensively (`keep="last"`) before building its lookup — the two lookup directions have genuinely different uniqueness requirements on the same table, and now both are handled correctly.

### Composition — the Phase 3 contract
```python
def sp500_universe_breadth(panel, period_of_report, as_of_date, mapping, *, passive_ciks=None, history_path=...):
    raw = pit.breadth(panel, period_of_report, as_of_date, exclude_ciks=passive_ciks)
    universe = sp500_members(as_of_date, mapping, history_path=history_path)
    return raw[raw["CUSIP"].isin(universe)].reset_index(drop=True)
```
The exact `[CUSIP, breadth]` shape `factor.py` (Phase 3) will consume — point-in-time correct by construction, built entirely on `pit.breadth()`'s existing no-lookahead guarantee without modifying it.

---

## New `data/reference/` files

| File | Size | Rows | Produced by |
|---|---|---|---|
| `sp500_history.csv` | 585KB | 287 | One-time download + trim of `fja05680/sp500`'s MIT-licensed history, via `python -m src.universe --build-sp500-history` |
| `cusip_ticker_map.csv` | 301KB | 7,998 | One-time `python -m src.mapping --build` (OpenFIGI, resolved candidates only, `min_breadth=100`) |
| `cusip_overrides.csv` | 3.1KB | 6 | Manual curation: 2 real CUSIP-change corporate actions (Honeywell reverse split, Medtronic Ireland redomicile) + 2 real ticker-rename alias pairs (META/FB, BNY/BK) |
| `passive_manager_ciks.csv` | 2.4KB | 9 | Manual curation, cross-verified against real `FILINGMANAGER_NAME` data |
| `SOURCES.md` | 9.7KB | — | Full source URL, license, retrieval date, and transformation for every file above, including all bug/fix narratives |

---

## Test coverage

68 tests, all passing (grown from 39 at the start of this phase):

| File | Tests | What's new in Phase 2 |
|---|---|---|
| `test_ingest.py` | 29 | +6: `_clean_coverpage` (flag logic, date coercion, dedup), left-join-survives-missing-coverpage, end-to-end confidentiality-flag flow, CIK `zfill` |
| `test_mapping.py` | 21 (new file) | `candidate_cusips` filtering; `_choose_primary_listing` correctness (including the real XOM edge case as a fixture); batching at both batch sizes; API-key header; 429 and connection-error retry; checkpoint resume after a simulated failure; the `RESOLVED` dtype regression test; override loading, including the multi-alias-per-CUSIP case; `cusip_to_ticker`; `unmapped_summary`; `sp500_ticker_coverage` |
| `test_universe.py` | 8 (new file) | `sp500_members` as-of-date correctness (including the exact-change-date boundary), out-of-range error, unmapped-ticker exclusion; `passive_manager_ciks` zfill + missing-file error; end-to-end `sp500_universe_breadth` (including passive-CIK exclusion and out-of-universe filtering in one fixture); `build_sp500_history` window-trim + consecutive-duplicate-row dedup |
| `test_pit.py` | 10 | unchanged from Phase 1 |

All new network-touching functions (`fetch_openfigi_mappings`, `build_sp500_history`) are tested via `monkeypatch`, never real network calls in the test suite itself — consistent with the pattern established in Phase 1.

One test-writing mistake worth noting for calibration: an early draft of `test_sp500_universe_breadth_restricts_to_universe_and_excludes_passive` split one CIK's holdings across three different accession numbers on the same day, which `pit._winning_accessions` (correctly) read as amendments superseding each other, dropping two of the three CUSIPs. Not a `pit.py` or `universe.py` bug — a real 13F filing reports every CUSIP a manager holds under one accession number, and the fixture was wrong to model it otherwise. Fixed by consolidating the fixture to one accession per filer.

---

## End-to-end validation against real data

Ran `universe.sp500_universe_breadth()` against the real ingested sample panel (not synthetic fixtures) for `period_of_report='2026-03-31'`, `as_of_date='2026-05-29'`:

- **429 rows**, zero `NaN`s anywhere in the result.
- **100% of returned CUSIPs** are within the S&P 500 as of that date (cross-checked against `sp500_members()` independently).
- Breadth values are plausible and match real-world institutional ownership patterns: MSFT (`594918104`) tops the list at 6,122 distinct filers, AAPL (`037833100`) at 6,027, AMZN (`023135106`) at 5,931; median breadth across the 429 names is 1,172.
- Universe size (S&P 500 CUSIPs resolvable as of that date): 430 — meaning ~85% of the *current* S&P 500 constituents resolve correctly, notably higher than the 65.1% aggregate-across-history figure, consistent with the diagnosis above (current, still-independently-trading names resolve far more reliably than the full 14-year historical set, which includes many now-defunct tickers).

---

## What Phase 2 deliberately does *not* do

Everything here is a conscious, disclosed deferral, not an oversight:
- **Full historical CUSIP/ticker genealogy.** The crosswalk reflects each CUSIP's *current* ticker (plus a handful of manually-curated aliases/old-CUSIPs); a name that was acquired, merged, or renamed mid-window may not resolve under its historical label. Demonstrated with 4 real override examples, not comprehensively patched.
- **VALUE unit correction** for the 2023-01-03 thousands→dollars change (still deferred from Phase 1) — irrelevant to `breadth()`, necessary before any value-weighted factor.
- **Filer dedup across CIK changes** beyond zero-padding — a manager restructuring under an entirely new CIK (as several passive giants apparently have) is not automatically detected; the passive-manager list was curated with this in mind but isn't a general solution.
- **Full `min_breadth=10` crosswalk build.** The committed `cusip_ticker_map.csv` was built at `min_breadth=100` due to real unauthenticated rate-limit constraints (10 jobs/request, ~25 req/min). Rebuildable at the originally-planned threshold with a free `OPENFIGI_API_KEY` — see `SOURCES.md` for the exact command.
- **Confidential-treatment holdings recovery.** COVERPAGE now *detects* confidential-treatment history, but the omitted position itself is fundamentally unrecoverable from public data until the manager's own later disclosure — this is a property of the reporting regime, not something more engineering can fix.

## Recommended next step

Phase 3 (`src/factor.py`): `signal(cusip, period) = breadth(t) - breadth(t-1)`, cross-sectionally standardized, built directly on `universe.sp500_universe_breadth()`'s now-verified output. The interface contract (`[CUSIP, breadth]`, point-in-time correct, S&P 500-restricted, passive-manager-excluded) is exactly what this phase needs and is already tested end-to-end against real data.
