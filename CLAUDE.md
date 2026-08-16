# Schonfeld Case Study

Quant research case study. Python, pandas, scipy.

## Setup

```bash
./setup.sh
source .venv/bin/activate
```

## Structure

```
docs/
  prompt.pdf   # case study prompt
  memo.md      # 3-5 page deliverable memo
data/
  raw/         # untouched input data (gitignored — never commit)
  processed/   # derived/cleaned data (gitignored — never commit)
  reference/   # small committed reference tables (CUSIP overrides, S&P 500 membership, etc.)
notebooks/     # exploratory analysis
src/           # reusable analysis code (ingest, pit, mapping, universe, factor, backtest, report, detail, run, _http)
tests/         # tests for src/
results/       # generated backtest report (self-contained HTML deliverable)
```

## Commands

```bash
pytest                                # run tests
python -m src.run                     # regenerate report+detail from committed backtest artifacts (no network)
python -m src.run --mode smoke        # real, small end-to-end wiring check (ingest -> backtest -> report)
python -m src.run --mode full         # the real, expensive full reproduction from zero
python -m src.ingest                  # build the 3-dataset validation sample panel
python -m src.ingest --full           # download+parse every SEC-listed dataset (~55 zips, several GB)
```

## Conventions

- Reusable logic goes in `src/`; notebooks are for exploration, not the
  final analysis logic.
- Nothing under `data/raw/` or `data/processed/` is committed — treat it as
  local-only input/output.
