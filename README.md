# Schonfeld Case Study

13F positioning factor and backtester (Python, pandas, scipy). Quant
research case study — see `docs/prompt.pdf` for the original brief and
`docs/memo.md` for the 3-5 page deliverable memo.

## Note

The pipeline requires at least 16 GB RAM. The model
must be futher developed to have a better usage of memory

## Setup

Requires **Python 3.12.x** (`.python-version` pins 3.12.3 exactly, the
version this was validated against; `pyproject.toml` enforces `>=3.12` as
the floor). This is a hard requirement, not a suggestion: `requirements.txt`
pins exact dependency versions (`numpy==2.5.2`, `scipy==1.18.0`, and others)
whose published wheels require Python 3.12+ — `pip install` will fail on
3.10 or 3.11 even though none of this project's own code needs anything
newer than 3.10's syntax. A fresh clone on Python 3.12.x should reproduce
the same environment this was built in, not whatever's newest at install
time.

**macOS / Linux:**
```bash
git clone <repo-url>
cd SchonfeldCaseStudy_GabrielChristensen
./setup.sh
source .venv/bin/activate
```

**Windows (recommended — plain, native, no extra software):** confirm
your Python first, then set up the venv manually:
```bash
python --version
```
Confirm the output is 3.12.x, then:
```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```
This is the actual environment this project was validated against on
Windows during development — plain `cmd`/PowerShell, nothing else
required.

