# Schonfeld Case Study

Quant research case study. Python, pandas, scipy.

## Setup

```bash
./setup.sh
source .venv/bin/activate
```

## Structure

```
data/
  raw/         # untouched input data (gitignored — never commit)
  processed/   # derived/cleaned data (gitignored — never commit)
notebooks/     # exploratory analysis
src/           # reusable analysis code
tests/         # tests for src/
```

## Commands

```bash
pytest         # run tests
```

## Conventions

- Reusable logic goes in `src/`; notebooks are for exploration, not the
  final analysis logic.
- Nothing under `data/raw/` or `data/processed/` is committed — treat it as
  local-only input/output.
