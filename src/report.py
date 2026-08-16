"""Self-contained HTML backtest report.

Renders src.backtest.run_backtest()'s results dict into a single HTML file
with no external assets -- every chart is a matplotlib figure embedded
directly as a base64 PNG data URI, matching CLAUDE.md's "self-contained
HTML deliverable" requirement for results/. No new charting dependency:
matplotlib is already pinned in requirements.txt.

Layout mirrors the case-study prompt's own evaluation criteria: a primary
equity curve against both benchmarks, then the lag and cost sensitivity
grids the prompt explicitly asks about (rebalancing timing / transaction
costs), then rank-IC and turnover as supporting diagnostics, then a
disclosures section naming every caveat already surfaced in Phases 1-4
(coverage gaps, mega-cap tilt, lag-decay uncertainty, open-quarter
exclusion) so a reader never has to go dig for them.
"""

import base64
import io
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

from src.backtest import (
    COST_BPS_GRID,
    LAG_DAYS_GRID,
    PRIMARY_COST_BPS,
    PRIMARY_LAG_DAYS,
    performance_stats,
)

DEFAULT_OUT_PATH = Path("results/backtest_report.html")

_CSS = """
body { font-family: -apple-system, Segoe UI, Helvetica, Arial, sans-serif; max-width: 960px; margin: 40px auto; padding: 0 20px; color: #1a1a1a; line-height: 1.5; }
h1 { font-size: 1.6em; border-bottom: 2px solid #333; padding-bottom: 8px; }
h2 { font-size: 1.25em; margin-top: 2.2em; border-bottom: 1px solid #ccc; padding-bottom: 4px; }
table { border-collapse: collapse; width: 100%; margin: 1em 0; font-size: 0.92em; }
th, td { border: 1px solid #ddd; padding: 6px 10px; text-align: right; }
th { background: #f2f2f2; text-align: center; }
td:first-child, th:first-child { text-align: left; }
img { max-width: 100%; height: auto; display: block; margin: 1em 0; }
.caveat { background: #fff8e1; border-left: 4px solid #f0ad4e; padding: 10px 14px; margin: 1em 0; }
.meta { color: #555; font-size: 0.9em; }
code { background: #f2f2f2; padding: 1px 5px; border-radius: 3px; }
"""


def _fig_to_data_uri(fig) -> str:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=130, bbox_inches="tight")
    plt.close(fig)
    encoded = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def _line_chart(series_dict: dict, *, title: str, ylabel: str) -> str:
    fig, ax = plt.subplots(figsize=(8, 4))
    for label, series in series_dict.items():
        if series is not None and not series.empty:
            ax.plot(series.index, series.values, label=label, linewidth=1.4)
    ax.set_title(title)
    ax.set_ylabel(ylabel)
    ax.legend(fontsize=8)
    ax.grid(alpha=0.3)
    fig.autofmt_xdate()
    return _fig_to_data_uri(fig)


def _bar_chart(labels: list, values: list, *, title: str, ylabel: str) -> str:
    fig, ax = plt.subplots(figsize=(6, 3.5))
    ax.bar([str(l) for l in labels], values, color="#4472C4")
    ax.set_title(title)
    ax.set_ylabel(ylabel)
    ax.grid(axis="y", alpha=0.3)
    return _fig_to_data_uri(fig)


def _stats_table(rows: list[dict], *, row_label_key: str, row_label_name: str) -> str:
    if not rows:
        return "<p><em>No closed quarters available.</em></p>"
    cols = ["total_return", "annualized_return", "annualized_vol", "sharpe", "max_drawdown", "hit_rate", "n_days"]
    header = "".join(f"<th>{c.replace('_', ' ')}</th>" for c in cols)
    body = ""
    for row in rows:
        cells = "".join(
            f"<td>{row[c]:.2%}</td>" if c in {"total_return", "annualized_return", "annualized_vol", "max_drawdown", "hit_rate"}
            else f"<td>{row[c]:.2f}</td>" if c == "sharpe"
            else f"<td>{int(row[c])}</td>"
            for c in cols
        )
        body += f"<tr><td>{row[row_label_key]}</td>{cells}</tr>"
    return f"<table><tr><th>{row_label_name}</th>{header}</tr>{body}</table>"


