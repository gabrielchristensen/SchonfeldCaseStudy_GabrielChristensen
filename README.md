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
data/
  raw/         # untouched input data (gitignored)
  processed/   # derived/cleaned data (gitignored)
notebooks/     # exploratory analysis
src/           # reusable analysis code
tests/         # tests for src/
```

## Tests

```bash
pytest
```
