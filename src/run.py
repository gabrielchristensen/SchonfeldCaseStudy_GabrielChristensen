"""Single entry point for the whole pipeline: `python -m src.run --mode ...`.

Every module under `src/` already has its own tested CLI
(`ingest.py`, `backtest.py`, `report.py`, `detail.py`) -- this doesn't
re-implement any of their logic, it shells out to each one in the right
order with matching, explicit paths, so the pipeline can be reproduced
with one command instead of manually copy-pasting several `python -m
src.X` invocations and keeping their `--panel`/`--out`/`--results`/
`--prices` flags in sync by hand.

Three modes:

- `report-only` (default): regenerates `results/backtest_report.html` and
  `detail.py`'s two CSVs from the already-committed
  `data/processed/backtest_results.pkl` + `prices.parquet`. No network,
  cheap, safe to run with zero flags.
- `smoke`: a real end-to-end wiring check -- sample-mode ingest (real SEC
  download) -> a `--quick` backtest on that sample panel (real yfinance
  calls) -> a report -- proving the whole chain actually works, without the
  --full pipeline's multi-GB/multi-hour cost. Too few quarters for
  `detail.py`'s regime-split analysis to mean anything, so `detail` is
  skipped in this mode. Everything writes under `results/smoke/`, never
  touching the real committed deliverable.
- `full`: the real, expensive reproduction from zero -- `ingest --full`,
  then the full backtest sweep, then `report`/`detail` against the real
  output. Multi-GB download, can take hours; the committed artifacts
  already represent one verified run of this.

Reference-data builds (`mapping.py --build`, `universe.py
--build-sp500-history`) are deliberately NOT run by any mode here -- they
build the small, already-committed crosswalk/S&P-history CSVs under
`data/reference/`, a one-time step, not part of the repeatable pipeline.
Run them directly if you ever need to rebuild those.
"""

import argparse
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_PROCESSED = REPO_ROOT / "data" / "processed"
RESULTS_DIR = REPO_ROOT / "results"
SMOKE_DIR = RESULTS_DIR / "smoke"


def _py(*args: str) -> list[str]:
    return [sys.executable, "-m", *args]


def build_stages(mode: str) -> list[tuple[str, list[str]]]:
    """Returns an ordered list of (description, argv) for `mode`. Every
    path passed between stages is constructed here, once, so consecutive
    stages are guaranteed to actually chain -- not left to each module's
    own independently-defaulted `--out`/`--panel`/`--results` agreeing by
    accident (the exact class of drift a full-repo audit found: e.g.
    `backtest.py --quick`'s output path isn't `report.py`'s default input
    path)."""
    results_pkl = DATA_PROCESSED / "backtest_results.pkl"
    prices_parquet = DATA_PROCESSED / "prices.parquet"

    if mode == "report-only":
        return [
            (
                "Generate HTML report from the committed backtest results",
                _py(
                    "src.report",
                    "--results", str(results_pkl),
                    "--prices", str(prices_parquet),
                    "--out", str(RESULTS_DIR / "backtest_report.html"),
                ),
            ),
            (
                "Generate per-asset detail + sub-period stats CSVs",
                _py(
                    "src.detail",
                    "--results", str(results_pkl),
                    "--prices", str(prices_parquet),
                    "--out", str(DATA_PROCESSED / "quarter_asset_detail.csv"),
                    "--subperiods-out", str(DATA_PROCESSED / "subperiod_stats.csv"),
                ),
            ),
        ]

    if mode == "smoke":
        sample_panel = DATA_PROCESSED / "13f_panel_sample.parquet"
        quick_results = SMOKE_DIR / "backtest_results_quick.pkl"
        quick_prices = SMOKE_DIR / "prices_quick.parquet"
        return [
            (
                "Ingest the 3-dataset validation sample (real SEC download)",
                # ingest.py has no --out flag; sample mode always writes to
                # `sample_panel`'s hardcoded path -- referenced below, not
                # re-derived.
                _py("src.ingest"),
            ),
            (
                "Quick backtest on the sample panel (real yfinance calls)",
                _py(
                    "src.backtest",
                    "--panel", str(sample_panel),
                    "--quick",
                    "--out", str(quick_results),
                    "--prices-cache", str(quick_prices),
                ),
            ),
            (
                "Generate a smoke-test HTML report (not the real deliverable)",
                _py(
                    "src.report",
                    "--results", str(quick_results),
                    "--prices", str(quick_prices),
                    "--out", str(SMOKE_DIR / "backtest_report.html"),
                ),
            ),
        ]

    if mode == "full":
        full_panel = DATA_PROCESSED / "13f_panel_full.parquet"
        return [
            (
                "Full SEC ingest (~55 zips, several GB, real network)",
                # ingest.py --full has no --out flag either; it always
                # writes to `full_panel`'s hardcoded path.
                _py("src.ingest", "--full"),
            ),
            (
                "Full backtest sweep (real yfinance calls)",
                _py(
                    "src.backtest",
                    "--panel", str(full_panel),
                    "--out", str(results_pkl),
                    "--prices-cache", str(prices_parquet),
                ),
            ),
            (
                "Generate HTML report",
                _py(
                    "src.report",
                    "--results", str(results_pkl),
                    "--prices", str(prices_parquet),
                    "--out", str(RESULTS_DIR / "backtest_report.html"),
                ),
            ),
            (
                "Generate per-asset detail + sub-period stats CSVs",
                _py(
                    "src.detail",
                    "--results", str(results_pkl),
                    "--prices", str(prices_parquet),
                    "--out", str(DATA_PROCESSED / "quarter_asset_detail.csv"),
                    "--subperiods-out", str(DATA_PROCESSED / "subperiod_stats.csv"),
                ),
            ),
        ]

    raise ValueError(f"unknown mode: {mode!r}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--mode", choices=["report-only", "smoke", "full"], default="report-only",
        help="report-only (default, cheap, offline) / smoke (real but small end-to-end "
             "wiring check) / full (the real, expensive reproduction from zero)",
    )
    args = parser.parse_args()

    if args.mode == "smoke":
        print("Note: --mode smoke skips detail.py -- the sample panel's handful of "
              "quarters is too few for detail.py's regime-split analysis to mean "
              "anything. This mode proves the wiring works, not a meaningful backtest.",
              flush=True)

    stages = build_stages(args.mode)
    n = len(stages)
    for i, (description, argv) in enumerate(stages, start=1):
        # flush=True: without it, the parent's own stdout is fully
        # block-buffered whenever it isn't a TTY (piped through a tool,
        # redirected to a file, ...), so this banner would sit in the
        # buffer until the whole script exits -- while each child
        # subprocess's inherited stdout flushes on its own, real exit.
        # Verified for real: without flush=True, every banner printed
        # AFTER all the child processes' own output instead of before
        # each stage, the opposite of what a progress banner is for.
        print(f"=== [{i}/{n}] {description} ===", flush=True)
        try:
            subprocess.run(argv, check=True, cwd=REPO_ROOT)
        except subprocess.CalledProcessError:
            print(f"=== FAILED at stage: {description} ===", flush=True)
            sys.exit(1)

    print(f"=== Pipeline complete ({args.mode}) ===", flush=True)


if __name__ == "__main__":
    main()
