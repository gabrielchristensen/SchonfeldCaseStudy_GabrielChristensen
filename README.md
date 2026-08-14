# Schonfeld Case Study

Quant research case study (Python, pandas, scipy).

## Setup

```bash
git clone <repo-url>
cd SchonfeldCaseStudy_GabrielChristensen
./setup.sh
source .venv/bin/activate
```

Or manually:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Structure

```
docs/
  prompt.pdf   # case study prompt
  memo.md      # 3-5 page deliverable memo
data/
  raw/         # untouched input data (gitignored)
  processed/   # derived/cleaned data (gitignored)
  reference/   # small committed reference tables (CUSIP overrides, S&P 500 membership, etc.)
notebooks/     # exploratory analysis
src/           # reusable analysis code (ingest, pit, mapping, universe, factor, backtest, report)
tests/         # tests for src/
results/       # generated backtest report (self-contained HTML deliverable)
```

## Tests

```bash
pytest
```