def build_report(results: dict, out_path: Path = DEFAULT_OUT_PATH, *, meta: dict | None = None) -> Path:
    """Renders `results` (src.backtest.run_backtest()'s output) to a
    self-contained HTML file at `out_path`. `meta` is an optional dict of
    free-form run metadata (window dates, universe size, panel row counts)
    shown at the top of the report."""
    meta = meta or {}
    primary_key = (PRIMARY_LAG_DAYS, PRIMARY_COST_BPS)
    sections = []

    sections.append(f"<h1>13F Ownership-Breadth-Momentum Backtest</h1>")
    meta_html = "".join(f"<li><strong>{k}:</strong> {v}</li>" for k, v in meta.items())
    sections.append(f'<div class="meta"><ul>{meta_html}</ul></div>')

    # --- Primary result -----------------------------------------------
    if primary_key in results:
        primary = results[primary_key]
        chart = _line_chart(
            {
                f"Long-short spread ({PRIMARY_LAG_DAYS}d lag, {PRIMARY_COST_BPS}bps)": primary["spread_nav"],
                "Internal equal-weight universe": primary["universe_nav"],
                "SPY": primary["spy_nav"],
            },
            title="Primary backtest: long-short spread vs. benchmarks",
            ylabel="NAV (start = 1.0)",
        )
        sections.append("<h2>Primary result</h2>")
        sections.append(f'<img src="{chart}" alt="primary equity curve">')
        stats_rows = [
            {"label": "Long-short spread", **performance_stats(primary["spread_nav"])},
            {"label": "Internal equal-weight universe", **performance_stats(primary["universe_nav"])},
            {"label": "SPY", **performance_stats(primary["spy_nav"])},
        ]
        sections.append(_stats_table(stats_rows, row_label_key="label", row_label_name="Series"))
        if primary["open_quarters"]:
            oq = primary["open_quarters"][0]
            sections.append(
                f'<p class="meta">The most recent formation quarter '
                f'({oq["period_of_report"].date()}, formed {oq["as_of_date"].date()}) is still open '
                f"as of this run and is excluded from the stats above -- its next rebalance hasn't happened yet.</p>"
            )

    # --- Lag sensitivity -------------------------------------------------
    lag_series = {
        f"{lag}d lag": results[(lag, PRIMARY_COST_BPS)]["spread_nav"]
        for lag in LAG_DAYS_GRID if (lag, PRIMARY_COST_BPS) in results
    }
    if lag_series:
        chart = _line_chart(lag_series, title=f"Lag sensitivity (cost = {PRIMARY_COST_BPS}bps)", ylabel="NAV (start = 1.0)")
        sections.append("<h2>Formation-lag sensitivity</h2>")
        sections.append(f'<img src="{chart}" alt="lag sensitivity">')
        lag_rows = [
            {"label": f"{lag}d", **performance_stats(results[(lag, PRIMARY_COST_BPS)]["spread_nav"])}
            for lag in LAG_DAYS_GRID if (lag, PRIMARY_COST_BPS) in results
        ]
        sections.append(_stats_table(lag_rows, row_label_key="label", row_label_name="Lag"))
        sections.append(
            '<div class="caveat"><strong>Read with caution:</strong> a longer lag captures more filers '
            "before formation (96.3% by day 60 vs. 87.0% by day 45, measured on the full panel) but also "
            "enters later into any front-loaded post-disclosure return decay. Whichever direction this table "
            "shows should not be over-read as proof of the underlying mechanism -- it's one realization over "
            "one historical window, not a controlled experiment.</div>"
        )

    # --- Cost sensitivity --------------------------------------------------
    cost_labels, cost_returns = [], []
    for cost_bps in COST_BPS_GRID:
        key = (PRIMARY_LAG_DAYS, cost_bps)
        if key in results:
            stats = performance_stats(results[key]["spread_nav"])
            cost_labels.append(f"{cost_bps}bps")
            cost_returns.append(stats["annualized_return"])
    if cost_labels:
        chart = _bar_chart(cost_labels, cost_returns, title=f"Transaction-cost sensitivity ({PRIMARY_LAG_DAYS}d lag)", ylabel="Annualized return")
        sections.append("<h2>Transaction-cost sensitivity</h2>")
        sections.append(f'<img src="{chart}" alt="cost sensitivity">')

    # --- Rank IC -------------------------------------------------------------
    if primary_key in results:
        ic_series = results[primary_key]["ic_series"]
        if not ic_series.empty:
            chart = _line_chart({"Rank IC (Spearman)": ic_series}, title="Per-quarter rank-IC", ylabel="Spearman correlation")
            sections.append("<h2>Rank information coefficient</h2>")
            sections.append(f'<img src="{chart}" alt="rank IC">')
            sections.append(
                f"<p>Mean IC: {ic_series.mean():.3f}, std: {ic_series.std():.3f}, "
                f"positive quarters: {(ic_series > 0).mean():.1%} of {len(ic_series)}.</p>"
            )

    # --- Turnover --------------------------------------------------------
    if primary_key in results and results[primary_key]["quarters"]:
        quarters = results[primary_key]["quarters"]
        labels = [q["period_of_report"].date().isoformat() for q in quarters]
        turnover = [q["turnover_long"] for q in quarters]
        chart = _bar_chart(labels, turnover, title="Long-leg turnover by quarter", ylabel="One-way turnover fraction")
        sections.append("<h2>Turnover</h2>")
        sections.append(f'<img src="{chart}" alt="turnover">')

    # --- Disclosures ------------------------------------------------------
    sections.append("<h2>Known limitations, disclosed</h2>")
    sections.append(
        '<div class="caveat">'
        "<ul>"
        "<li><strong>CUSIP-mapping coverage gap:</strong> only tickers with a resolved CUSIP crosswalk "
        "entry (OpenFIGI, current-ticker-only) are tradable here -- names acquired, merged, or delisted "
        "mid-window are systematically under-represented; the internal equal-weight universe benchmark "
        "exists specifically to separate this effect from genuine factor skill.</li>"
        "<li><strong>Mega-cap tilt:</strong> raw (unscaled) breadth change mechanically favors names with "
        "a larger baseline breadth, most visible in the short leg -- documented in Phase 3, not corrected.</li>"
        "<li><strong>Missing price data:</strong> a name with no usable adjusted-close data for a given "
        "quarter is dropped from that quarter's basket (not imputed) -- see the panel's dropped-ticker log.</li>"
        "<li><strong>rf = 0</strong> for all Sharpe ratios -- no risk-free-rate series was sourced.</li>"
        "<li><strong>SPY / internal benchmark carry no simulated transaction costs</strong>; only the "
        "long-short strategy legs do.</li>"
        "</ul></div>"
    )

    body = "\n".join(sections)
    html = f"<title>13F Backtest Report</title>\n<style>{_CSS}</style>\n{body}\n"

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html)
    return out_path


def main() -> None:
    import argparse
    import pickle

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", default="data/processed/backtest_results.pkl")
    parser.add_argument("--out", default=str(DEFAULT_OUT_PATH))
    args = parser.parse_args()

    with open(args.results, "rb") as fh:
        payload = pickle.load(fh)
    results = payload["results"] if isinstance(payload, dict) and "results" in payload else payload
    meta = payload.get("meta") if isinstance(payload, dict) else None

    out_path = build_report(results, out_path=Path(args.out), meta=meta)
    print(f"Report written -> {out_path}")


if __name__ == "__main__":
    main()