**Windows (alternative)**: `setup.sh` runs `python --version`'s check
for you automatically, but needs a POSIX shell — **WSL** (ships
`python3` out of the box) or **Git Bash** (works too, but only if your
Windows Python install put `python3` or `python` on Git Bash's `PATH`):
```bash
git clone <repo-url>
cd SchonfeldCaseStudy_GabrielChristensen
bash setup.sh
source .venv/bin/activate
```
WSL install: https://learn.microsoft.com/windows/wsl/install — Git Bash
ships with Git for Windows: https://git-scm.com/download/win

**Manual setup on macOS / Linux / WSL / Git Bash** (skips `setup.sh`'s
Python-version check — confirm you're on 3.12.x yourself first):
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## How to evaluate this repo, in increasing order of cost

**0. The single-command version:** `python -m src.run` regenerates the
HTML report + detail CSVs from the committed backtest artifacts (no
network, no re-run of the backtest — the same thing step 3 below does with
two separate commands). `python -m src.run --mode smoke` is a real,
small, end-to-end wiring check (real SEC + yfinance calls, but a 3-file
sample instead of the full archive) that exercises the *whole* chain —
ingest → backtest → report — in one shot; `python -m src.run --mode full`
is the one-command version of step 4's real full reproduction. See
`src/run.py`'s module docstring (or `python -m src.run --help`) for what
each mode actually does and why. The steps below show the same things run
manually, one module at a time, for anyone who wants to see each stage in
isolation rather than trust an orchestrator.

**1. Prove the pipeline is real (~1 minute, live SEC data):**
`pytest` runs 163 tests, fully offline. `python -m src.ingest` (no
flags) downloads 3 real SEC 13F datasets and parses them — watch real
filing data flow into a point-in-time panel. Each command below is a
single line — copy-paste it exactly as shown, on any OS/shell:
```bash
pytest
python -m src.ingest
```

**2. See the deliverable (instant, nothing to run):**
- `docs/memo.md` — the case-study memo.
- `results/backtest_report.html` — the self-contained backtest report.

**3. Verify the deliverable is reproducible, offline, from committed data:**
A handful of the actual pipeline's output files are committed
deliberately (not just source code) — `data/processed/backtest_results.pkl`,
`data/processed/prices.parquet`, `data/processed/quarter_asset_detail.csv`,
`data/processed/subperiod_stats.csv` — small (~11MB total) and load-bearing
for this specific reason: they let the reporting/analysis layer be
re-run and checked against what's already committed, with **no network
call and no re-running the actual backtest**. `python -m src.run` below is
equivalent to running the two commands after it separately:
```bash
python -m src.run
```
```bash
python -m src.report --results data/processed/backtest_results.pkl --prices data/processed/prices.parquet
python -m src.detail --results data/processed/backtest_results.pkl --prices data/processed/prices.parquet
```
`--prices` on `src.report` is optional — when supplied it folds a full
Regime & attribution section (per-era Sharpe/IC, benchmark correlation and
beta, top-contributor tickers) straight into the HTML report, reusing
`src.detail`'s already-tested functions against the same committed
artifacts; `src.detail`'s own CLI still runs separately to produce the two
CSVs that section is built from. Every chart in the report is also written
out as a standalone `.jpg` under `results/charts/`, in addition to being
embedded inline.

`prices.parquet` specifically is committed because yfinance's adjusted-
close data gets revised retroactively (splits/dividends) — re-fetching
live could produce numbers that drift slightly from what the memo
reports; the committed snapshot makes the memo's numbers exactly
reproducible, not just directionally similar.

Everything else under `data/` (the raw SEC downloads, the full parsed
panel) is gitignored on purpose — it's either a straight copy of public
SEC data or fully regenerable from committed inputs, so it isn't
committed weight.

**4. Reproduce the real backtest from zero (real time, optional):**
`python -m src.ingest --full` downloads the entire SEC archive (~55
zips, several GB, checkpointed — safe to interrupt and resume). Then
run the actual backtest against the full panel it produces, then
regenerate the report/detail outputs from that real result. `python -m
src.run --mode full` below is equivalent to running the four commands
after it separately:
```bash
python -m src.run --mode full
```
```bash
python -m src.ingest --full
python -m src.backtest --panel data/processed/13f_panel_full.parquet --out data/processed/backtest_results.pkl
python -m src.report --results data/processed/backtest_results.pkl --prices data/processed/prices.parquet
python -m src.detail --results data/processed/backtest_results.pkl --prices data/processed/prices.parquet
```
The full lag×cost sensitivity grid now runs in a few minutes (a real
performance bottleneck was found and fixed during this project — see
`docs/full_project_documentation.md` §2, Phase 4) — this is fast enough
to run live if useful during a technical discussion, not something that
has to be taken on faith.

## Reference data (already built, committed, rarely needs rerunning)

`data/reference/cusip_ticker_map.csv` (the CUSIP↔ticker crosswalk) and
`data/reference/sp500_history.csv` (point-in-time S&P 500 membership) are
small, committed, one-time builds — not part of the repeatable pipeline
above, and not run by `src/run.py`'s stages. Rebuild either only if you
have a specific reason to (a new CUSIP needs mapping, the S&P history
needs extending):
```bash
python -m src.mapping --build
python -m src.universe --build-sp500-history
```

## Structure

```
docs/
  prompt.pdf                      # case study prompt
  memo.md                         # 3-5 page deliverable memo
  full_project_documentation.md   # full technical reference: structure,
                                   # workflow, every formula, phase-by-phase
                                   # build history, defense quick-reference
data/
  raw/         # untouched SEC downloads (gitignored)
  processed/   # derived/cleaned data (gitignored, except the 4 small
               # committed artifacts described above)
  reference/   # small committed reference tables (CUSIP crosswalk,
               # S&P 500 point-in-time membership, passive-manager list)
src/           # reusable pipeline code: ingest, pit, mapping, universe,
               # factor, backtest, report, detail, run (single entry
               # point), _http (shared resilient GET/POST retry helpers)
tests/         # one test file per src/ module, 163 tests total
results/       # the committed, self-contained HTML backtest report
  backtest_report.html   # the report itself (charts embedded inline)
  charts/                 # the same charts, also as standalone .jpg files
  smoke/                  # python -m src.run --mode smoke's output (gitignored)
```

## Tests

```bash
pytest
```
