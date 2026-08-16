# Schonfeld Case Study

13F positioning factor and backtester (Python, pandas, scipy). Quant
research case study — see `docs/prompt.pdf` for the original brief and
`docs/memo.md` for the 3-5 page deliverable memo.

## Setup

Built and tested against **Python 3.12.3** specifically (`.python-version`;
`pyproject.toml` declares `>=3.10` as the floor `setup.sh` enforces, but
3.12.3 is the exact version this was validated against).
`requirements.txt` pins every other dependency to an exact version too —
a fresh clone should reproduce the same environment this was built in,
not whatever's newest at install time.

**macOS / Linux:**
```bash
git clone <repo-url>
cd SchonfeldCaseStudy_GabrielChristensen
./setup.sh
source .venv/bin/activate
```

**Windows:** `setup.sh` needs a POSIX shell — it does not run under
native `cmd`/PowerShell. Use **WSL** (recommended — ships `python3` out
of the box) or **Git Bash** (works too, but only if your Windows Python
install put `python3` or `python` on Git Bash's `PATH` — `setup.sh`
checks for either):
```bash
git clone <repo-url>
cd SchonfeldCaseStudy_GabrielChristensen
bash setup.sh
source .venv/bin/activate
```
WSL install: https://learn.microsoft.com/windows/wsl/install — Git Bash
ships with Git for Windows: https://git-scm.com/download/win

**Manual setup** (any OS, skips `setup.sh`'s Python-version check — confirm
you're on ≥3.10, ideally 3.12.3, yourself first):
```bash
# macOS / Linux / WSL / Git Bash:
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Windows native (cmd or PowerShell):
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

## How to evaluate this repo, in increasing order of cost

**1. Prove the pipeline is real (~1 minute, live SEC data):**
```bash
pytest                 # 129 tests, fully offline, no data needed
python -m src.ingest   # downloads 3 real SEC 13F datasets, parses them --
                        # watch real filing data flow into a point-in-time panel
```

**2. See the deliverable (instant, nothing to run):**
- `docs/memo.md` — the case-study memo.
- `results/backtest_report.html` — the self-contained backtest report.
- `notebooks/regime_and_attribution.ipynb` — already executed, real
  outputs (tables, equity curves, drawdown charts).

**3. Verify the deliverable is reproducible, offline, from committed data:**
A handful of the actual pipeline's output files are committed
deliberately (not just source code) — `data/processed/backtest_results.pkl`,
`data/processed/prices.parquet`, `data/processed/quarter_asset_detail.csv`,
`data/processed/subperiod_stats.csv` — small (~11MB total) and load-bearing
for this specific reason: they let the reporting/analysis layer be
re-run and checked against what's already committed, with **no network
call and no re-running the actual backtest**:
```bash
python -m src.report --results data/processed/backtest_results.pkl
python -m src.detail --results data/processed/backtest_results.pkl \
  --prices data/processed/prices.parquet
```
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
```bash
python -m src.ingest --full   # entire SEC archive, ~55 zips, several GB, checkpointed
python -m src.backtest --panel data/processed/13f_panel_full.parquet \
  --out data/processed/backtest_results.pkl
```
The full lag×cost sensitivity grid now runs in a few minutes (a real
performance bottleneck was found and fixed during this project — see
`docs/full_project_documentation.md` §2, Phase 4) — this is fast enough
to run live if useful during a technical discussion, not something that
has to be taken on faith.

## Structure

```
docs/
  prompt.pdf                      # case study prompt
  memo.md                         # 3-5 page deliverable memo
  full_project_documentation.md   # full technical reference: structure,
                                   # workflow, every formula, phase-by-phase
                                   # build history, defense quick-reference
  phase1_report.md ... phase4_audit.md   # detailed per-phase records
data/
  raw/         # untouched SEC downloads (gitignored)
  processed/   # derived/cleaned data (gitignored, except the 4 small
               # committed artifacts described above)
  reference/   # small committed reference tables (CUSIP crosswalk,
               # S&P 500 point-in-time membership, passive-manager list)
notebooks/     # exploratory analysis (regime_and_attribution.ipynb)
src/           # reusable pipeline code: ingest, pit, mapping, universe,
               # factor, backtest, report, detail
tests/         # one test file per src/ module, 129 tests total
results/       # the committed, self-contained HTML backtest report
```

## Tests

```bash
pytest
```
